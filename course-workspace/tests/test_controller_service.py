"""Composition unit tests use real TLS sockets/state and a fake Docker backend."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from authority_test_pki import TestPKI
from test_ownership_contract import FakeBackend
import authority_access as access
import controller_service as controller
import ownership_contract as ownership


class ControllerServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory();cls.pki=TestPKI(Path(cls.temporary.name).resolve()/'pki');cls.guardian_cert,_=cls.pki.issue('guardian')

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve();self.backend=FakeBackend()
        for worker in ('one','two'):self.backend.worker(worker);self.backend.course('course',worker)
        self.authority=ownership.Authority.create(self.root/'authority',self.backend)
        for worker in ('one','two'):self.authority.register_worker(worker)
        self.authority.register_course('course','one');self.replay=access.ReplayStore.create(self.root/'replay',self.authority.authority_id)
        self.plans=self.root/'plans';self.plans.mkdir(mode=0o700);self.acl=self.root/'acl.json';self.fingerprint=self.pki.fingerprint(self.guardian_cert)
        self.entries={self.fingerprint:{'role':'guardian','workers':['one'],'courses':['course']}};self.write_acl()
        self.config={'authority_id':self.authority.authority_id};self.service={'authority_directory':str(self.authority.directory),'replay_directory':str(self.replay.directory),
            'plan_directory':str(self.plans),'acl_file':str(self.acl),'bind':'127.0.0.1','port':0,'certfile':str(self.pki.server_cert),'keyfile':str(self.pki.server_key),'ca_file':str(self.pki.ca_cert)}

    def write_acl(self):
        self.acl.write_bytes(access.encoded({'version':access.VERSION,'authority_id':self.authority.authority_id,'clients':self.entries}));self.acl.chmod(0o600)

    def test_actual_tls_listener_closes_on_watchdog_failure_without_logging_or_state_reset(self):
        created=[];original=controller.AuthorityServer;captured=io.StringIO();before=self.authority.inventory()
        class Watchdog:
            def supervision_tick(self):raise RuntimeError('PRIVATE WATCHDOG DETAILS')
            def dispatch(self,*args):raise AssertionError('failure served runtime request')
        def server(*args,**kwargs):
            value=original(*args,**kwargs);created.append(value);return value
        with patch.object(controller,'DockerBackend',return_value=self.backend),patch.object(controller,'RuntimeDispatcher',return_value=Watchdog()), \
                patch.object(controller,'AuthorityServer',side_effect=server),patch.object(ownership.Authority,'create',side_effect=AssertionError('startup recreated authority')), \
                patch.object(access.ReplayStore,'create',side_effect=AssertionError('startup recreated replay')),redirect_stdout(captured),redirect_stderr(captured):
            with self.assertRaisesRegex(RuntimeError,'authority_supervision_failed'):controller.serve(self.config,self.service)
        self.assertEqual(len(created),1);self.assertEqual(created[0].socket.fileno(),-1);self.assertEqual(captured.getvalue(),'')
        with self.assertRaises(OSError):socket.create_connection(('127.0.0.1',created[0].server_port),timeout=.2)
        self.assertEqual(self.authority.inventory(),before);self.assertEqual(list(self.plans.iterdir()),[])
        self.assertEqual(created[0].threads,set())

    def test_missing_authority_or_replay_state_refuses_before_listener_and_never_recreates(self):
        for name,path in [('authority',self.authority.database),('replay',self.replay.database)]:
            held=path.with_suffix('.held');path.rename(held)
            try:
                before=set(path.parent.iterdir())
                with self.subTest(state=name),patch.object(controller,'DockerBackend',return_value=self.backend), \
                        patch.object(controller,'RuntimeDispatcher'),patch.object(controller,'AuthorityServer') as server, \
                        patch.object(ownership.Authority,'create',side_effect=AssertionError('authority bootstrap')), \
                        patch.object(access.ReplayStore,'create',side_effect=AssertionError('replay bootstrap')):
                    with self.assertRaises((ownership.OwnershipError,access.AuthorityError)):controller.serve(self.config,self.service)
                    server.assert_not_called()
                self.assertFalse(path.exists());self.assertEqual(set(path.parent.iterdir()),before)
            finally:held.rename(path)

    def test_guardian_policy_callback_is_fresh_and_operator_runtime_routing_is_separate(self):
        calls=[];guard=[];watchdog_failed=threading.Event()
        class Runtime:
            def dispatch(self,*args):calls.append(('runtime',args));return {'kind':'runtime'}
            def supervision_tick(self):watchdog_failed.set();raise RuntimeError('finish controlled unit loop')
        class Operator:
            def dispatch(self,*args):calls.append(('operator',args));return {'kind':'operator'}
        class Server:
            def __init__(self,*args,**kwargs):
                self.dispatch=kwargs['dispatch'];self.closed=False
            def shutdown(self):pass
            def serve_forever(self,**kwargs):
                self.dispatch({'role':'operator'},'ownership.inventory','course',{})
                self.dispatch({'role':'hub'},'hub.authorize','course',{})
                if not watchdog_failed.wait(2):raise AssertionError('unit watchdog did not run')
            def server_close(self):self.closed=True
        def runtime(authority,backend,authorize):guard.append(authorize);return Runtime()
        with patch.object(controller,'DockerBackend',return_value=self.backend),patch.object(controller,'RuntimeDispatcher',side_effect=runtime), \
                patch.object(controller,'OperatorDispatcher',return_value=Operator()),patch.object(controller,'AuthorityServer',Server):
            with self.assertRaisesRegex(RuntimeError,'authority_supervision_failed'):controller.serve(self.config,self.service)
        self.assertEqual([value[0] for value in calls],['operator','runtime']);self.assertEqual(len(guard),1)
        guard[0](self.fingerprint,'one','course');self.entries.clear();self.write_acl()
        with self.assertRaises(access.AuthorityError):guard[0](self.fingerprint,'one','course')

    def test_listener_is_closed_if_watchdog_thread_cannot_start(self):
        created=[];original=controller.AuthorityServer
        def server(*args,**kwargs):value=original(*args,**kwargs);created.append(value);return value
        with patch.object(controller,'DockerBackend',return_value=self.backend),patch.object(controller,'RuntimeDispatcher'),patch.object(controller,'AuthorityServer',side_effect=server), \
                patch.object(controller.threading.Thread,'start',side_effect=RuntimeError('synthetic OS thread-start refusal')):
            with self.assertRaises(RuntimeError):controller.serve(self.config,self.service)
        self.assertEqual(len(created),1);self.assertEqual(created[0].socket.fileno(),-1)


if __name__=='__main__':unittest.main()
