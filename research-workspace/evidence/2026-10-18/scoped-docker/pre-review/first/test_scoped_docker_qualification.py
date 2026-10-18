"""Synthetic harness/oracle controls. These tests never launch Docker or a broker."""
import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import qualify_scoped_docker as q
from research_workspace import runtime
from research_workspace.store import Store


def receipt(index=0, rid=None, pid=None):
    return dict(schema_version=1, request_id=rid or f'{index + 1:032x}', project_id=pid or f'{index // 2 + 10:032x}',
                image=q.IMAGES[q.MATRIX[index]['runtime']], status='succeeded', admitted=True,
                execution_started=True, cleanup_verified=True, unresolved_liability=False, exit_code=0,
                error='', stdout=json.dumps(q.ORACLES[index]), stderr='',
                compute=dict(reserved_cpu_seconds=30, charged_cpu_seconds=30, provenance='estimated_upper_bound'))


def complete_report():
    result = dict(format=q.FORMAT, exercise=q.EXERCISE, manifest_sha256='a' * 64,
                  checks={name: True for name in q.REQUIRED}, attempts=[], charged_cpu_seconds=120)
    for i, plan in enumerate(q.MATRIX):
        r = receipt(i)
        path = '/synthetic-private/broker'
        identity = dict(owner='1' * 32, authority_id='2' * 32, path_sha256=q.digest(path.encode()))
        request = dict(request_id=r['request_id'], project_id=r['project_id'], image=r['image'])
        binding = dict(index=i, slot=plan['slot'], exercise=q.EXERCISE, broker_sha256=q.BROKER_SHA,
                       manifest_sha256=result['manifest_sha256'], ledger_identity=identity, ledger_path=path, **request)
        record = dict(r, owner=identity['owner'], request=request, create_attempted=True, create_finished=True,
                      create_ambiguous=False, container_name='outfitter-' + q.digest((identity['owner'] + ':' + r['request_id']).encode())[:32])
        run = dict(id=r['request_id'], project_id=r['project_id'], environment=dict(image=r['image']),
                   status='succeeded', cleanup_verified=True, compute=dict(r['compute'], unresolved_liability=False), metrics=q.ORACLES[i])
        result['attempts'].append(dict(binding=binding, binding_sha256=q.digest(q.encoded(binding)), final_receipt=r,
            request=request, run=run, ledger_record=record, ledger_identity=identity, ledger_verified=True,
            container_absent=True, metrics=q.ORACLES[i]))
    return copy.deepcopy(result)


class OracleTests(unittest.TestCase):
    def test_complete_bound_synthetic_observations_pass(self):
        self.assertTrue(q.accepted(complete_report()))

    def test_empty_partial_duplicate_extra_or_false_controls_fail(self):
        for change in ('empty', 'missing', 'extra', 'false'):
            report = complete_report()
            if change == 'empty': report['checks'] = {}
            elif change == 'missing': report['checks'].pop(next(iter(q.REQUIRED)))
            elif change == 'extra': report['checks']['unreviewed'] = True
            else: report['checks'][next(iter(q.REQUIRED))] = 1
            with self.subTest(change=change): self.assertFalse(q.accepted(report))

    def test_partial_duplicate_or_fifth_attempt_fails(self):
        for size in (0, 1, 3, 5):
            report = complete_report()
            report['attempts'] = (report['attempts'] + report['attempts'])[:size]
            self.assertFalse(q.accepted(report))
        report = complete_report(); report['attempts'][1] = copy.deepcopy(report['attempts'][0])
        self.assertFalse(q.accepted(report))

    def test_zero_partial_float_bool_refund_or_unknown_charge_fails(self):
        for value in (0, 1, 29, 30.0, True):
            for field in ('reserved_cpu_seconds', 'charged_cpu_seconds'):
                report = complete_report(); report['attempts'][0]['final_receipt']['compute'][field] = value
                with self.subTest(value=value, field=field): self.assertFalse(q.accepted(report))
        for field, value in (('unresolved_liability', True), ('cleanup_verified', False), ('status', 'unknown'), ('admitted', False)):
            report = complete_report(); report['attempts'][0]['final_receipt'][field] = value
            self.assertFalse(q.accepted(report))

    def test_binary_ledger_request_project_image_and_manifest_binding_failures(self):
        for field in ('broker_sha256', 'request_id', 'project_id', 'image', 'manifest_sha256'):
            report = complete_report(); binding = report['attempts'][0]['binding']; binding[field] = 'changed'
            report['attempts'][0]['binding_sha256'] = q.digest(q.encoded(binding))
            with self.subTest(field=field): self.assertFalse(q.accepted(report))
        for target, field in (('ledger_record', 'owner'), ('ledger_identity', 'authority_id'), ('request', 'request_id')):
            report = complete_report(); report['attempts'][0][target][field] = 'different'
            self.assertFalse(q.accepted(report))

    def test_generic_receipt_and_failure_marker_cannot_become_pass(self):
        report = complete_report(); report['attempts'][0]['final_receipt'] = dict(status='succeeded', cleanup_verified=True)
        self.assertFalse(q.accepted(report))
        report = complete_report(); report['failure'] = dict(type='KeyboardInterrupt')
        self.assertFalse(q.accepted(report))

    def test_ledger_or_application_disagreement_fails(self):
        for key in ('ledger_record', 'run'):
            report = complete_report(); report['attempts'][0][key]['compute']['charged_cpu_seconds'] = 0
            self.assertFalse(q.accepted(report))
        report = complete_report(); report['attempts'][0]['ledger_record']['create_ambiguous'] = True
        self.assertFalse(q.accepted(report))

    def test_independent_numeric_oracle_checks_both_output_and_saved_result(self):
        for key in ('metrics', 'run'):
            report = complete_report()
            if key == 'metrics': report['attempts'][0]['metrics']['groups'][0]['mean'] = 23
            else: report['attempts'][0]['run']['metrics']['mean'] = 0
            self.assertFalse(q.accepted(report))
        for bad in (True, float('nan'), float('inf'), '18'):
            value = copy.deepcopy(q.ORACLES[0]); value['mean'] = bad
            self.assertFalse(q.oracle_matches(value, q.ORACLES[0]))

    def test_exercise_marker_survives_missing_or_changed_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); marker = root / 'exercise.started.json'
            manifest = dict(output=str(root / 'first'), restart_marker=str(marker))
            audit = q.Audit(manifest, 'a' * 64)
            self.assertEqual(audit.report['status'], 'FAIL')
            manifest['output'] = str(root / 'different')
            with self.assertRaises(FileExistsError): q.Audit(manifest, 'a' * 64)
            self.assertFalse((root / 'different').exists())

    def test_exclusive_output_and_fifth_attempt_refuse_without_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); output = root / 'already-exists'; output.mkdir()
            manifest = dict(output=str(output), restart_marker=str(root / 'started'))
            with self.assertRaises(FileExistsError): q.Audit(manifest, 'b' * 64)
            self.assertTrue((root / 'started').exists())
        audit = object.__new__(q.Audit); audit.report = dict(attempts=[{}] * 4)
        with patch.object(q, 'file_hash', side_effect=AssertionError('must not inspect/spawn')):
            with self.assertRaisesRegex(ValueError, 'ceiling'): audit.before_launch('unused', 'unused', 'unused')

    def test_manifest_captures_every_runtime_source_and_disabled_catalog(self):
        hashes = q.source_hashes()
        self.assertIn('environments.json', hashes)
        for name in ('runtime', 'sqlite_ownership', 'bounded_export', 'store', '__main__'):
            self.assertIn('research_workspace/' + name + '.py', hashes)


class SimulatedWiringTests(unittest.TestCase):
    """Real loopback HTTP and v4 Store/Worker; all external effects simulated.

    Passing here is not local Docker qualification. Temporary evidence is never
    copied into the real exercise, and its stable marker is a temporary path.
    """
    def simulated(self, interrupt=False):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp).resolve(); binary = Path(sys.executable).resolve()
            stack.enter_context(patch.object(q, 'BROKER', binary))
            stack.enter_context(patch.object(q, 'BROKER_SHA', q.file_hash(binary)))
            manifest = dict(output=str(root / 'synthetic'), restart_marker=str(root / 'synthetic.started'),
                            source_commit='c' * 40, source_archive_sha256='d' * 64,
                            prerequisites=dict(images={k: dict(id='sha256:' + 'a' * 64) for k in q.IMAGES}))
            manifest['sources'] = q.source_hashes()
            audit = q.Audit(manifest, 'e' * 64)
            def operator(*args):
                if args[0] == 'init':
                    config = dict(version=1, mode=q.finite.SCOPED_MODE, broker=str(binary), images=q.IMAGES,
                                  total_budget_cpu_seconds=120, logical_authority=q.finite.profile(4))
                    store = Store(audit.state, create=True, guarded=True, config=config, subject='owner')
                    return dict(access_key=store.initial_key)
                return dict(access_key=Store(audit.state).add_user('editor'))
            stack.enter_context(patch.object(q, 'cli', side_effect=operator))
            def init_project(broker, pid, budget, launch=None):
                def write():
                    target = Path(broker.state); target.mkdir(exist_ok=True)
                    if (target / 'ledger.json').exists(): data = audit.ledger()
                    else: data = dict(version=2, owner='a' * 32, authority=dict(authority_id='b' * 32, state_path=str(target), admission='active'), projects={}, runs={})
                    data['projects'][pid] = dict(project_id=pid, budget_cpu_seconds=budget)
                    (target / 'ledger.json').write_bytes(q.encoded(data))
                return launch(write) if launch else write()
            stack.enter_context(patch.object(q.ObservedBroker, 'init_project', init_project))
            last = {}
            def spawn(command, **kwargs):
                self.assertEqual(command[:3], [str(binary), '--state-dir', str(audit.state / 'broker')])
                self.assertEqual(command[3:5], ['run', '--request'])
                index = len(audit.report['attempts']) - 1
                self.assertTrue((audit.root / f'{index + 1}-binding.private.json').is_file())
                if interrupt: raise KeyboardInterrupt('synthetic interruption before external effect')
                request = q.strict_json(Path(command[5]).read_bytes())
                value = receipt(index, request['request_id'], request['project_id'])
                data = audit.ledger()
                data['runs'][request['request_id']] = dict(value, owner=data['owner'], request=request, create_attempted=True,
                    create_finished=True, create_ambiguous=False, container_name='outfitter-' + q.digest((data['owner'] + ':' + request['request_id']).encode())[:32])
                (audit.state / 'broker/ledger.json').write_bytes(q.encoded(data))
                last['receipt'] = value
                return Mock(returncode=0)
            stack.enter_context(patch.object(runtime.subprocess, 'Popen', side_effect=spawn))
            stack.enter_context(patch.object(runtime, 'collect', side_effect=lambda *a, **k: (q.encoded(last['receipt']), b'')))
            def inspect(command, **kwargs):
                self.assertEqual(command[:3], ['docker', 'container', 'inspect'])
                return subprocess.CompletedProcess(command, 1, b'[]\n', ('Error response from daemon: No such container: ' + command[3] + '\n').encode())
            stack.enter_context(patch.object(q.subprocess, 'run', side_effect=inspect))
            stack.enter_context(patch.object(q, 'prerequisites', return_value=manifest['prerequisites']))
            stack.enter_context(patch.object(q.recovery, 'checkpoint', return_value=dict(checkpoint_sha256='f' * 64)))
            stack.enter_context(patch.object(q.recovery, 'restore', side_effect=ValueError('not qualified: simulated refusal')))
            original_shutdown = q.Server.shutdown
            def shutdown(server):
                if interrupt:
                    retained = q.strict_json((audit.root / 'report.json').read_bytes())
                    self.assertEqual(retained['failure']['type'], 'KeyboardInterrupt')
                    self.assertEqual(retained['status'], 'FAIL')
                return original_shutdown(server)
            stack.enter_context(patch.object(q.Server, 'shutdown', shutdown))
            result = q.exercise(audit)
            return copy.deepcopy(result)

    def test_real_http_v4_worker_wiring_with_simulated_provider(self):
        report = self.simulated()
        self.assertNotIn('failure', report, report.get('failure'))
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(len(report['attempts']), 4)
        self.assertEqual(report['charged_cpu_seconds'], 120)
        self.assertEqual(set(report['refusal_controls']), {'queued_cancel', 'current_access'})

    def test_interrupt_records_failure_before_cleanup_and_stops_after_one_intent(self):
        report = self.simulated(interrupt=True)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['failure']['type'], 'KeyboardInterrupt')
        self.assertEqual(len(report['attempts']), 1)
        self.assertEqual(report['attempts'][0]['outcome'], 'UNKNOWN_BEFORE_LAUNCH')
        self.assertFalse(q.accepted(report))


if __name__ == '__main__': unittest.main()
