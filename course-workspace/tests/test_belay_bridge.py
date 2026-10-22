"""Hermetic synthetic Belay proxy policy tests; no server/provider execution."""
import base64
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'course'))
import waypoint_belay as bridge


class BelayBridgeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        # macOS /var aliases /private/var; the production reader refuses aliases.
        self.root = Path(self.directory.name).resolve()
        self.env = dict(WORKSPACE_BELAY_LOCAL_TEST='1',
            WORKSPACE_BELAY_HOST_ORIGIN='http://127.0.0.1:5188',
            WORKSPACE_BELAY_API_ORIGIN='http://127.0.0.1:8018',
            WORKSPACE_BELAY_WORKSPACE_ORIGIN='http://127.0.0.1:5190',
            WORKSPACE_BELAY_ASSET_ROOT=str(self.root), WORKSPACE_BELAY_LEARNER_ID='edu:2')
        self.config = bridge.configuration(self.env)

    def test_disabled_default_and_explicit_synthetic_complete_binding(self):
        self.assertIsNone(bridge.configuration({}))
        self.assertIsNone(bridge.configuration({'WORKSPACE_BELAY_LOCAL_TEST':'1'}))
        for name in self.env:
            env = dict(self.env)
            del env[name]
            with self.subTest(name=name), self.assertRaises(ValueError):
                bridge.configuration(env)
        self.assertEqual(self.config.learner, 'edu:2')

    def test_hostile_configuration_origins_and_asset_aliases(self):
        for raw in ('https://127.0.0.1:5188', 'http://localhost:5188', 'http://127.0.0.2:5188',
                    'http://127.0.0.1:80', 'http://127.0.0.1:65536', 'http://127.0.0.1:5188/',
                    'http://u:p@127.0.0.1:5188', 'http://127.0.0.1:5188?token=x',
                    'http://127.0.0.1:5188#x', 'http://127.0.0.1:5188\\evil',
                    'http://127.0.0.1:5188\r\nX: y'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                bridge.loopback_origin(raw)
        alias = self.root / 'alias'; alias.symlink_to(self.root, target_is_directory=True)
        for update in ({'WORKSPACE_BELAY_ASSET_ROOT':str(alias)},
                       {'WORKSPACE_BELAY_API_ORIGIN':self.config.host},
                       {'WORKSPACE_BELAY_LEARNER_ID':'edu:someone-else'}):
            with self.subTest(update=update), self.assertRaises((ValueError, OSError)):
                bridge.configuration(dict(self.env, **update))

    def test_exact_methods_and_narrow_routes_no_saved_grade_run_or_tokens(self):
        private = '/api/classroom/' + bridge.EXERCISE + '/' + bridge.VERSION
        for method, path in [('GET', '/healthz'), ('GET','/api/curriculum'),
                ('GET','/api/conversations/config'), ('GET','/api/sol/jobs/config'),
                ('GET',private), ('POST',private), ('POST','/api/participant'),
                ('POST','/api/overlay'), ('POST','/api/sol/jobs'),
                ('GET','/api/sol/jobs/by-request/'+'a'*32),
                ('POST','/api/sol/jobs/by-request/'+'a'*32+'/reconcile'),
                ('GET','/api/sol/jobs/'+'b'*32)]:
            self.assertTrue(bridge.allowed_route(method, path), (method,path))
        for path in ('/api/run','/api/sol','/api/conversations','/api/conversations/abc',
                     '/api/conversations/abc/jobs','/api/grades','/__belay_local__/token',
                     '/api/sol/jobs/../config','/api/sol/jobs/%2e%2e/config',
                     '/api/sol/jobs//config','/api/sol/jobs/config?token=x',
                     '/api/classroom/other/'+bridge.VERSION):
            for method in ('GET','POST','PUT','DELETE'):
                self.assertFalse(bridge.allowed_route(method,path), (method,path))
        self.assertFalse(bridge.allowed_route('POST','/api/sol/jobs/'+'b'*32))

    def test_origin_and_fetch_evidence_are_both_pinned(self):
        host = {'Host':'127.0.0.1:5190'}
        self.assertTrue(bridge.same_origin(host,self.config.workspace))
        self.assertFalse(bridge.same_origin(host,self.config.workspace,True))
        for proof in ({'Origin':self.config.workspace}, {'Referer':self.config.workspace+'/lab'}):
            self.assertTrue(bridge.same_origin(dict(host,**proof),self.config.workspace,True))
        for update in ({'Host':'evil.invalid'}, {'Origin':'null'}, {'Origin':'http://127.0.0.1:5191'},
                       {'Origin':self.config.workspace,'Sec-Fetch-Site':'cross-site'},
                       {'Referer':'http://evil.invalid'}, {'Referer':'http://['}):
            self.assertFalse(bridge.same_origin(dict(host,**update),self.config.workspace,True))

    def test_allowlisted_assets_exact_bytes_symlink_fifo_and_size_rejected(self):
        (self.root/'dev-client.js').write_bytes(b'actual reviewed frontend')
        self.assertEqual(bridge.read_asset(self.config,'dev-client.js'),b'actual reviewed frontend')
        for name in ('../dev-client.js','%2e%2e/dev-client.js','/dev-client.js','__belay_local__/token','private.env'):
            with self.assertRaises(ValueError): bridge.read_asset(self.config,name)
        (self.root/'auth-client.js').symlink_to(self.root/'dev-client.js')
        with self.assertRaises(OSError): bridge.read_asset(self.config,'auth-client.js')
        os.mkfifo(self.root/'theme-client.js')
        with self.assertRaises(ValueError): bridge.read_asset(self.config,'theme-client.js')
        (self.root/'workspace.css').write_bytes(b'x'*(bridge.MAX_ASSET+1))
        with self.assertRaises(ValueError): bridge.read_asset(self.config,'workspace.css')

    def test_real_bootstrap_identity_retained_public_proxy_append_and_html_rewrite(self):
        original = b"window.BELAY_LEARNER_ID = 'edu:2';\nwindow.BELAY_CLASSROOM_DEMO = {synthetic:true};"
        actual = bridge.proxy_bootstrap(self.config,original,'/belay')
        self.assertTrue(actual.startswith(original))
        self.assertIn(b'delete window.BELAY_GET_ACCESS_TOKEN;',actual)
        self.assertIn(b'http://127.0.0.1:5190/belay/api',actual)
        self.assertIn(json.dumps({'version':1,'origin':self.config.workspace}).encode(),actual)
        self.assertNotIn(b'access_token',actual)
        for body in (b"window.BELAY_LEARNER_ID = 'edu:1';", b'no binding'):
            with self.assertRaises(ValueError): bridge.proxy_bootstrap(self.config,body,'/belay')
        for html in (b'<script src="auth-client.js"></script>',
                     b'<script src="/__belay_local__/bootstrap.js"></script><script src="auth-client.js"></script>'):
            rewritten = bridge.page_html(html,'/belay')
            self.assertEqual(rewritten.count(b'/belay/bootstrap.js'),1)
            self.assertNotIn(b'/__belay_local__/',rewritten)
        with self.assertRaises(ValueError): bridge.page_html(b'<html>unknown</html>','/belay')

    def test_broker_token_subject_and_fresh_short_lifetime_no_frontend_bearer(self):
        def body(claims):
            payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
            # Deliberately not signed: authenticity is the backend's job.
            return json.dumps({'access_token':'header.'+payload+'.signature'}).encode()
        claims = {'sub':'synthetic-two','iat':1000,'exp':1300}
        token = bridge.issued_token(self.config,200,body(claims),now=1001)
        self.assertTrue(token.startswith('header.'))
        for update in ({'sub':'synthetic-one'}, {'sub':'unknown'}, {'exp':1400},
                       {'exp':1001}, {'iat':1100}, {'iat':900}, {'exp':True}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                bridge.issued_token(self.config,200,body(dict(claims,**update)),now=1001)
        for status, raw in ((403,body(claims)),(200,b'{}'),(200,b'[]'),
                            (200,b'{"access_token":"not-a-token"}')):
            with self.assertRaises(ValueError): bridge.issued_token(self.config,status,raw,now=1001)

    def test_browser_assets_preserve_mime_despite_native_api_finish_override(self):
        class HTTPError(Exception):
            pass
        class NativeHandler:
            xsrf_token = b'native-proof'
            def set_header(self, name, value):
                if not hasattr(self, 'response_headers'): self.response_headers = {}
                self.response_headers[name] = value
            def finish(self, value): self.result = value
            def check_xsrf_cookie(self):
                if self.request.headers.get('X-XSRFToken') != 'native-proof': raise HTTPError(403)
        class APIHandler(NativeHandler):
            # Model the Jupyter APIHandler seam that caused HTML/JS responses to
            # appear as JSON even after the bridge explicitly set their MIME.
            def finish(self, value):
                self.api_finish_called = True
                self.set_header('Content-Type', 'application/json')
                super().finish(value)
            def set_status(self, value): self.status = value
        handlers = []
        app = SimpleNamespace(web_app=SimpleNamespace(settings={'base_url':'/'},
            add_handlers=lambda _host, values:handlers.extend(values)))
        modules = {
            'jupyter_server':SimpleNamespace(), 'jupyter_server.base':SimpleNamespace(),
            'jupyter_server.base.handlers':SimpleNamespace(APIHandler=APIHandler),
            'jupyter_server.utils':SimpleNamespace(url_path_join=lambda base,name:base.rstrip('/')+'/'+name),
            'tornado':SimpleNamespace(),
            'tornado.httpclient':SimpleNamespace(AsyncHTTPClient=object,HTTPRequest=object),
            'tornado.web':SimpleNamespace(authenticated=lambda function:function,HTTPError=HTTPError,RequestHandler=NativeHandler),
        }
        with patch.dict(sys.modules,modules), patch.dict(os.environ,self.env,clear=True):
            bridge._load_jupyter_server_extension(app)
        Handler = handlers[0][1]
        headers = {'Host':'127.0.0.1:5190','Referer':self.config.workspace+'/belay/',
                   'X-XSRFToken':'native-proof'}
        def handler():
            value = Handler()
            value.request = SimpleNamespace(headers=headers,query='',body=b'')
            return value
        html = b'<html><script src="auth-client.js"></script></html>'
        for name, body in (('dev-client.html',html),('dev-client.js',b'window.synthetic=true;'),
                           ('workspace.css',b'body {}')):
            (self.root/name).write_bytes(body)
        async def exercise():
            for resource, mime in (('', 'text/html; charset=utf-8'),
                    ('dev-client.js','text/javascript'),('workspace.css','text/css'),
                    ('bootstrap.js','text/javascript')):
                value = handler()
                value.upstream = AsyncMock(return_value=SimpleNamespace(code=200,
                    body=b"window.BELAY_LEARNER_ID = 'edu:2';"))
                await value.get(resource)
                self.assertEqual(value.response_headers['Content-Type'],mime)
                self.assertFalse(getattr(value,'api_finish_called',False))
                if not resource: self.assertIn(b'/belay/bootstrap.js',value.result)
                if resource == 'bootstrap.js': self.assertIn(b'BELAY_SESSION_PROXY = true',value.result)
            value = handler()
            await value.get('config')
            self.assertTrue(value.api_finish_called)
            self.assertEqual(value.response_headers['Content-Type'],'application/json')
            self.assertEqual(value.result['max_source_bytes'],8192)
            value = handler()
            value.upstream = AsyncMock(side_effect=[SimpleNamespace(code=200,body=b'{}'),
                SimpleNamespace(code=200,body=b'{"synthetic":true}',headers={'Content-Type':'application/json'})])
            with patch.object(bridge,'issued_token',return_value='server-only-token'):
                await value.get('api/api/curriculum')
            self.assertTrue(value.api_finish_called)
            self.assertEqual(value.response_headers['Content-Type'],'application/json')
            self.assertEqual(value.result,b'{"synthetic":true}')
        asyncio.run(exercise())

    def test_authenticated_native_xsrf_and_origin_gate_before_any_upstream(self):
        class HTTPError(Exception):
            def __init__(self, status, *_args): self.status_code = status
        class NativeHandler:
            def check_xsrf_cookie(self):
                if self.request.headers.get('X-XSRFToken') != 'native-proof': raise HTTPError(403)
            def finish(self, value): self.result = value
        class APIHandler(NativeHandler):
            def set_header(self, *_args): pass
            def set_status(self, value): self.status = value
            def finish(self, value): self.result = value
        def authenticated(function):
            async def wrapper(handler, *args):
                if not handler.authorized: raise HTTPError(403)
                return await function(handler, *args)
            return wrapper
        handlers = []
        clients = []
        class Client:
            response = SimpleNamespace(code=200,body=b'{}')
            def __init__(self, **limits):
                self.limits = limits; self.closed = False; clients.append(self)
            async def fetch(self, request, **options):
                self.request, self.options = request, options
                return self.response
            def close(self): self.closed = True
        app = SimpleNamespace(web_app=SimpleNamespace(settings={'base_url':'/'},
            add_handlers=lambda _host, values:handlers.extend(values)))
        modules = {
            'jupyter_server':SimpleNamespace(), 'jupyter_server.base':SimpleNamespace(),
            'jupyter_server.base.handlers':SimpleNamespace(APIHandler=APIHandler),
            'jupyter_server.utils':SimpleNamespace(url_path_join=lambda base,name:base.rstrip('/')+'/'+name),
            'tornado':SimpleNamespace(),
            'tornado.httpclient':SimpleNamespace(AsyncHTTPClient=Client,
                HTTPRequest=lambda url,**kwargs:SimpleNamespace(url=url,**kwargs)),
            'tornado.web':SimpleNamespace(authenticated=authenticated,HTTPError=HTTPError,RequestHandler=NativeHandler),
        }
        with patch.dict(sys.modules,modules), patch.dict(os.environ,self.env,clear=True):
            bridge._load_jupyter_server_extension(app)
        Handler = handlers[0][1]
        good = {'Host':'127.0.0.1:5190','Referer':self.config.workspace+'/belay/',
                'X-XSRFToken':'native-proof','Content-Type':'application/json'}
        async def exercise(method, resource, headers, *, authorized=True, query='', body=b'{}'):
            handler = Handler(); handler.authorized = authorized
            handler.request = SimpleNamespace(headers=headers,query=query,body=body)
            handler.upstream = AsyncMock()
            try:
                await getattr(handler,method)(resource)
            except HTTPError as error:
                handler.upstream.assert_not_awaited()
                return error.status_code
            raise AssertionError('hostile request unexpectedly reached upstream')
        for method in ('get','post'):
            resource = 'api/api/sol/jobs/config' if method == 'get' else 'api/api/sol/jobs'
            for headers, kwargs, expected in (
                (good,{'authorized':False},403),
                ({**good,'Origin':'https://evil.invalid'},{},403),
                ({k:v for k,v in good.items() if k!='X-XSRFToken'},{},403),
                ({**good,'X-XSRFToken':'wrong'},{},403),
                ({**good,'Authorization':'Bearer browser-supplied'},{},403),
                (good,{'query':'token=private'},404),
                (good,{'body':b'x'*(bridge.MAX_REQUEST+1)},413)):
                with self.subTest(method=method,headers=list(headers),kwargs=list(kwargs)):
                    self.assertEqual(asyncio.run(exercise(method,resource,headers,**kwargs)),expected)
        for resource in ('api/api/run','__belay_local__/token','api/api/grades','api/api/conversations/abc'):
            self.assertEqual(asyncio.run(exercise('post',resource,good)),404)

        async def allowed():
            handler = Handler(); handler.authorized = True
            handler.request = SimpleNamespace(headers=good,query='',body=b'{"synthetic":"only"}')
            upstream_token = json.dumps({'access_token':'server-token-not-for-browser'}).encode()
            handler.upstream = AsyncMock(side_effect=[SimpleNamespace(code=200,body=upstream_token),
                SimpleNamespace(code=202,body=b'{"state":"queued"}',headers={'Content-Type':'application/json'})])
            with patch.object(bridge,'issued_token',return_value='server-only-short-lived-token'):
                await handler.post('api/api/sol/jobs')
            self.assertEqual(handler.status,202)
            self.assertEqual(handler.result,b'{"state":"queued"}')
            calls = handler.upstream.await_args_list
            self.assertEqual(calls[0].args,(self.config.host,'/__belay_local__/token'))
            self.assertEqual(calls[1].args,(self.config.api,'/api/sol/jobs'))
            self.assertEqual(calls[1].kwargs['headers']['Authorization'],'Bearer server-only-short-lived-token')
            self.assertNotIn(b'server-only',handler.result)
            self.assertEqual(calls[1].kwargs['body'],handler.request.body)
        asyncio.run(allowed())
        async def bounded_wire():
            handler = Handler()
            await handler.upstream(self.config.host,'/__belay_local__/token')
            client = clients[-1]
            self.assertEqual(client.limits,{'force_instance':True,'max_body_size':bridge.MAX_RESPONSE,
                                          'max_buffer_size':bridge.MAX_RESPONSE})
            self.assertFalse(client.request.follow_redirects)
            self.assertEqual(client.request.request_timeout,15)
            self.assertEqual(client.request.connect_timeout,3)
            self.assertEqual(client.request.headers,{'Origin':self.config.host})
            self.assertTrue(client.closed)
            for code, body in ((302,b'{}'),(200,b'x'*(bridge.MAX_RESPONSE+1))):
                Client.response = SimpleNamespace(code=code,body=body)
                with self.assertRaises(HTTPError) as raised:
                    await handler.upstream(self.config.api,'/api/curriculum')
                self.assertEqual(raised.exception.status_code,502)
                self.assertTrue(clients[-1].closed)
        asyncio.run(bounded_wire())


if __name__ == '__main__':
    unittest.main()
