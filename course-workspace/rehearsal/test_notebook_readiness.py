"""Bounded fixture readiness and safe failure diagnostics, without weaker checks."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from notebook import Notebook


def response(status, issuer=None):
    return SimpleNamespace(status_code=status,json=lambda:{'issuer':issuer})


class NotebookReadinessTests(unittest.TestCase):
    def test_missing_learner_server_reports_api_status_without_weakening_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            book=Notebook(Path(directory)); session=Mock(); book.http_session=Mock(return_value=session)
            session.get.return_value=SimpleNamespace(ok=True,status_code=200,json=lambda:{'server':None})
            with patch('notebook.time.sleep') as sleep,patch.object(book,'learner_readiness_diagnostics') as diagnostics:
                with self.assertRaisesRegex(AssertionError,'notebook did not become ready'):
                    book.ready(Mock(),'alice')
            self.assertEqual(session.get.call_count,60); self.assertEqual(sleep.call_count,60)
            diagnostics.assert_called_once_with({'hub_api':200,'contents':None,'server_present':False})

    def test_learner_diagnostics_bound_states_and_validate_quota_age_without_private_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); book=Notebook(root); book.homes=root/'homes'
            status=book.homes/'.educloud-status'; status.mkdir(parents=True)
            secret='PRIVATE_NEVER_PRINT'
            quota={'instance':book.instance,'healthy':True,'checked_at':1000,
                   'volumes':{secret:{'identity':secret}},'owner':secret}
            path=status/(book.instance+'.json'); path.write_text(json.dumps(quota)); path.chmod(0o600)
            identities=[format(index,'064x') for index in range(1,7)]
            state={'Status':'exited','Running':False,'OOMKilled':True,'ExitCode':137,'Error':secret}
            results=[SimpleNamespace(returncode=0,stdout='\n'.join(identities),stderr=secret)]
            results += [SimpleNamespace(returncode=0,stdout=json.dumps(state),stderr=secret) for _ in range(4)]
            with patch('notebook.subprocess.run',side_effect=results) as command, \
                    patch('notebook.time.time',return_value=1146),patch('builtins.print') as log:
                result=book.learner_readiness_diagnostics({'hub_api':200,'contents':None,'server_present':False,'private':secret})
            self.assertEqual(result['learner_count'],6); self.assertTrue(result['states_truncated'])
            self.assertEqual(len(result['container_states']),4); self.assertEqual(command.call_count,5)
            self.assertEqual(result['quota_verification'],{'healthy':True,'age_seconds':146,'fresh':False})
            self.assertFalse(result['server_present']); self.assertEqual(result['last_http_status'],{'hub_api':200,'contents':None})
            for private in (secret,book.instance,str(root),*identities):
                self.assertNotIn(private,json.dumps(result)); self.assertNotIn(private,str(log.call_args))
            self.assertIn('label=educloud.workspace.instance='+book.instance,command.call_args_list[0].args[0])

    def test_malformed_quota_or_container_metadata_only_returns_unknown_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve(); book=Notebook(root); book.homes=root/'homes'
            status=book.homes/'.educloud-status'; status.mkdir(parents=True)
            path=status/(book.instance+'.json')
            cases=[json.dumps({'instance':book.instance,'healthy':True,'checked_at':10**1000}).encode(),
                   b'['*2000+b'0'+b']'*2000,
                   json.dumps({'instance':book.instance,'healthy':True,'checked_at':float('nan')}).encode(),
                   json.dumps({'instance':'another','healthy':True,'checked_at':1000}).encode()]
            for raw in cases:
                with self.subTest(raw=raw[:40]):
                    path.write_bytes(raw); path.chmod(0o600)
                    self.assertEqual(book.quota_status_diagnostic(),{'healthy':None,'age_seconds':None,'fresh':None})
            with patch('notebook.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout='['*2000+'0'+']'*2000,stderr='')):
                self.assertEqual(book.container_state_diagnostic('owned'),{'status':'unavailable'})

    def test_ready_still_requires_discovery_hub_and_exact_issuer(self):
        with tempfile.TemporaryDirectory() as directory:
            book=Notebook(Path(directory)); book.session=Mock()
            issuer=book.auth+'/realms/course'
            book.session.get.side_effect=[response(503),response(200,issuer),response(503),
                                          response(200,issuer),response(200)]
            with patch('notebook.time.sleep') as sleep,patch.object(book,'readiness_diagnostics') as diagnostics:
                book.wait_for_fixture()
            self.assertEqual(sleep.call_count,2); diagnostics.assert_not_called()
            book.session.get.side_effect=[response(200,issuer+'/wrong'),response(200)]
            with self.assertRaises(AssertionError): book.wait_for_fixture()

    def test_default_keeps_120_poll_attempts_and_reports_last_status(self):
        with tempfile.TemporaryDirectory() as directory:
            book=Notebook(Path(directory)); book.session=Mock()
            book.session.get.side_effect=lambda url,**kwargs:response(200 if url.endswith('/hub/login') else 502)
            with patch('notebook.time.sleep') as sleep,patch.object(book,'readiness_diagnostics') as diagnostics:
                with self.assertRaisesRegex(AssertionError,'OIDC fixture did not become ready'): book.wait_for_fixture()
            self.assertEqual(book.session.get.call_count,121)
            self.assertEqual(sleep.call_count,120)
            diagnostics.assert_called_once_with({'discovery':502,'hub':200})
            self.assertTrue(all(call.kwargs['timeout']==2 for call in book.session.get.call_args_list))

    def test_managed_wait_has_240_second_wall_deadline_including_request_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); book=Notebook(root,allocation_policy=root/'policy.json'); book.session=Mock()
            clock=[0.0]; discovery=[]
            def get(url,timeout):
                self.assertGreater(timeout,0); self.assertLessEqual(timeout,2)
                if url.endswith('/hub/login'): return response(200)
                discovery.append(clock[0]); clock[0]+=timeout; return response(503)
            book.session.get.side_effect=get
            with patch('notebook.time.monotonic',side_effect=lambda:clock[0]), \
                    patch('notebook.time.sleep',side_effect=lambda delay:clock.__setitem__(0,clock[0]+delay)), \
                    patch.object(book,'readiness_diagnostics') as diagnostics:
                with self.assertRaisesRegex(AssertionError,'OIDC fixture did not become ready'): book.wait_for_fixture()
            self.assertEqual(clock[0],240); self.assertEqual(len(discovery),80)
            diagnostics.assert_called_once_with({'discovery':503,'hub':200})

    def test_diagnostics_never_publish_logs_error_environment_identity_or_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            book=Notebook(Path(directory),allocation_policy=Path(directory)/'policy.json')
            secret='PRIVATE_NEVER_PRINT'
            first={'Status':'running','Running':True,'OOMKilled':False,'ExitCode':0,
                   'Error':secret,'Pid':900,'StartedAt':secret,'environment':{'TOKEN':secret}}
            second={'Status':'exited','Running':False,'OOMKilled':True,'ExitCode':137,'Error':secret}
            results=[SimpleNamespace(returncode=0,stdout=json.dumps(first),stderr=secret),
                     SimpleNamespace(returncode=0,stdout=json.dumps(second),stderr=secret),
                     SimpleNamespace(returncode=1,stdout=secret,stderr=secret)]
            with patch('notebook.subprocess.run',side_effect=results) as command,patch('builtins.print') as log:
                result=book.readiness_diagnostics({'discovery':503,'hub':True,'private':secret})
            self.assertEqual(result['containers']['identity'],{'status':'running','running':True,'oom_killed':False,'exit_code':0})
            self.assertEqual(result['containers']['proxy']['exit_code'],137)
            self.assertEqual(result['containers']['hub'],{'status':'unavailable'})
            self.assertEqual(result['last_http_status'],{'discovery':503,'hub':None})
            self.assertNotIn(secret,json.dumps(result)); self.assertNotIn(secret,str(log.call_args))
            self.assertNotIn(book.instance,str(log.call_args)); self.assertNotIn(str(book.root),str(log.call_args))
            self.assertTrue(all(call.args[0][:4]==['docker','inspect','--format','{{json .State}}']
                                and call.kwargs['timeout']==5 for call in command.call_args_list))

    def test_managed_identity_cpu_cap_is_point_four_default_remains_one(self):
        for managed,cpu in ((False,'1'),(True,'0.4')):
            with self.subTest(managed=managed),tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve(); book=Notebook(root,allocation_policy=root/'policy.json' if managed else None)
                book.homes=root/'homes'; book.homes.mkdir()
                book.env={'WORKSPACE_PORT':'18000','WORKSPACE_OIDC_CLIENT_SECRET':'synthetic-secret'}
                with patch('notebook.run',side_effect=['sha256:'+'a'*64,'sha256:'+'b'*64]): book.render_setup()
                with patch('notebook.run') as run: book.configure_fixture_edges()
                args=run.call_args.args
                self.assertEqual(args[args.index('--cpus')+1],cpu)


if __name__=='__main__': unittest.main()
