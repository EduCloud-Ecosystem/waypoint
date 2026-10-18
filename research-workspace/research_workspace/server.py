"""Authenticated loopback-only HTTP surface for the research pilot."""

import fcntl
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit, urlencode

from .evidence import build_bundle
from .runtime import Broker, Worker, strict_json
from .store import AccessError, Store
from .identity import Identity, IdentityUnavailable

STATIC = Path(__file__).resolve().parent.parent / "static"
WARNING = "Descriptive results from a demonstration recipe. A successful run is not scientific validation or evidence of an intervention effect."


class Application:
    def __init__(self, state):
        self.store = Store(state)
        self.config = strict_json((self.store.root / "config.json").read_bytes())
        mode = self.config.get("mode")
        if mode not in {"local-public-data", "local-synthetic-oidc"}:
            raise ValueError("This build requires an active local pilot mode.")
        self.identity = Identity(self.config.get("identity"), self.store) if mode == "local-synthetic-oidc" else None
        self.broker = Broker(self.config["broker"], self.store.root / "broker")
        self.worker = Worker(self.store, self.broker, self.identity)

    def recipes(self):
        from .catalog import list_environments
        available = {entry["recipe"] for entry in list_environments(self.config["images"]) if entry["available"]}
        return {"recipes": [
            {"id": "summary-python", "name": "Python · group comparison", "description": "Compare all observations with a trimmed descriptive summary.", "available": "summary-python" in available},
            {"id": "summary-r", "name": "R · group comparison", "description": "Run the same reviewed method in R for a runtime comparison.", "available": "summary-r" in available},
        ]}

    def dispatch(self, method, path, subject, data, request_key, context=None):
        if path == "/api/session" and method == "GET":
            return 200, {"subject": subject}
        if path == "/api/recipes" and method == "GET":
            return 200, self.recipes()
        if path == "/api/environments" and method == "GET":
            from .catalog import list_environments
            return 200, {"environments": list_environments(self.config["images"])}
        if path == "/api/projects":
            if method == "GET":
                return 200, {"projects": self.store.projects(subject)}
            if method == "POST":
                return 201, self.store.create_project(subject, data, self.broker, self.config["total_budget_cpu_seconds"])
        match = re.fullmatch(r"/api/projects/([a-f0-9]{32})(.*)", path)
        if not match:
            return 404, {"error": "That page or action is unavailable."}
        project_id, action = match.groups()
        if action == "" and method == "GET":
            response = self.store.project(project_id, subject)
            try:
                response["usage"] = self.broker.usage(project_id)
            except ValueError:
                response["usage"] = {"status": "unavailable", "message": "Compute accounting cannot currently be verified."}
            return 200, response
        if action == "/members" and method == "POST":
            if data.get("role") not in ("editor", "viewer"):
                raise ValueError("Choose editor or viewer for the collaborator.")
            self.store.membership(project_id, subject, data.get("subject"), data.get("role"))
            return 200, {"ok": True}
        if action.startswith("/members/") and method == "DELETE":
            self.store.membership(project_id, subject, action.removeprefix("/members/"))
            return 200, {"ok": True}
        if action == "/close" and method == "POST":
            return 200, self.store.close_project(project_id, subject)
        if action == "/runs" and method == "POST":
            if self.identity and (context is None or context.subject != subject):
                raise AccessError("Current identity is required for submission.")
            bound = []
            def authorize(rid):
                self.identity.bind(rid, context)
                bound.append(rid)
            try:
                return 202, self.store.submit(project_id, subject, data, self.config["images"], request_key, authorize if self.identity else None)
            except Exception:
                for rid in bound:
                    self.identity.release(rid)
                raise
        run_match = re.fullmatch(r"/runs/([a-f0-9]{32})(/cancel)?", action)
        if run_match:
            run_id, cancel = run_match.groups()
            if cancel and method == "POST":
                return 200, self.store.cancel(project_id, run_id, subject)
            if not cancel and method == "GET":
                return 200, self.store.run(project_id, run_id, subject)
        if action == "/comparison" and method == "GET":
            response = self.store.project(project_id, subject)
            return 200, {"runs": response["runs"], "warning": WARNING}
        if action == "/export" and method == "GET":
            project, runs, files = self.store.snapshot(project_id, subject)
            return 200, build_bundle(project, runs, files)
        return 404, {"error": "That page or action is unavailable."}


class Handler(BaseHTTPRequestHandler):
    server_version = "EduCloudResearch/0.1"
    sys_version = ""

    def log_message(self, *_):
        # URLs, IDs, credentials and user-provided values are not request logs.
        pass

    def respond(self, status, value, *, headers=None, content_type=None):
        if isinstance(value, bytes):
            payload = value
            content_type = content_type or "application/zip"
        else:
            payload = json.dumps(value, allow_nan=False).encode()
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        for key, value in (headers.items() if isinstance(headers, dict) else (headers or [])):
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def session_token(self, name="research_session"):
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            return jar[name].value if name in jar else ""
        except cookies.CookieError:
            return ""

    def handle_request(self):
        identity = self.server.app.identity
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if identity:
            allowed_hosts = {urlsplit(identity.config["public_origin"]).netloc}
            if not identity.boundary(self.headers):
                self.respond(403, {"error": "Use the configured workspace entrance."})
                return
        host = self.headers.get("Host", "")
        if host not in allowed_hosts:
            self.respond(403, {"error": "This local pilot requires its loopback origin."})
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            self.respond(403, {"error": "Cross-origin requests are not allowed."})
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/api/auth-config" and self.command == "GET":
            config = {"mode": "key"}
            if identity:
                config = {"mode": "oidc", "sign_in": "/oauth2/start?rd=%2F%3Fsignin%3D1",
                          "sign_out": identity.config["issuer"] + "/protocol/openid-connect/logout?" +
                          urlencode({"client_id": identity.config["client_id"], "post_logout_redirect_uri": identity.config["public_origin"] + "/"})}
            self.respond(200, config)
            return
        if self.command not in ("GET", "POST", "DELETE"):
            self.respond(405, {"error": "This method is not supported."})
            return
        data = {}
        if self.command != "GET":
            if self.headers.get("X-Research-Request") != "1":
                self.respond(403, {"error": "A same-origin request header is required."})
                return
            if self.headers.get("Transfer-Encoding"):
                self.respond(400, {"error": "Streaming request bodies are not accepted."})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 <= size <= 400000:
                    raise ValueError()
                if size:
                    if self.headers.get_content_type() != "application/json":
                        raise ValueError()
                    data = strict_json(self.rfile.read(size))
                    if not isinstance(data, dict):
                        raise ValueError()
            except (ValueError, UnicodeError):
                self.respond(400, {"error": "Use a bounded JSON object for this request."})
                return
        if path == "/api/session" and self.command == "POST":
            try:
                if identity:
                    if data:
                        raise AccessError("Use the identity-provider sign-in for this workspace.")
                    subject, token = identity.begin(self.headers.get("Cookie", ""))
                else:
                    subject, token = self.server.app.store.session(key=data.get("access_key"))
                name = identity.config["cookie_name"] if identity else "research_session"
                self.respond(200, {"subject": subject}, headers={"Set-Cookie": f"{name}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800"})
            except AccessError as exc:
                self.respond(401, {"error": str(exc)})
            except IdentityUnavailable as exc:
                self.respond(503, {"error": str(exc)})
            return
        if not path.startswith("/api/"):
            if self.command != "GET":
                self.respond(405, {"error": "This method is not supported."})
                return
            relative = "index.html" if path == "/" else path.lstrip("/")
            source = (STATIC / relative).resolve()
            if not source.is_relative_to(STATIC.resolve()) or not source.is_file() or source.name.startswith("."):
                self.respond(404, {"error": "That page is unavailable."})
                return
            self.respond(200, source.read_bytes(), content_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream")
            return
        token = self.session_token(identity.config["cookie_name"] if identity else "research_session")
        if identity and path == "/api/session" and self.command == "DELETE":
            # Local invalidation is effective even if the IdP is unavailable.
            identity.logout(token)
            names = [identity.config["cookie_name"], identity.config["proxy_cookie_name"]]
            names += [identity.config["proxy_cookie_name"] + "_" + str(i) for i in range(8)]
            self.respond(200, {"ok": True, "identity_provider_session_ended": False},
                         headers=[("Set-Cookie", f"{name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0") for name in names])
            return
        try:
            context = identity.authenticate(token, self.headers.get("Cookie", "")) if identity else None
            subject = context.subject if identity else self.server.app.store.session(token=token)
        except AccessError as exc:
            self.respond(401, {"error": str(exc)})
            return
        except IdentityUnavailable as exc:
            self.respond(503, {"error": str(exc)})
            return
        try:
            if path == "/api/session" and self.command == "DELETE":
                self.server.app.store.logout(token)
                self.respond(200, {"ok": True}, headers={"Set-Cookie": "research_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                return
            status, value = self.server.app.dispatch(self.command, path, subject, data, self.headers.get("Idempotency-Key"), context)
            headers = {"Content-Disposition": 'attachment; filename="research-evidence.zip"'} if isinstance(value, bytes) else None
            self.respond(status, value, headers=headers)
        except AccessError as exc:
            self.respond(403, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self.respond(400, {"error": str(exc)[:300]})
        except Exception:
            self.respond(503, {"error": "The operation could not be verified. Existing records were retained; ask the operator."})

    do_GET = handle_request
    do_POST = handle_request
    do_DELETE = handle_request
    do_OPTIONS = handle_request


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def serve(state, host="127.0.0.1", port=18765):
    with (Path(state) / "server.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("This workspace already has a running service.") from exc
        app = Application(state)
        if host != "127.0.0.1" and not (app.identity and host == "0.0.0.0"):
            raise ValueError("Only the isolated OIDC backend may bind its private container network.")
        server = Server((host, port), Handler)
        server.app = app
        app.worker.start()
        print(f"Research workspace: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            app.worker.stop()
            server.server_close()
