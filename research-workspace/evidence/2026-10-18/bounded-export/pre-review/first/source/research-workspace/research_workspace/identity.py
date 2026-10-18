"""Transient, online identity authority for the isolated synthetic OIDC pilot.

The proxy owns the browser protocol. This adapter accepts no browser identity
claims, performs no JWT cryptography, and never persists access tokens.
"""

from dataclasses import dataclass
from contextlib import closing, contextmanager
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
from http.client import HTTPException
from queue import Queue, Empty
import math
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .store import AccessError, identifier


class IdentityUnavailable(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


@dataclass(repr=False)
class Context:
    subject: str
    sid: str
    token: str
    expires: int
    session_key: str


class Identity:
    """All sessions and queued authorizations disappear on process restart."""

    def __init__(self, config, store, *, clock=time.time):
        if store.finite:
            raise ValueError("This finite disposable profile supports local keys; a finite OIDC profile is not qualified.")
        required = {"issuer", "audience", "client_id", "client_secret", "proxy_auth_url",
                    "introspection_url", "boundary_secret", "public_origin", "subjects",
                    "cookie_name", "proxy_cookie_name"}
        if not isinstance(config, dict) or set(config) != required:
            raise ValueError("Use the complete operator-owned synthetic OIDC configuration.")
        for field in required - {"subjects"}:
            value = config[field]
            if not isinstance(value, str) or not value or len(value) > 2000 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid synthetic identity configuration.")
        origin = urlsplit(config["public_origin"])
        if (origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port
                or origin.path or origin.query or origin.fragment or origin.username):
            raise ValueError("This identity slice requires a loopback-only browser entrance.")
        if not config["issuer"].startswith(config["public_origin"] + "/idp/realms/"):
            raise ValueError("The synthetic issuer must use the configured local identity route.")
        for field in ("proxy_auth_url", "introspection_url"):
            parsed = urlsplit(config[field])
            if (parsed.scheme != "http" or parsed.hostname not in {"oauth2-proxy", "keycloak", "127.0.0.1"}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment):
                raise ValueError("Identity backchannels must stay on the isolated pilot network.")
        if len(config["boundary_secret"]) < 32 or len(config["client_secret"]) < 32:
            raise ValueError("Generate fresh bounded identity secrets for this pilot.")
        subjects = config["subjects"]
        if not isinstance(subjects, dict) or not 1 <= len(subjects) <= 16:
            raise ValueError("Provision between one and sixteen stable subject mappings.")
        for external, internal in subjects.items():
            identifier(external)
            identifier(internal)
        if len(set(subjects.values())) != len(subjects):
            raise ValueError("Identity mappings must be one-to-one.")
        for field in ("cookie_name", "proxy_cookie_name"):
            identifier(config[field])
        self.config, self.store, self.clock = dict(config), store, clock
        self.epoch = math.floor(clock()) + 1
        self.lock = threading.RLock()
        self.sessions, self.runs, self.revoked_sids = {}, {}, set()
        self.http = build_opener(ProxyHandler({}), NoRedirect())
        self.inflight = threading.BoundedSemaphore(4)

    def boundary(self, headers):
        supplied = headers.get_all("X-Research-Proxy", [])
        return len(supplied) == 1 and hmac.compare_digest(supplied[0], self.config["boundary_secret"])

    def _request(self, request, *, proxy=False):
        # urllib's socket timeout is not a total deadline (a peer can drip
        # headers/body forever). At most four daemon I/O tasks may remain in
        # flight; callers fail closed within six seconds even in that case.
        if not self.inflight.acquire(blocking=False):
            raise IdentityUnavailable("The finite identity service allowance is busy. Access remains closed.")
        result = Queue(maxsize=1)
        def fetch():
            try:
                result.put((True, self._fetch(request, proxy=proxy)))
            except Exception as exc:
                result.put((False, exc))
            finally:
                self.inflight.release()
        threading.Thread(target=fetch, daemon=True, name="identity-backchannel").start()
        try:
            ok, value = result.get(timeout=6)
        except Empty:
            raise IdentityUnavailable("The identity service exceeded its deadline. Access remains closed.") from None
        if not ok:
            if isinstance(value, (AccessError, IdentityUnavailable)):
                raise value
            raise IdentityUnavailable("The identity service returned an invalid response. Access remains closed.") from None
        return value

    def _fetch(self, request, *, proxy=False):
        try:
            with self.http.open(request, timeout=3) as response:
                content = response.read(32769)
                if len(content) > 32768:
                    raise IdentityUnavailable("The identity service response exceeded its bound.")
                if proxy:
                    if response.status != 202:
                        raise IdentityUnavailable("The sign-in service returned an unexpected response.")
                    tokens = response.headers.get_all("X-Auth-Request-Access-Token", [])
                    if len(tokens) != 1 or not 1 <= len(tokens[0]) <= 16384:
                        raise IdentityUnavailable("The sign-in service did not establish current identity.")
                    return tokens[0]
                from .runtime import strict_json
                return strict_json(content)
        except HTTPError as exc:
            exc.close()
            if proxy and exc.code in (401, 403):
                raise AccessError("Sign in with the identity provider to continue.") from None
            raise IdentityUnavailable("The identity service is unavailable. Access remains closed.") from None
        except (URLError, OSError, TimeoutError, ValueError, HTTPException):
            raise IdentityUnavailable("The identity service is unavailable. Access remains closed.") from None

    def _active_subject(self, subject):
        with closing(self.store.connect()) as db:
            if not db.execute("SELECT 1 FROM users WHERE subject=? AND active=1", (subject,)).fetchone():
                raise AccessError("This identity is not currently provisioned for the workspace.")

    def inspect_token(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(ord(c) < 33 for c in token):
            raise AccessError("Current identity could not be established.")
        body = urlencode({"token": token, "client_id": self.config["client_id"],
                          "client_secret": self.config["client_secret"]}).encode()
        claims = self._request(Request(self.config["introspection_url"], data=body,
                                      headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}))
        if not isinstance(claims, dict):
            raise IdentityUnavailable("The identity service returned an invalid response.")
        aud = claims.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud
        expires, auth_time, issued = claims.get("exp"), claims.get("auth_time"), claims.get("iat")
        sub, sid = claims.get("sub"), claims.get("sid")
        if (claims.get("active") is not True or claims.get("iss") != self.config["issuer"]
                or not isinstance(audiences, list) or self.config["audience"] not in audiences
                or type(expires) is not int or expires <= self.clock()
                or type(issued) is not int or not self.epoch <= issued <= self.clock() + 5
                or type(auth_time) is not int or not self.epoch <= auth_time <= self.clock() + 5
                or not isinstance(sub, str) or sub not in self.config["subjects"]
                or not isinstance(sid, str) or not 1 <= len(sid) <= 200):
            raise AccessError("Your current identity is expired or unavailable. Sign in again.")
        with self.lock:
            if sid in self.revoked_sids:
                raise AccessError("This workspace session was signed out. End the identity-provider session before signing in again.")
        subject = self.config["subjects"][sub]
        self._active_subject(subject)
        return subject, sid, expires

    def _browser_token(self, cookie):
        if not isinstance(cookie, str) or len(cookie) > 16384 or any(ord(c) < 32 for c in cookie):
            raise AccessError("Use a valid browser sign-in session.")
        jar = SimpleCookie()
        try:
            jar.load(cookie)
        except CookieError:
            raise AccessError("Use a valid browser sign-in session.") from None
        name = self.config["proxy_cookie_name"]
        # oauth2-proxy may split an encrypted session across numbered cookies.
        selected = [m.OutputString() for k, m in jar.items()
                    if k == name or (k.startswith(name + "_") and k[len(name) + 1:].isdigit())]
        if not selected or len(selected) > 8:
            raise AccessError("Sign in with the identity provider to continue.")
        # Only a private server-to-server call may obtain the token response.
        # The public ingress never exposes /oauth2/auth.
        return self._request(Request(self.config["proxy_auth_url"], headers={"Cookie": "; ".join(selected),
                             "X-Forwarded-Proto": "http", "X-Forwarded-Host": urlsplit(self.config["public_origin"]).netloc}), proxy=True)

    @staticmethod
    def _key(cookie):
        if not isinstance(cookie, str) or not 20 <= len(cookie) <= 128:
            raise AccessError("Your workspace session ended. Sign in again.")
        return hashlib.sha256(cookie.encode()).hexdigest()

    def begin(self, browser_cookie):
        token = self._browser_token(browser_cookie)
        subject, sid, expires = self.inspect_token(token)
        cookie = secrets.token_urlsafe(32)
        key = self._key(cookie)
        with self.lock:
            if sid in self.revoked_sids:
                raise AccessError("This workspace session was signed out.")
            self.sessions = {k: v for k, v in self.sessions.items() if v.expires > self.clock()}
            if len(self.sessions) >= 64 or len(self.revoked_sids) >= 128:
                raise IdentityUnavailable("The finite identity session allowance is full; ask the operator.")
            self.sessions[key] = Context(subject, sid, token, expires, key)
        return subject, cookie

    def authenticate(self, cookie, browser_cookie):
        key = self._key(cookie)
        token = self._browser_token(browser_cookie)
        subject, sid, expires = self.inspect_token(token)
        with self.lock:
            prior = self.sessions.get(key)
            if prior is None or sid in self.revoked_sids or prior.expires <= self.clock() or (prior.subject, prior.sid) != (subject, sid):
                raise AccessError("Your workspace session ended. Sign in again.")
            context = Context(subject, sid, token, expires, key)
            self.sessions[key] = context
            return context

    def logout(self, cookie):
        try:
            key = self._key(cookie)
        except AccessError:
            return
        with self.lock:
            context = self.sessions.pop(key, None)
            if context is not None:
                self.revoked_sids.add(context.sid)
                self.sessions = {k: v for k, v in self.sessions.items() if v.sid != context.sid}

    def bind(self, run_id, context):
        with self.lock:
            if run_id in self.runs:
                raise AccessError("An existing run cannot receive replacement identity authority.")
            if context.session_key not in self.sessions or context.sid in self.revoked_sids:
                raise AccessError("Identity authorization ended before submission.")
            if len(self.runs) >= 20 and run_id not in self.runs:
                raise IdentityUnavailable("The finite identity queue allowance is full.")
            self.runs[run_id] = context

    def check_run(self, run_id):
        with self.lock:
            context = self.runs.get(run_id)
            if context is None or context.session_key not in self.sessions or context.sid in self.revoked_sids:
                raise AccessError("Identity authorization is no longer available for this run.")
        subject, sid, _ = self.inspect_token(context.token)
        if (subject, sid) != (context.subject, context.sid):
            raise AccessError("Identity authorization changed for this run.")
        with self.lock:
            if context.session_key not in self.sessions or sid in self.revoked_sids:
                raise AccessError("Identity authorization ended during verification.")

    def release(self, run_id):
        with self.lock:
            self.runs.pop(run_id, None)

    @contextmanager
    def publication(self, run_id):
        # Called only after acquiring the Store transaction, matching submit's
        # DB -> identity lock order. Logout and local publication are ordered.
        with self.lock:
            context = self.runs.get(run_id)
            yield bool(context and context.session_key in self.sessions
                       and context.sid not in self.revoked_sids and context.expires > self.clock())

