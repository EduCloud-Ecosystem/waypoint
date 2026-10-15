"""Bounded real subprocess/TLS fixture and public-evidence negative controls."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import remote_worker_fixture as fixture
import verify_remote_workers as verifier
from ownership_contract import digest


class LocalBackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'worker'
        self.authority = uuid.uuid4().hex
        fixture.create_worker_files(self.root, self.authority, 'worker-one', ['main-course', 'peer-course'])
        self.backend = fixture.LocalWorkerBackend(self.root, initialize=True); self.addCleanup(self.backend.close)

    def request(self, course='main-course', epoch=1):
        worker = self.backend.inspect_worker('worker-one')
        return {'authority_id': self.authority, 'worker_id': 'worker-one', 'course_id': course,
            'provider_id': worker['provider_id'], 'boot_id': worker['boot_id'], 'epoch': epoch}

    def transfer(self, epoch=1):
        request = self.request()
        return {'authority_id': self.authority, 'course_id': 'main-course',
            'source_worker_id': 'worker-one', 'receiver_worker_id': 'worker-two',
            'source_provider_id': request['provider_id'], 'source_boot_id': request['boot_id'],
            'receiver_provider_id': 'provider-two', 'receiver_boot_id': 'b' * 32,
            'old_epoch': epoch, 'transaction_id': uuid.uuid4().hex, 'plan_sha256': 'a' * 64}

    def test_actual_process_floor_and_peer_preservation(self):
        for course in ('main-course', 'peer-course'):
            request = self.request(course)
            with self.backend.activation_context(request):
                result = self.backend.runtime(request, 'hub.spawn', {}, lambda: None)
            self.assertTrue(result['running'])
        peer = self.backend.processes.live('worker-one', 'peer-course')
        request = self.transfer()
        with self.backend.locks([('worker-one', 'main-course')]):
            self.backend.fence(request, ['worker-one']); self.backend.stop_endpoint('worker-one', 'main-course')
        self.assertEqual(self.backend.processes.live('worker-one', 'peer-course'), peer)
        self.assertEqual(self.backend.inspect_course('main-course', 'worker-one')['running_count'], 0)
        with self.assertRaises(ValueError), self.backend.activation_context(self.request()): self.fail('old epoch admitted')
        self.backend.processes.launch('worker-one', 'main-course', self.backend.home('main-course'), epoch=1, expect_refusal=True)
        with self.backend.activation_context(self.request(epoch=2)):
            self.assertTrue(self.backend.runtime(self.request(epoch=2), 'hub.spawn', {}, lambda: None)['running'])

    def test_runtime_requires_exact_held_scope_and_fixed_payload(self):
        with self.assertRaises(ValueError): self.backend.runtime(self.request(), 'hub.spawn', {}, lambda: None)
        with self.backend.activation_context(self.request()):
            for operation, payload in [('arbitrary', {}), ('hub.spawn', {'command': 'private-canary'})]:
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    self.backend.runtime(self.request(), operation, payload, lambda: None)
            with self.assertRaises(ValueError):
                self.backend.runtime(self.request('peer-course'), 'hub.spawn', {}, lambda: None)
        self.assertEqual(len(self.backend.processes.children), 0)

    def test_revocation_during_child_launch_compensates_exact_owned_process(self):
        calls = []
        def authorize():
            calls.append(True)
            if len(calls) == 3: raise ValueError('fixed_revoked')
        with self.backend.activation_context(self.request()), self.assertRaises(ValueError):
            self.backend.runtime(self.request(), 'hub.spawn', {}, authorize)
        self.assertEqual(self.backend.processes.running_count('worker-one', 'main-course'), 0)
        self.assertTrue((self.backend.home('main-course') / 'saved.txt').is_file())

    def test_atomic_history_rollback_and_replaced_database_refuse(self):
        original = self.backend.barrier('worker-one', 'main-course')
        store = self.backend._store
        def fail(connection, course, value):
            store(connection, course, value); raise ValueError('controlled_interrupt')
        with patch.object(self.backend, '_store', side_effect=fail), self.assertRaises(ValueError):
            self.backend.fence(self.transfer(), ['worker-one'])
        self.assertEqual(self.backend.barrier('worker-one', 'main-course'), original)
        duplicate = self.root / 'replacement.sqlite'
        duplicate.write_bytes(self.backend.database.read_bytes()); duplicate.chmod(0o600)
        os.replace(duplicate, self.backend.database)
        with self.assertRaisesRegex(ValueError, 'fixture_barrier_binding_changed'):
            self.backend.barrier('worker-one', 'main-course')

    def test_snapshot_marker_is_associated_with_actual_retained_bytes(self):
        directory = self.root / 'courses/main-course'
        old = (directory / 'snapshot.fixture').read_bytes()
        (directory / 'retained/saved.txt').write_text('independent changed synthetic work')
        fixture.refresh_snapshot(self.root, 'main-course')
        self.assertNotEqual(old, (directory / 'snapshot.fixture').read_bytes())
        self.assertEqual((directory / 'snapshot.fixture').read_text().splitlines()[-1], fixture.retained_digest(directory / 'retained'))
        self.assertEqual((directory / 'snapshot.fixture').stat().st_mode & 0o777, 0o600)

    def test_close_waits_for_paused_actual_launch_and_refuses_future_effects(self):
        entered = threading.Event(); release = threading.Event(); closed = threading.Event(); failures = []
        launch = self.backend.processes.launch
        def paused(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise ValueError('test_release_missing')
            return launch(*args, **kwargs)
        def start():
            try:
                request = self.request()
                with self.backend.activation_context(request): self.backend.runtime(request, 'hub.spawn', {}, lambda: None)
            except Exception as exc: failures.append(type(exc).__name__)
        def close():
            try: self.backend.close(); closed.set()
            except Exception as exc: failures.append(type(exc).__name__)
        first = threading.Thread(target=start); shutdown = threading.Thread(target=close)
        try:
            with patch.object(self.backend.processes, 'launch', side_effect=paused):
                first.start(); self.assertTrue(entered.wait(2)); shutdown.start()
                self.assertFalse(closed.wait(.05), 'cleanup certified while runtime effect still live')
                release.set(); first.join(4); shutdown.join(4)
            self.assertFalse(first.is_alive()); self.assertFalse(shutdown.is_alive())
            self.assertEqual(failures, []); self.assertTrue(closed.is_set())
            self.assertTrue(self.backend.processes.cleanup_verified)
            self.assertEqual(self.backend.processes.running_count('worker-one', 'main-course'), 0)
            with self.assertRaisesRegex(ValueError, 'fixture_worker_closing'):
                self.backend.runtime(self.request(), 'hub.spawn', {}, lambda: None)
            with self.assertRaisesRegex(ValueError, 'fixture_worker_closing'): self.backend.fence(self.transfer(), ['worker-one'])
        finally:
            release.set()
            if first.ident is not None: first.join(4)
            if shutdown.ident is not None: shutdown.join(4)


class EvidenceTests(unittest.TestCase):
    @staticmethod
    def report():
        return {'version': verifier.VERSION, 'status': 'PASS', 'scope': 'two-local-process-workers',
            'source': {'revision': 'a' * 40, 'code_sha256': {n: 'b' * 64 for n in verifier.CODE_FILES}},
            'checks': list(verifier.CHECKS), 'flags': dict(verifier.FLAGS), 'logical_workers': 2,
            'concurrent_worker_server_processes': 2, 'worker_agent_restarts': 1, 'real_worker_hosts': 0,
            'successful_transfers': 2, 'successful_reconciliations': 1,
            'listener_reconnect_peer_continuity': True, 'agent_restart_peer_continuity': False, 'cleanup_verified': True}

    def test_publisher_strict_schema_controls_counts_boundaries_and_exclusive_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); good = self.report()
            changes = [lambda v: v.update(cleanup_verified=False), lambda v: v['checks'].pop(),
                lambda v: v['checks'].append(v['checks'][0]), lambda v: v.update(logical_workers=True),
                lambda v: v['flags'].update(authenticated_mtls_exercised=1),
                lambda v: v.update(private_path='/private-canary'),
                lambda v: v['source']['code_sha256'].update({'ownership_contract.py': 'not-a-hash'}),
                lambda v: v.update(agent_restart_peer_continuity=True)]
            for change in changes:
                value = copy.deepcopy(good); change(value)
                with self.assertRaises(ValueError): verifier.publish_pass(root, value)
                self.assertFalse((root / 'REMOTE-WORKERS-PASS.json').exists())
            path = verifier.publish_pass(root, good)
            self.assertEqual(json.loads(path.read_bytes()), good)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError): verifier.publish_pass(root, good)
            self.assertNotIn('private-canary', path.read_text())

    def test_dirty_source_never_publishes_or_starts_fixture(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(verifier.evidence, '_source',
                return_value={'revision': 'a' * 40, 'dirty': True}), patch.object(verifier, 'RemoteFixture') as start:
            with self.assertRaisesRegex(ValueError, 'clean_source_required'): verifier.run(Path(temporary) / 'out')
            start.assert_not_called()

    def test_full_actual_tls_process_flow_unit_provenance_only(self):
        # Only provenance is stubbed: all TLS, remote locks/journals, actual
        # process calls, retained bytes and cleanup controls execute unchanged.
        with tempfile.TemporaryDirectory() as temporary, patch.object(verifier, '_source', return_value=self.report()['source']):
            report = verifier.run(Path(temporary) / 'out')
            self.assertEqual(json.loads(report.read_bytes()), self.report())

    def test_partial_worker_start_cleanup_failure_never_publishes_pass(self):
        partial = type('Partial', (), {'cleanup_verified': False, 'close': lambda _: (_ for _ in ()).throw(ValueError())})()
        with tempfile.TemporaryDirectory() as temporary, patch.object(verifier, '_source', return_value=self.report()['source']), \
                patch.object(verifier, 'RemoteFixture', return_value=partial), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(ValueError): verifier.run(Path(temporary) / 'out')
            result = json.loads(output.getvalue())
            self.assertIs(result['cleanup_verified'], False)
            self.assertEqual(result['status'], 'FAIL')
            self.assertFalse((Path(temporary) / 'out/REMOTE-WORKERS-PASS.json').exists())

    def test_constructor_failure_has_no_positive_cleanup_claim(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(verifier, '_source', return_value=self.report()['source']), \
                patch.object(verifier, 'RemoteFixture', side_effect=ValueError('private-canary')), contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(ValueError): verifier.run(Path(temporary) / 'out')
            value = json.loads(output.getvalue())
            self.assertIs(value['cleanup_verified'], False)
            self.assertNotIn('private-canary', output.getvalue())

    def test_existing_or_symlink_output_refuses_before_start(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(verifier, '_source', return_value=self.report()['source']), \
                patch.object(verifier, 'RemoteFixture') as start:
            root = Path(temporary); existing = root / 'existing'; existing.mkdir()
            link = root / 'linked'; link.symlink_to(existing)
            for directory in (existing, link):
                with self.assertRaisesRegex(ValueError, 'fresh_report_directory_required'): verifier.run(directory)
            start.assert_not_called()

    def test_source_drift_after_actual_cleanup_saves_failure_packet(self):
        source = self.report()['source']; changed = dict(source, revision='c' * 40)
        with tempfile.TemporaryDirectory() as temporary, patch.object(verifier, '_source', side_effect=[source, changed]), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'remote_worker_rehearsal_failed'):
                verifier.run(Path(temporary) / 'out')
            directory = Path(temporary) / 'out'
            self.assertFalse((directory / 'REMOTE-WORKERS-PASS.json').exists())
            value = json.loads((directory / 'REMOTE-WORKERS-FAIL.json').read_bytes())
            self.assertIs(value['cleanup_verified'], True)
            self.assertEqual(value['source'], source)


class OperatorTests(unittest.TestCase):
    def test_actual_cli_inventory_preview_apply_and_config_cas(self):
        import remote_control
        from authority_access import AuthorityError
        with tempfile.TemporaryDirectory() as temporary:
            f = fixture.RemoteFixture(Path(temporary).resolve() / 'private')
            try:
                peer = f.runtime('worker-one', 'peer-course', 'hub.spawn')
                f.runtime('worker-one', 'main-course', 'hub.spawn')
                plans = f.root / 'plans'; plans.mkdir(mode=0o700)
                config = {'version': remote_control.VERSION, 'authority_id': f.authority_id,
                    'authority_directory': str(f.root / 'authority'), 'plan_directory': str(plans), 'workers': {}}
                for worker, client in f.clients.items():
                    config['workers'][worker] = {'evidence': f.backend.workers[worker],
                        'courses': list(f.backend.courses[worker]), 'channel': {'host': 'localhost', 'port': client.port,
                            'server_sha256': client.server_sha256, 'ca_file': str(f.pki.ca_cert),
                            'certfile': str(f.credentials[0]), 'keyfile': str(f.credentials[1])}}
                path = f.root / 'operator.json'; fixture.private_json(path, config)
                def cli(*args):
                    return subprocess.run([sys.executable, str(ROOT / 'remote_control.py'), '--config', str(path), *args],
                        capture_output=True, text=True, timeout=15)
                inventory = cli('inventory'); self.assertEqual(inventory.returncode, 0)
                self.assertIs(json.loads(inventory.stdout)['worker_observations_verified'], True)
                preview = cli('preview', '--course', 'main-course', '--receiver', 'worker-two')
                self.assertEqual(preview.returncode, 0); decision = json.loads(preview.stdout)
                wrong = cli('apply', '--course', 'main-course', '--plan-id', decision['plan_id'], '--plan-sha256', '0' * 64)
                self.assertNotEqual(wrong.returncode, 0)
                self.assertEqual(wrong.stderr.strip(), 'remote_worker_operation_refused')
                complete = cli('apply', '--course', 'main-course', '--plan-id', decision['plan_id'], '--plan-sha256', decision['plan_sha256'])
                self.assertEqual(complete.returncode, 0); self.assertEqual(json.loads(complete.stdout)['status'], 'COMPLETE')
                self.assertEqual(f.runtime('worker-one', 'peer-course'), peer)
                self.assertFalse(f.runtime('worker-two', 'main-course')['running'])
                # Mutate trusted config after initial load; real dispatcher
                # fresh callback must refuse before a reviewed decision/effect.
                dispatch = remote_control.OperatorDispatcher.dispatch
                def changed(dispatcher, principal, operation, course, payload):
                    fixture.private_json(path, dict(config, version='private-canary'))
                    return dispatch(dispatcher, principal, operation, course, payload)
                with patch.object(remote_control.OperatorDispatcher, 'dispatch', new=changed), self.assertRaises(AuthorityError):
                    remote_control.execute(path, 'ownership.preview', 'main-course', {'receiver_worker_id': 'worker-one'})
                fixture.private_json(path, config)
                self.assertEqual(f.authority.inventory()['pending_transfers'], 0)
                self.assertEqual(f.runtime('worker-one', 'peer-course'), peer)
                for result in (inventory, preview, complete):
                    self.assertLess(len(result.stdout), 16384)
                    self.assertNotIn(str(f.root), result.stdout)
                    self.assertNotIn('PRIVATE KEY', result.stdout)
            finally: f.close()
            self.assertTrue(f.cleanup_verified)


if __name__ == '__main__': unittest.main()
