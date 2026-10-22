"""Opt-in, synthetic-only native Jupyter session proxy to a pinned Belay host.

The operator binds one isolated notebook server to one finite Belay identity.
This is not Hub/SSO identity federation. No learner code executes here.
"""
from dataclasses import dataclass
import base64
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from waypoint_cairn import open_root, open_beneath

MAX_REQUEST = 32768
MAX_RESPONSE = 262144
MAX_ASSET = 1048576
TIMEOUT = 15
EXERCISE = 'info511-lab3-q4-synthetic'
VERSION = EXERCISE + '-v1'
ASSETS = {
    'dev-client.html': 'text/html; charset=utf-8',
    'AI-Powered-Spark.svg': 'image/svg+xml',
    'workspace.css': 'text/css',
    **{name: 'text/javascript' for name in (
        'theme-client.js', 'auth-client.js', 'control-messages.js',
        'request-lifecycle.js', 'ask-jobs.js', 'conversation-client.js',
        'classroom-guidance.js', 'notebook-context.js', 'dev-client.js', 'vendor/marked.umd.js',
        'vendor/purify.min.js', 'vendor/highlight.min.js')},
}


def loopback_origin(value):
    if not isinstance(value, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', value):
        raise ValueError('explicit IPv4 loopback origin required')
    if not 1024 <= urlsplit(value).port <= 65535:
        raise ValueError('unprivileged loopback port required')
    return value


@dataclass(frozen=True)
class Configuration:
    host: str
    api: str
    workspace: str
    assets: Path
    learner: str


def configuration(env):
    names = ('HOST_ORIGIN', 'API_ORIGIN', 'WORKSPACE_ORIGIN', 'ASSET_ROOT', 'LEARNER_ID')
    if not any(env.get('WORKSPACE_BELAY_' + name) for name in names):
        return None
    if env.get('WORKSPACE_BELAY_LOCAL_TEST') != '1':
        raise ValueError('Belay notebook proxy requires explicit synthetic local opt-in')
    host, api, workspace = [loopback_origin(env.get('WORKSPACE_BELAY_' + key))
                            for key in names[:3]]
    if len({host, api, workspace}) != 3:
        raise ValueError('separate notebook, frontend and API origins required')
    assets = Path(env.get('WORKSPACE_BELAY_ASSET_ROOT', ''))
    if not assets.is_absolute() or '..' in assets.parts:
        raise ValueError('absolute operator asset root required')
    fd = open_root(assets)
    os.close(fd)
    learner = env.get('WORKSPACE_BELAY_LEARNER_ID', '')
    if learner not in ('edu:1', 'edu:2'):
        raise ValueError('explicit synthetic learner binding required')
    return Configuration(host, api, workspace, assets, learner)


def allowed_route(method, path):
    if not isinstance(path, str) or len(path) > 256 or not re.fullmatch(r'/[A-Za-z0-9_/-]+', path):
        return False
    private = '/api/classroom/' + EXERCISE + '/' + VERSION
    if method == 'GET' and path in {'/healthz', '/api/curriculum', '/api/conversations/config', '/api/sol/jobs/config', private}:
        return True
    if method == 'POST' and path in {'/api/participant', '/api/overlay', '/api/sol/jobs', private}:
        return True
    request = r'/api/sol/jobs/by-request/[A-Za-z0-9_-]{16,64}'
    if method == 'GET' and re.fullmatch(request, path):
        return True
    if method == 'POST' and re.fullmatch(request + '/reconcile', path):
        return True
    return method == 'GET' and bool(re.fullmatch(r'/api/sol/jobs/[A-Za-z0-9_-]{16,64}', path)) and path != '/api/sol/jobs/conversations'


def same_origin(headers, expected, require_evidence=False):
    if headers.get('Host') != urlsplit(expected).netloc:
        return False
    if headers.get('Sec-Fetch-Site', 'none') not in ('none', 'same-origin'):
        return False
    origin = headers.get('Origin')
    if origin is not None:
        return origin == expected
    referer = headers.get('Referer')
    if referer:
        try:
            parsed = urlsplit(referer)
            return not parsed.username and not parsed.password and parsed.scheme + '://' + parsed.netloc == expected
        except ValueError:
            return False
    return not require_evidence


def read_asset(config, name):
    if name not in ASSETS:
        raise ValueError('asset unavailable')
    root = open_root(config.assets)
    try:
        fd = open_beneath(root, name.split('/'))
        try:
            import stat
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError('regular asset required')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                body = stream.read(MAX_ASSET + 1)
            if len(body) > MAX_ASSET:
                raise ValueError('asset too large')
            return body
        finally:
            os.close(fd)
    finally:
        os.close(root)


def proxy_bootstrap(config, upstream, base):
    text = upstream.decode('utf-8')
    identity = re.search(r"window\.BELAY_LEARNER_ID\s*=\s*(['\"])(edu:[12])\1\s*;", text)
    if not identity or identity.group(2) != config.learner:
        raise ValueError('Belay host identity does not match notebook binding')
    backend = config.workspace + base + '/api'
    public = '\nwindow.BELAY_SESSION_PROXY = true;\ndelete window.BELAY_GET_ACCESS_TOKEN;\n'
    public += 'window.SOL_BACKEND_URL = ' + json.dumps(backend) + ';\n'
    public += 'window.BELAY_AUTH_ORIGIN = ' + json.dumps(config.workspace) + ';\n'
    public += 'window.BELAY_NOTEBOOK_CONTEXT = ' + json.dumps({'version': 1, 'origin': config.workspace}) + ';\n'
    return upstream + public.encode()


def page_html(body, base):
    text = body.decode('utf-8')
    original = '<script src="/__belay_local__/bootstrap.js"></script>'
    replacement = '<script src="' + base + '/bootstrap.js"></script>'
    if original in text:
        text = text.replace(original, replacement)
    else:
        marker = '<script src="auth-client.js"></script>'
        if text.count(marker) != 1:
            raise ValueError('unsupported Belay frontend bootstrap')
        text = text.replace(marker, replacement + '\n' + marker)
    return text.encode()


def issued_token(config, response_code, body, now=None):
    """Check the pinned broker's identity/short lifetime, not JWT authenticity.

    The ordinary Belay API remains the signature, issuer, membership and finite
    runtime authority verifier. This rejects an operator cross-learner misbinding
    before forwarding a token even if a caller bypasses the public bootstrap.
    """
    value = json.loads(body)
    if response_code != 200 or not isinstance(value, dict) or set(value) != {'access_token'}:
        raise ValueError('credential unavailable')
    token = value['access_token']
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token) or not 32 <= len(token) <= 16384:
        raise ValueError('unsupported credential')
    encoded = token.split('.')[1]
    claims = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
    instant = time.time() if now is None else now
    subject = {'edu:1':'synthetic-one', 'edu:2':'synthetic-two'}[config.learner]
    if (not isinstance(claims, dict) or claims.get('sub') != subject
            or type(claims.get('iat')) is not int or type(claims.get('exp')) is not int
            or not instant - 30 <= claims['iat'] <= instant + 5
            or not instant < claims['exp'] <= claims['iat'] + 300):
        raise ValueError('credential subject or short lifetime mismatch')
    return token


def _jupyter_server_extension_points():
    return [{'module': 'waypoint_belay'}]


def _load_jupyter_server_extension(serverapp):
    from jupyter_server.base.handlers import APIHandler
    from jupyter_server.utils import url_path_join
    from tornado.httpclient import AsyncHTTPClient, HTTPRequest
    from tornado.web import authenticated, HTTPError, RequestHandler

    config = configuration(os.environ)
    if config is None:
        return
    base = url_path_join(serverapp.web_app.settings['base_url'], 'belay')

    class BelayHandler(APIHandler):
        def set_default_headers(self):
            super().set_default_headers()
            self.set_header('Cache-Control', 'no-store')
            self.set_header('Referrer-Policy', 'same-origin')
            self.set_header('X-Content-Type-Options', 'nosniff')
            self.set_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

        async def upstream(self, origin, path, headers=None, body=None, method='GET'):
            client = AsyncHTTPClient(force_instance=True, max_body_size=MAX_RESPONSE, max_buffer_size=MAX_RESPONSE)
            try:
                response = await client.fetch(HTTPRequest(origin + path, method=method,
                    headers={'Origin': config.host, **(headers or {})}, body=body,
                    follow_redirects=False, connect_timeout=3, request_timeout=TIMEOUT), raise_error=False)
                if len(response.body) > MAX_RESPONSE or 300 <= response.code < 400:
                    raise HTTPError(502)
                return response
            except HTTPError:
                raise
            except Exception:
                raise HTTPError(502, 'Belay service unavailable') from None
            finally:
                client.close()

        def local(self, api=False):
            if not same_origin(self.request.headers, config.workspace, require_evidence=api):
                raise HTTPError(403)
            if api:
                # Force the native cookie/header proof even if another Jupyter
                # authenticator accepts a token. Browser bearer auth is forbidden.
                if self.request.headers.get('Authorization'):
                    raise HTTPError(403)
                RequestHandler.check_xsrf_cookie(self)

        @authenticated
        async def get(self, resource=''):
            self.local()
            if resource.startswith('api/'):
                return await self.proxy('GET', '/' + resource[4:])
            if self.request.query and (resource not in ASSETS or not re.fullmatch(r'v=[A-Za-z0-9_-]{1,64}', self.request.query)):
                raise HTTPError(400)
            if resource == 'config':
                self.local(api=True)
                return self.finish({'version': 1, 'base': base, 'max_source_bytes': 8192})
            if resource == 'bootstrap.js':
                response = await self.upstream(config.host, '/__belay_local__/bootstrap.js')
                if response.code != 200:
                    raise HTTPError(502)
                try:
                    body = proxy_bootstrap(config, response.body, base)
                except (ValueError, UnicodeError):
                    raise HTTPError(502, 'Belay configuration unavailable') from None
                self.set_header('Content-Type', 'text/javascript')
                # APIHandler.finish forces application/json. These are browser
                # assets; preserve their explicit MIME type through Tornado.
                return RequestHandler.finish(self, body)
            name = 'dev-client.html' if resource == '' else resource
            try:
                body = read_asset(config, name)
                if name == 'dev-client.html':
                    body = page_html(body, base)
                    self.xsrf_token
            except (ValueError, OSError, UnicodeError):
                raise HTTPError(404) from None
            self.set_header('Content-Type', ASSETS[name])
            RequestHandler.finish(self, body)

        @authenticated
        async def post(self, resource=''):
            if not resource.startswith('api/'):
                raise HTTPError(404)
            await self.proxy('POST', '/' + resource[4:])

        async def proxy(self, method, path):
            self.local(api=True)
            if self.request.query or not allowed_route(method, path):
                raise HTTPError(404)
            if len(self.request.body) > MAX_REQUEST:
                raise HTTPError(413)
            if method == 'POST' and self.request.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise HTTPError(415)
            credential = await self.upstream(config.host, '/__belay_local__/token')
            try:
                token = issued_token(config, credential.code, credential.body)
            except (ValueError, KeyError, TypeError):
                raise HTTPError(502, 'Belay authorization unavailable') from None
            response = await self.upstream(config.api, path,
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                         'X-Belay-Institution': 'synthetic-inst', 'X-Belay-Class': 'synthetic-class'},
                body=self.request.body if method == 'POST' else None, method=method)
            if response.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise HTTPError(502)
            self.set_status(response.code)
            self.set_header('Content-Type', 'application/json')
            if re.fullmatch(r'[0-9]{1,3}', response.headers.get('Retry-After', '')):
                self.set_header('Retry-After', response.headers['Retry-After'])
            self.finish(response.body)

    serverapp.web_app.add_handlers('.*$', [(base + r'/(.*)', BelayHandler)])
