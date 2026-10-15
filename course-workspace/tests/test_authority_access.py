"""Real local TLS unit evidence; no hosted/provider acceptance or retained keys."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock,patch
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from authority_test_pki import TestPKI
import authority_access as access
from hub.authority_client import AuthorityClient,AuthorityError,VERSION,PATH,encoded


class AuthorityAccessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory();cls.base=Path(cls.temporary.name).resolve();cls.pki=TestPKI(cls.base/'pki')
        cls.credentials={role:cls.pki.issue(role) for role in ('hub','operator','guardian','rotated','unknown')}
        cls.foreign=TestPKI(cls.base/'foreign');cls.foreign_client=cls.foreign.issue('untrusted')

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve()
        self.authority_id=uuid.uuid4().hex;self.acl=self.root/'acl.json';self.entries={}
        for role in ('hub','operator','guardian'):
            self.entries[self.pki.fingerprint(self.credentials[role][0])]={'role':role,'workers':['one','two'],'courses':['course']}
        self.write_acl();self.store=access.ReplayStore.create(self.root/'replay',self.authority_id);self.calls=[];self.effect=None
        def dispatch(principal,op,course,payload):
            self.calls.append((principal['role'],op,course,deepcopy(payload)))
            if self.effect:self.effect(principal,op,course,payload)
            principal['authorize']()
            return {'observed':True}
        self.dispatch=dispatch;self.servers=[];self.server=self.start_server(self.store);self.addCleanup(self.close_servers)

    def write_acl(self):
        self.acl.write_bytes(encoded({'version':VERSION,'authority_id':self.authority_id,'clients':self.entries}));self.acl.chmod(0o600)

    def start_server(self,store,port=0):
        server=access.AuthorityServer(('127.0.0.1',port),authority_id=self.authority_id,certfile=self.pki.server_cert,keyfile=self.pki.server_key,
            ca_file=self.pki.ca_cert,acl_file=self.acl,replay_store=store,dispatch=self.dispatch,timeout=.5,max_threads=2)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True);thread.start();self.servers.append((server,thread));return server

    def close_servers(self):
        for server,thread in self.servers:server.shutdown();server.server_close();thread.join(2)

    def client(self,role='hub',worker='one',course='course',authority=None,host='localhost',ca=None,credentials=None):
        cert,key=credentials or self.credentials[role]
        return AuthorityClient('https://'+host+':'+str(self.server.server_port),authority or self.authority_id,ca or self.pki.ca_cert,cert,key,worker,course,timeout=.5)

    def frame(self,operation='hub.authorize',payload=None):
        return {'version':VERSION,'authority_id':self.authority_id,'nonce':uuid.uuid4().hex,'issued_at':int(time.time()),
                'operation':operation,'worker_id':'one','course_id':'course','payload':payload or {}}

    def raw(self,frame=None,body=None,role='hub',path=PATH,headers=None):
        cert,key=self.credentials[role];context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(cafile=self.pki.ca_cert);context.load_cert_chain(cert,key)
        connection=http.client.HTTPSConnection('localhost',self.server.server_port,context=context,timeout=1)
        try:
            raw=body if body is not None else encoded(frame or self.frame())
            connection.request('POST',path,body=raw,headers=headers or {'Content-Type':'application/json','Content-Length':str(len(raw))})
            response=connection.getresponse();return response.status,json.loads(response.read())
        finally:connection.close()

    def refused(self,code,call):
        with self.assertRaises(AuthorityError) as caught:call()
        self.assertEqual(caught.exception.code,code)

    def test_real_mutual_tls_roles_scopes_and_fresh_no_cached_calls(self):
        client=self.client();self.assertEqual(client.call('hub.authorize',{}),{'observed':True});client.call('hub.authorize',{})
        self.client('operator').call('ownership.preview',{'receiver_worker_id':'two'})
        self.client('operator').call('ownership.apply',{'plan_id':'a'*32,'plan_sha256':'b'*64})
        self.client('guardian').call('guardian.observe',{'guardian_container_id':'a'*64,'start_identity':'b'*64})
        self.assertEqual(len(self.calls),5)
        self.refused('authority_access_refused',lambda:self.client().call('ownership.inventory',{}))
        self.refused('authority_access_refused',lambda:self.client('guardian').call('hub.authorize',{}))
        self.refused('authority_access_refused',lambda:self.client(worker='other').call('hub.authorize',{}))
        self.refused('authority_access_refused',lambda:self.client(course='other').call('hub.authorize',{}))
        self.assertEqual(len(self.calls),5)

    def test_unknown_revoked_and_rotated_certificate_acl_are_checked_each_request(self):
        client=self.client();client.call('hub.authorize',{})
        self.refused('authority_access_refused',lambda:self.client('unknown').call('hub.authorize',{}))
        del self.entries[self.pki.fingerprint(self.credentials['hub'][0])];self.write_acl()
        self.refused('authority_access_refused',lambda:client.call('hub.authorize',{}))
        self.entries[self.pki.fingerprint(self.credentials['rotated'][0])]={'role':'hub','workers':['one'],'courses':['course']};self.write_acl()
        self.client('rotated').call('hub.authorize',{});self.assertEqual(len(self.calls),2)

    def test_real_tls_missing_untrusted_wrong_hostname_ca_and_incarnation_refuse(self):
        self.refused('authority_transport_unavailable',lambda:self.client(credentials=self.foreign_client).call('hub.authorize',{}))
        self.refused('authority_transport_unavailable',lambda:self.client(ca=self.foreign.ca_cert).call('hub.authorize',{}))
        self.refused('authority_response_invalid',lambda:self.client(authority='f'*32).call('hub.authorize',{}))
        # Resolves locally, but this name is absent from the certificate SAN.
        with patch('socket.getaddrinfo',return_value=socket.getaddrinfo('127.0.0.1',self.server.server_port)):
            self.refused('authority_transport_unavailable',lambda:self.client(host='wrong.test').call('hub.authorize',{}))
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(cafile=self.pki.ca_cert)
        connection=http.client.HTTPSConnection('localhost',self.server.server_port,context=context,timeout=.5)
        try:
            with self.assertRaises((OSError,ssl.SSLError,http.client.HTTPException)):
                connection.request('POST',PATH,body=encoded(self.frame()),headers={'Content-Type':'application/json'});connection.getresponse()
        finally:connection.close()
        self.assertEqual(self.calls,[])

    def test_fresh_authorize_callback_revocation_before_effect_is_not_a_cached_grant(self):
        def revoke(principal,*args):
            del self.entries[principal['cert_sha256']];self.write_acl();principal['authorize']()
            self.fail('revoked request reached effect')
        self.effect=revoke
        self.refused('authority_operation_refused',lambda:self.client().call('hub.authorize',{}))
        self.assertEqual(len(self.calls),1)

    def test_replay_same_or_modified_body_is_durable_across_restart_and_certificate_rotation(self):
        frame=self.frame();self.assertEqual(self.raw(frame)[0],200)
        changed=deepcopy(frame);changed['operation']='hub.spawn';changed['payload']={'subject':'learner','environment':{},'expected_epoch':1}
        self.assertEqual(self.raw(changed)[0],409)
        server,thread=self.servers.pop();server.shutdown();server.server_close();thread.join(2)
        reopened=access.ReplayStore.open(self.store.directory,self.authority_id);self.server=self.start_server(reopened)
        self.assertEqual(self.raw(frame)[0],409)
        self.entries[self.pki.fingerprint(self.credentials['rotated'][0])]={'role':'hub','workers':['one'],'courses':['course']};self.write_acl()
        self.assertEqual(self.raw(frame,role='rotated')[0],409);self.assertEqual(len(self.calls),1)

    def test_same_port_real_tls_restart_preserves_replay_after_server_active_close(self):
        frame=self.frame();body=encoded(frame);port=self.server.server_port
        context=self.client().context
        # Read the server's EOF before closing the client: the server actively
        # closes this real TLS connection and may retain TCP TIME_WAIT state.
        with context.wrap_socket(socket.create_connection(('127.0.0.1',port),timeout=1),server_hostname='localhost') as connection:
            connection.sendall(('POST '+PATH+' HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body))+'\r\nConnection: close\r\n\r\n').encode()+body)
            chunks=[]
            while True:
                chunk=connection.recv(65536)
                if not chunk:break
                chunks.append(chunk)
            self.assertTrue(b''.join(chunks).startswith(b'HTTP/1.1 200 '))
        # A failed incoming TLS handshake also actively closes a consumed TCP
        # stream. This leaves TIME_WAIT on Linux even when the successful TLS
        # client's SSL teardown above happened to reset its connection.
        with socket.create_connection(('127.0.0.1',port),timeout=1) as connection:
            connection.sendall(b'GET /')
            self.assertEqual(connection.recv(1024),b'')
        server,thread=self.servers.pop();server.shutdown();server.server_close();thread.join(2)
        self.assertFalse(thread.is_alive())
        reopened=access.ReplayStore.open(self.store.directory,self.authority_id)
        self.server=self.start_server(reopened,port=port)
        self.assertEqual(self.server.server_port,port)
        self.assertEqual(self.raw(frame)[0],409)
        self.assertEqual(self.client().call('hub.authorize',{}),{'observed':True})
        self.assertEqual(len(self.calls),2)

    def test_same_port_live_second_tls_listener_is_still_refused(self):
        self.refused('authority_access_refused',lambda:self.start_server(self.store,port=self.server.server_port))
        self.assertEqual(self.client().call('hub.authorize',{}),{'observed':True})

    def test_nonce_consumed_before_failed_callback_no_logs_or_secret_replay_body(self):
        def private_error(*args):raise RuntimeError('DO_NOT_LOG_PRIVATE_TOKEN')
        self.effect=private_error;frame=self.frame();captured=io.StringIO()
        with redirect_stdout(captured),redirect_stderr(captured):status,value=self.raw(frame)
        self.assertEqual(status,503);self.assertEqual(value['error'],'authority_operation_refused');self.assertNotIn('PRIVATE_TOKEN',encoded(value).decode());self.assertEqual(captured.getvalue(),'')
        self.assertEqual(self.raw(frame)[0],409)
        with self.store._connection() as conn:
            record=dict(conn.execute('SELECT * FROM requests').fetchone());self.assertEqual(record['request_sha256'],hashlib.sha256(encoded(frame)).hexdigest())
            self.assertNotIn('payload',record)

    def test_bad_frames_duplicate_json_timestamps_paths_and_operator_paths_refuse_before_callback(self):
        frame=self.frame();cases=[]
        for key,value in [('issued_at',True),('issued_at',int(time.time())-31),('issued_at',int(time.time())+10),('nonce','invalid'),('payload',[]),('operation','unknown')]:
            changed=deepcopy(frame);changed[key]=value;cases.append(encoded(changed))
        cases.extend([encoded(frame)[:-1]+b',"nonce":"'+frame['nonce'].encode()+b'"}',b'{"payload":NaN}',b'{'])
        for body in cases:
            with self.subTest(body=body):self.assertEqual(self.raw(body=body)[0],400)
        self.assertEqual(self.raw(frame,path='/other')[0],400)
        self.refused('authority_frame_invalid',lambda:self.client('operator').call('ownership.apply',{'path':'/private/course'}))
        self.refused('authority_access_refused',lambda:self.client('operator').call('ownership.preview',{'receiver_worker_id':'unscoped'}))
        self.assertEqual(self.calls,[])

    def test_ambiguous_lengths_chunking_oversized_body_and_incomplete_tls_are_bounded(self):
        cert,key=self.credentials['hub'];context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT);context.load_verify_locations(cafile=self.pki.ca_cert);context.load_cert_chain(cert,key)
        for headers in ('Content-Length: 2\r\nContent-Length: 2','Content-Length: 2\r\nTransfer-Encoding: chunked','Content-Length: 65537'):
            with socket.create_connection(('localhost',self.server.server_port),timeout=1) as raw,context.wrap_socket(raw,server_hostname='localhost') as tls:
                tls.sendall(('POST '+PATH+' HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n'+headers+'\r\n\r\n{}').encode());self.assertIn(b'400',tls.recv(4096).split(b'\r\n')[0])
        with socket.create_connection(('localhost',self.server.server_port),timeout=1) as raw:
            deadline=time.monotonic()+1.5
            while self.server.slots.acquire(blocking=False):self.server.slots.release();break
            raw.sendall(b'not TLS')
            raw.settimeout(1)
            try:self.assertEqual(raw.recv(1),b'')
            except ConnectionResetError:pass
            self.assertLess(time.monotonic(),deadline)
        self.assertEqual(self.calls,[])

    def test_replay_missing_replaced_or_modified_history_fails_closed(self):
        self.client().call('hub.authorize',{})
        with self.store._connection() as conn:conn.execute('DELETE FROM requests')
        self.refused('authority_replay_state_unavailable',lambda:self.client().call('hub.authorize',{}))
        with self.assertRaises(AuthorityError):access.ReplayStore.open(self.store.directory,self.authority_id)
        self.store.sentinel.unlink()
        with self.assertRaises(AuthorityError):access.ReplayStore.open(self.store.directory,self.authority_id)
        self.assertEqual(len(self.calls),1)

    def test_outage_and_ssl_keylog_environment_never_return_cached_success_or_secret_artifact(self):
        keylog=self.root/'unwanted-secrets.log'
        with patch.dict(os.environ,{'SSLKEYLOGFILE':str(keylog)}):
            client=self.client();client.call('hub.authorize',{})
            self.assertIsNone(client.context.keylog_filename);self.assertIsNone(self.server.tls.keylog_filename);self.assertFalse(keylog.exists())
        server,thread=self.servers.pop();server.shutdown();server.server_close();thread.join(2)
        self.refused('authority_transport_unavailable',lambda:client.call('hub.authorize',{}));self.assertEqual(len(self.calls),1)

    def test_dispatch_is_outside_replay_sql_transaction_and_bound_to_trusted_principal(self):
        def inspect(principal,op,course,payload):
            with self.store._connection() as conn:conn.execute('BEGIN IMMEDIATE');conn.execute('COMMIT')
            self.assertEqual(principal['role'],'hub');self.assertEqual(principal['worker_id'],'one');self.assertEqual(principal['course_id'],course)
        self.effect=inspect;self.client().call('hub.authorize',{})

    def test_concurrent_identical_request_has_one_durable_callback_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        frame=self.frame();ready=threading.Barrier(2)
        def execute():ready.wait();return self.raw(frame)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:execute(),range(2)))
        self.assertEqual(sorted(results),[200,409]);self.assertEqual(len(self.calls),1)

    def test_nonce_quota_and_clock_rollback_refuse_without_recreation_or_effect(self):
        self.client().call('hub.authorize',{})
        with patch.object(access,'MAX_NONCES',1):self.refused('authority_replay_state_unavailable',lambda:self.client().call('hub.authorize',{}))
        earlier=int(time.time())-100
        with patch.object(access.time,'time',return_value=earlier):
            self.refused('authority_replay_state_unavailable',lambda:self.client().call('hub.authorize',{}))
        self.assertEqual(len(self.calls),1);self.client().call('hub.authorize',{});self.assertEqual(len(self.calls),2)

    def test_client_refuses_redirect_ambiguous_length_and_unbound_response_without_retry(self):
        original=access.AuthorityHandler.reply
        def hostile(mode):
            def reply(handler,status,error=None,frame=None,result=None):
                if error is not None:return original(handler,status,error,frame,result)
                value={'version':VERSION,'authority_id':self.authority_id,'nonce':frame['nonce'],'operation':frame['operation'],
                    'worker_id':frame['worker_id'],'course_id':frame['course_id'],'ok':True,'result':result,'error':None}
                if mode=='nonce':value['nonce']='f'*32
                if mode=='version':value['version']='wrong'
                if mode=='scope':value['course_id']='other'
                raw=encoded(value);handler.send_response(307 if mode=='redirect' else 200)
                handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(raw)))
                if mode=='length':handler.send_header('Content-Length',str(len(raw)))
                if mode=='redirect':handler.send_header('Location','https://localhost:'+str(self.server.server_port)+PATH)
                handler.send_header('Connection','close');handler.end_headers();handler.wfile.write(raw);handler.close_connection=True
            return reply
        for mode in ('nonce','version','scope','length','redirect'):
            before=len(self.calls)
            with self.subTest(mode=mode),patch.object(access.AuthorityHandler,'reply',hostile(mode)):
                self.refused('authority_response_invalid',lambda:self.client().call('hub.authorize',{}))
            self.assertEqual(len(self.calls),before+1)

    def test_live_acl_role_scope_changes_and_malformed_private_policy_close_access(self):
        client=self.client();fingerprint=self.pki.fingerprint(self.credentials['hub'][0]);original=deepcopy(self.entries[fingerprint])
        for entry in ({'role':'guardian','workers':['one'],'courses':['course']},{'role':'hub','workers':['two'],'courses':['course']},
                      {'role':'hub','workers':['one'],'courses':['other']},{'role':'hub','workers':['one','one'],'courses':['course']}):
            self.entries[fingerprint]=entry;self.write_acl();self.refused('authority_access_refused',lambda:client.call('hub.authorize',{}))
        self.entries[fingerprint]=original;self.write_acl();self.acl.chmod(0o644)
        self.refused('authority_access_refused',lambda:client.call('hub.authorize',{}));self.acl.chmod(0o600)
        policy=access.AccessPolicy(self.acl,self.authority_id);policy.authorize_fingerprint(fingerprint,'hub','one','course')
        self.refused('authority_access_refused',lambda:policy.authorize_fingerprint(fingerprint,'guardian','one','course'))
        self.assertEqual(self.calls,[])

    def test_replay_database_replacement_pin_and_sentinel_loss_do_not_bootstrap(self):
        self.client().call('hub.authorize',{});replacement=self.root/'replacement.sqlite';shutil.copy2(self.store.database,replacement);os.replace(replacement,self.store.database)
        with self.assertRaises(AuthorityError):access.ReplayStore.open(self.store.directory,self.authority_id)
        with self.assertRaises(AuthorityError):access.ReplayStore.open(self.store.directory,'f'*32)
        self.refused('authority_replay_state_unavailable',lambda:self.client().call('hub.authorize',{}))
        self.assertEqual(len(self.calls),1)

    def operator_fixture(self):
        import ownership_contract as ownership
        from test_ownership_contract import FakeBackend
        from authority_operator import OperatorDispatcher
        from types import SimpleNamespace
        backend=FakeBackend()
        for worker in ('one','two'):backend.worker(worker);backend.course('course',worker)
        with patch.object(ownership.uuid,'uuid4',return_value=SimpleNamespace(hex=self.authority_id)):
            authority=ownership.Authority.create(self.root/'ownership',backend)
        for worker in ('one','two'):authority.register_worker(worker)
        authority.register_course('course','one');plans=self.root/'plans';plans.mkdir(mode=0o700)
        dispatcher=OperatorDispatcher(authority,plans);self.server.dispatch=dispatcher.dispatch
        return authority,backend,plans

    def test_operator_real_tls_server_owned_reviewed_plan_digest_and_scoped_inventory(self):
        authority,backend,plans=self.operator_fixture();client=self.client('operator')
        preview=client.call('ownership.preview',{'receiver_worker_id':'two'})
        self.assertRegex(preview['plan_id'],r'^[0-9a-f]{32}$');self.assertEqual([path.name for path in plans.iterdir()],[preview['plan_id']+'.json'])
        self.refused('authority_operation_refused',lambda:client.call('ownership.apply',{'plan_id':preview['plan_id'],'plan_sha256':'f'*64}))
        result=client.call('ownership.apply',{'plan_id':preview['plan_id'],'plan_sha256':preview['plan_sha256']})
        self.assertEqual(result['status'],'COMPLETE');self.assertEqual(result['epoch'],2)
        inventory=client.call('ownership.inventory',{});self.assertEqual(inventory['owner_worker_id'],'two');self.assertIs(inventory['pending'],False)
        self.assertEqual(authority.inventory()['pending_transfers'],0)

    def test_operator_apply_rechecks_all_plan_endpoint_scopes_and_private_reference(self):
        authority,backend,plans=self.operator_fixture();client=self.client('operator');preview=client.call('ownership.preview',{'receiver_worker_id':'two'})
        fingerprint=self.pki.fingerprint(self.credentials['operator'][0]);self.entries[fingerprint]['workers']=['one'];self.write_acl()
        self.refused('authority_operation_refused',lambda:client.call('ownership.apply',{'plan_id':preview['plan_id'],'plan_sha256':preview['plan_sha256']}))
        self.assertEqual(authority.inventory()['pending_transfers'],0);self.assertFalse(backend.barriers)
        self.refused('authority_frame_invalid',lambda:client.call('ownership.apply',{'plan_id':'../secret','plan_sha256':'a'*64}))

    def test_operator_revoked_after_backend_read_cannot_prepare_under_sql_write_lock(self):
        authority,backend,plans=self.operator_fixture();client=self.client('operator');preview=client.call('ownership.preview',{'receiver_worker_id':'two'});changed=[]
        def revoke():
            if not changed:
                changed.append(True);del self.entries[self.pki.fingerprint(self.credentials['operator'][0])];self.write_acl()
        backend.callback=revoke
        self.refused('authority_operation_refused',lambda:client.call('ownership.apply',{'plan_id':preview['plan_id'],'plan_sha256':preview['plan_sha256']}))
        self.assertEqual(authority.inventory()['pending_transfers'],0);self.assertFalse(backend.barriers)

    def test_operator_acl_loss_after_prepare_is_rechecked_under_endpoint_locks_before_effects(self):
        import ownership_contract as ownership
        authority,backend,plans=self.operator_fixture();client=self.client('operator');preview=client.call('ownership.preview',{'receiver_worker_id':'two'})
        def revoke(stage,transaction):
            if stage=='prepared':del self.entries[self.pki.fingerprint(self.credentials['operator'][0])];self.write_acl()
        with patch.object(ownership,'fault_hook',side_effect=revoke):
            self.refused('authority_operation_refused',lambda:client.call('ownership.apply',{'plan_id':preview['plan_id'],'plan_sha256':preview['plan_sha256']}))
        self.assertFalse(backend.barriers);self.assertEqual(authority.inventory()['pending_transfers'],1)


class AuthorityClientDiagnosticsTests(unittest.TestCase):
    def client(self):
        client=AuthorityClient.__new__(AuthorityClient)
        client.host='private-host-canary.invalid';client.port=8443;client.context=object();client.timeout=3
        client.authority_id='a'*32;client.worker_id='worker-private';client.course_id='course-private'
        return client

    def test_transport_logs_only_fixed_stage_and_exception_category_and_closes_once(self):
        import hub.authority_client as module
        cases=[('request',TimeoutError('private-timeout-canary'),'timeout'),
            ('request',ConnectionResetError('private-connection-canary'),'connection'),
            ('request',ssl.SSLError('private-cert-key-canary'),'tls'),
            ('response_headers',http.client.BadStatusLine('private-header-canary'),'http'),
            ('response_body',OSError('private-body-canary'),'connection'),
            ('response_decode',ValueError('private-payload-canary'),'other')]
        for stage,error,category in cases:
            with self.subTest(stage=stage,category=category):
                connection=Mock();response=Mock();connection.getresponse.return_value=response
                response.status=200;response.headers.get_all.return_value=['2']
                response.getheader.side_effect=lambda name:{'Content-Type':'application/json'}.get(name)
                response.read.return_value=b'{}'
                if stage=='request':connection.request.side_effect=error
                elif stage=='response_headers':connection.getresponse.side_effect=error
                elif stage=='response_body':response.read.side_effect=error
                with patch.object(module.http.client,'HTTPSConnection',return_value=connection) as constructor, \
                        patch.object(module,'parsed',side_effect=error if stage=='response_decode' else module.parsed), \
                        self.assertLogs(module.log,level='WARNING') as captured:
                    with self.assertRaises(AuthorityError) as raised:self.client().call('hub.authorize',{'secret':'private-payload-canary'})
                self.assertEqual(raised.exception.code,'authority_transport_unavailable')
                self.assertEqual([record.getMessage() for record in captured.records],
                    ['authority_transport_failure stage='+stage+' category='+category])
                self.assertNotIn('private',str(captured.output));connection.close.assert_called_once_with()
                constructor.assert_called_once();self.assertEqual(constructor.call_args.kwargs['timeout'],3)
                self.assertEqual(connection.request.call_count,1)

    def test_constructor_failure_has_fixed_category_and_no_connection_to_close(self):
        import hub.authority_client as module
        with patch.object(module.http.client,'HTTPSConnection',side_effect=TimeoutError('private-url-cert-canary')) as constructor, \
                self.assertLogs(module.log,level='WARNING') as captured:
            with self.assertRaises(AuthorityError) as raised:self.client().call('hub.authorize',{})
        self.assertEqual(raised.exception.code,'authority_transport_unavailable');constructor.assert_called_once()
        self.assertEqual(captured.records[0].getMessage(),'authority_transport_failure stage=connection category=timeout')

    def test_broken_logging_cannot_replace_terminal_error_or_skip_connection_close(self):
        import hub.authority_client as module
        class SecretError(OSError):
            def __str__(self):raise AssertionError('private-exception-str-never-called')
            def __repr__(self):raise AssertionError('private-exception-repr-never-called')
        connection=Mock();connection.request.side_effect=SecretError('private-secret-args')
        with patch.object(module.http.client,'HTTPSConnection',return_value=connection), \
                patch.object(module.log,'warning',side_effect=RuntimeError('private-logger-canary')) as warning:
            with self.assertRaises(AuthorityError) as raised:self.client().call('hub.authorize',{})
        self.assertEqual(raised.exception.code,'authority_transport_unavailable');connection.close.assert_called_once_with()
        warning.assert_called_once_with('authority_transport_failure stage=%s category=%s','request','connection')

    def test_known_authority_errors_remain_unlogged_and_response_validation_unchanged(self):
        import hub.authority_client as module
        connection=Mock();response=Mock();connection.getresponse.return_value=response;response.status=302
        with patch.object(module.http.client,'HTTPSConnection',return_value=connection), patch.object(module.log,'warning') as warning:
            with self.assertRaises(AuthorityError) as raised:self.client().call('hub.authorize',{})
        self.assertEqual(raised.exception.code,'authority_response_invalid');warning.assert_not_called();connection.close.assert_called_once_with()

    def test_failed_diagnostic_itself_cannot_replace_transport_error_or_skip_close(self):
        import hub.authority_client as module
        connection=Mock();connection.request.side_effect=TimeoutError('private-transport-canary')
        with patch.object(module.http.client,'HTTPSConnection',return_value=connection), \
                patch.object(module,'transport_diagnostic',side_effect=ValueError('private-diagnostic-canary')) as diagnostic:
            with self.assertRaises(AuthorityError) as raised:self.client().call('hub.authorize',{})
        self.assertEqual(raised.exception.code,'authority_transport_unavailable')
        self.assertEqual(str(raised.exception),'authority_transport_unavailable')
        diagnostic.assert_called_once();connection.close.assert_called_once_with()


if __name__=='__main__':unittest.main()
