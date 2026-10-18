"""Synthetic harness/oracle controls. These tests never launch Docker or a broker."""
import copy
from contextlib import ExitStack
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import Mock, patch

import qualify_scoped_docker as q
from research_workspace import runtime, sqlite_ownership
from research_workspace.evidence import build_bundle
from research_workspace.store import Store


def receipt(index=0, rid=None, pid=None):
    return dict(schema_version=1, request_id=rid or f'{index + 1:032x}', project_id=pid or f'{index // 2 + 10:032x}',
                image=q.IMAGES[q.MATRIX[index]['runtime']], status='succeeded', admitted=True,
                execution_started=True, cleanup_verified=True, unresolved_liability=False, exit_code=0,
                error='', stdout=json.dumps(q.ORACLES[index]), stderr='',
                compute=dict(reserved_cpu_seconds=30, charged_cpu_seconds=30, provenance='estimated_upper_bound'))


def synthetic_manifest():
    return dict(source_commit='c' * 40, source_archive_sha256='d' * 64, output='/synthetic',
                prerequisites=dict(images={k: dict(id='sha256:' + 'a' * 64) for k in q.IMAGES}))


def complete_report():
    result = dict(format=q.FORMAT, exercise=q.EXERCISE, manifest_sha256='a' * 64,
                  checks={name: True for name in q.REQUIRED}, attempts=[], charged_cpu_seconds=120, accounting_complete=True,
                  projects={'first':f'{10:032x}', 'second':f'{11:032x}'})
    for i, plan in enumerate(q.MATRIX):
        r = receipt(i)
        path = '/synthetic/state.private/broker'
        identity = dict(owner='1' * 32, authority_id='2' * 32, path_sha256=q.digest(path.encode()))
        request = q.planned_request(i, r['request_id'], r['project_id'], path)
        binding = dict(index=i, slot=plan['slot'], exercise=q.EXERCISE, broker_sha256=q.BROKER_SHA,
                       manifest_sha256=result['manifest_sha256'], ledger_identity=identity, ledger_path=path,
                       request_id=r['request_id'], project_id=r['project_id'], image=r['image'], image_id='sha256:' + 'a' * 64,
                       source_commit='c' * 40, source_archive_sha256='d' * 64, broker_source_commit=q.BROKER_COMMIT,
                       request_sha256=q.digest(q.canonical(request).encode()),
                       project_authority_sha256=q.digest(q.encoded({pid:dict(project_id=pid,budget_cpu_seconds=60) for pid in result['projects'].values()})))
        diagnostics = q.encoded(dict(stdout=r['stdout'], stderr=r['stderr']))
        record = dict(r, output_sha256=q.digest(diagnostics), output_bytes=len(diagnostics), owner=identity['owner'], request=request, broker_pid=123,
                      payload_hash=q.digest(json.dumps(request, separators=(',', ':')).encode()), create_attempted=True, create_finished=True,
                      create_ambiguous=False, container_name='outfitter-' + q.digest((identity['owner'] + ':' + r['request_id']).encode())[:32])
        run = dict(id=r['request_id'], project_id=r['project_id'], environment=dict(image=r['image']),
                   status='succeeded', cleanup_verified=True, compute=dict(r['compute'], unresolved_liability=False), metrics=q.ORACLES[i])
        result['attempts'].append(dict(binding=binding, binding_sha256=q.digest(q.encoded(binding)), final_receipt=r,
            request=request, run=run, ledger_record=record, ledger_identity=identity, ledger_verified=True,
            container_absent=True, metrics=q.ORACLES[i], diagnostics_utf8=diagnostics.decode(),
            dispatch_boundary_entered=True, outcome='succeeded', observed_charged_cpu_seconds=30, observed_reserved_cpu_seconds=30))
    return copy.deepcopy(result)


class OracleTests(unittest.TestCase):
    def test_complete_bound_synthetic_observations_pass(self):
        self.assertTrue(q.accepted(complete_report(), synthetic_manifest()))

    def test_empty_partial_duplicate_extra_or_false_controls_fail(self):
        for change in ('empty', 'missing', 'extra', 'false'):
            report = complete_report()
            if change == 'empty': report['checks'] = {}
            elif change == 'missing': report['checks'].pop(next(iter(q.REQUIRED)))
            elif change == 'extra': report['checks']['unreviewed'] = True
            else: report['checks'][next(iter(q.REQUIRED))] = 1
            with self.subTest(change=change): self.assertFalse(q.accepted(report, synthetic_manifest()))

    def test_partial_duplicate_or_fifth_attempt_fails(self):
        for size in (0, 1, 3, 5):
            report = complete_report()
            report['attempts'] = (report['attempts'] + report['attempts'])[:size]
            self.assertFalse(q.accepted(report, synthetic_manifest()))
        report = complete_report(); report['attempts'][1] = copy.deepcopy(report['attempts'][0])
        self.assertFalse(q.accepted(report, synthetic_manifest()))

    def test_zero_partial_float_bool_refund_or_unknown_charge_fails(self):
        for value in (0, 1, 29, 30.0, True):
            for field in ('reserved_cpu_seconds', 'charged_cpu_seconds'):
                report = complete_report(); report['attempts'][0]['final_receipt']['compute'][field] = value
                with self.subTest(value=value, field=field): self.assertFalse(q.accepted(report, synthetic_manifest()))
        for field, value in (('unresolved_liability', True), ('cleanup_verified', False), ('status', 'unknown'), ('admitted', False)):
            report = complete_report(); report['attempts'][0]['final_receipt'][field] = value
            self.assertFalse(q.accepted(report, synthetic_manifest()))

    def test_binary_ledger_request_project_image_and_manifest_binding_failures(self):
        for field in ('broker_sha256', 'broker_source_commit', 'source_commit', 'source_archive_sha256', 'image_id', 'request_sha256', 'request_id', 'project_id', 'image', 'manifest_sha256'):
            report = complete_report(); binding = report['attempts'][0]['binding']; binding[field] = 'changed'
            report['attempts'][0]['binding_sha256'] = q.digest(q.encoded(binding))
            with self.subTest(field=field): self.assertFalse(q.accepted(report, synthetic_manifest()))
        for target, field in (('ledger_record', 'owner'), ('ledger_identity', 'authority_id'), ('request', 'request_id')):
            report = complete_report(); report['attempts'][0][target][field] = 'different'
            self.assertFalse(q.accepted(report, synthetic_manifest()))

    def test_generic_receipt_and_failure_marker_cannot_become_pass(self):
        report = complete_report(); report['attempts'][0]['final_receipt'] = dict(status='succeeded', cleanup_verified=True)
        self.assertFalse(q.accepted(report, synthetic_manifest()))
        report = complete_report(); report['failure'] = dict(type='KeyboardInterrupt')
        self.assertFalse(q.accepted(report, synthetic_manifest()))

    def test_dispatch_and_monotonic_accounting_observations_are_required(self):
        for field, value in (('dispatch_boundary_entered', False), ('outcome', 'DISPATCH_BOUNDARY_ENTERED'),
                             ('observed_charged_cpu_seconds', 0), ('observed_reserved_cpu_seconds', True),
                             ('charge_regression_observed', True)):
            report = complete_report(); report['attempts'][0][field] = value
            with self.subTest(field=field): self.assertFalse(q.accepted(report, synthetic_manifest()))

    def test_accounting_observation_cannot_erase_a_known_charge(self):
        audit = object.__new__(q.Audit); audit.report = complete_report(); audit.save = Mock()
        attempt = audit.report['attempts'][0]; binding = attempt['binding']; identity = binding['ledger_identity']
        data = dict(version=2, owner=identity['owner'], authority=dict(authority_id=identity['authority_id'],
                    state_path=binding['ledger_path'], admission='active'), runs={binding['request_id']: copy.deepcopy(attempt['ledger_record'])})
        data['runs'][binding['request_id']]['compute']['charged_cpu_seconds'] = 0
        audit.observe_accounting(data)
        self.assertEqual(audit.report['charged_cpu_seconds'], 120)
        self.assertEqual(attempt['observed_charged_cpu_seconds'], 30)
        self.assertTrue(attempt['charge_regression_observed'])
        data['runs'] = {}; audit.observe_accounting(data)
        self.assertEqual(audit.report['charged_cpu_seconds'], 120)
        self.assertIn('NO_RECORD_OBSERVED', attempt['accounting_observation'])
        self.assertFalse(q.accepted(audit.report, synthetic_manifest()))

    def test_ledger_or_application_disagreement_fails(self):
        for key in ('ledger_record', 'run'):
            report = complete_report(); report['attempts'][0][key]['compute']['charged_cpu_seconds'] = 0
            self.assertFalse(q.accepted(report, synthetic_manifest()))
        report = complete_report(); report['attempts'][0]['ledger_record']['create_ambiguous'] = True
        self.assertFalse(q.accepted(report, synthetic_manifest()))

    def test_independent_numeric_oracle_checks_both_output_and_saved_result(self):
        for key in ('metrics', 'run'):
            report = complete_report()
            if key == 'metrics': report['attempts'][0]['metrics']['groups'][0]['mean'] = 23
            else: report['attempts'][0]['run']['metrics']['mean'] = 0
            self.assertFalse(q.accepted(report, synthetic_manifest()))
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
        audit = object.__new__(q.Audit); audit.report = dict(attempts=[{}] * 4); audit.launch_count = 4
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
    def simulated(self, interrupt=False, interrupt_at=0, bad_charge=False, close_failure=False,
                  bad_diagnostics=None, project_budget=60, extra_project=False, absence_failure=False, export_mutation=None):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp).resolve(); binary = Path(sys.executable).resolve()
            stack.enter_context(patch.object(q, 'BROKER', binary))
            stack.enter_context(patch.object(q, 'BROKER_SHA', q.file_hash(binary)))
            manifest = dict(output=str(root / 'synthetic'), restart_marker=str(root / 'synthetic.started'),
                            source_commit='c' * 40, source_archive_sha256='d' * 64,
                            prerequisites=dict(images={k: dict(id='sha256:' + 'a' * 64) for k in q.IMAGES}))
            manifest['sources'] = q.source_hashes()
            audit = q.Audit(manifest, 'e' * 64)
            active = {}
            def event(root, role, boundary):
                owners = active.setdefault(threading.get_ident(), [])
                if boundary == 'opened': owners.append(role)
                elif boundary == 'closed': owners.remove(role)
            stack.enter_context(patch.object(sqlite_ownership, '_event', side_effect=event))
            artifact = audit.artifact
            def retained(name, content):
                self.assertEqual(active.get(threading.get_ident(), []), [], 'Evidence write inside SQLite owner')
                artifact(name, content)
            audit.artifact = retained
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
                    data['projects'][pid] = dict(project_id=pid, budget_cpu_seconds=project_budget)
                    if extra_project: data['projects']['extra'] = dict(project_id='extra', budget_cpu_seconds=60)
                    (target / 'ledger.json').write_bytes(q.encoded(data))
                return launch(write) if launch else write()
            stack.enter_context(patch.object(q.ObservedBroker, 'init_project', init_project))
            last = {}
            def spawn(command, **kwargs):
                self.assertEqual(command[:3], [str(binary), '--state-dir', str(audit.state / 'broker')])
                self.assertEqual(command[3:5], ['run', '--request'])
                index = len(audit.report['attempts']) - 1
                self.assertTrue((audit.root / f'{index + 1}-binding.private.json').is_file())
                if interrupt and index == interrupt_at: raise KeyboardInterrupt('synthetic interruption before external effect')
                request = q.strict_json(Path(command[5]).read_bytes())
                value = receipt(index, request['request_id'], request['project_id'])
                if bad_charge and index == 1: value['compute']['charged_cpu_seconds'] = 0
                data = audit.ledger()
                diagnostics = q.encoded(dict(stdout=value['stdout'], stderr=value['stderr']))
                if bad_diagnostics == 'different': diagnostics = q.encoded(dict(stdout='different valid diagnostics', stderr=''))
                output_hash = q.digest(diagnostics)
                output_dir = audit.state / 'broker/outputs'; output_dir.mkdir(exist_ok=True)
                (output_dir / (output_hash + '.json')).write_bytes(diagnostics)
                data['runs'][request['request_id']] = dict(value, output_sha256=output_hash, output_bytes=len(diagnostics),
                    owner=data['owner'], request=request, create_attempted=True,
                    create_finished=True, create_ambiguous=False, broker_pid=123,
                    payload_hash=q.digest(json.dumps(q.planned_request(index, request['request_id'], request['project_id'], str(audit.state / 'broker')), separators=(',', ':')).encode()), container_name='outfitter-' + q.digest((data['owner'] + ':' + request['request_id']).encode())[:32])
                if bad_diagnostics == 'missing': data['runs'][request['request_id']].pop('output_sha256')
                if bad_diagnostics == 'wrong-hash': data['runs'][request['request_id']]['output_sha256'] = '0' * 64
                if bad_diagnostics == 'wrong-size': data['runs'][request['request_id']]['output_bytes'] += 1
                (audit.state / 'broker/ledger.json').write_bytes(q.encoded(data))
                last['receipt'] = value
                return Mock(returncode=0)
            stack.enter_context(patch.object(runtime.subprocess, 'Popen', side_effect=spawn))
            stack.enter_context(patch.object(runtime, 'collect', side_effect=lambda *a, **k: (q.encoded(last['receipt']), b'')))
            def inspect(command, **kwargs):
                self.assertEqual(command[:3], ['docker', 'container', 'inspect'])
                return subprocess.CompletedProcess(command, 1, b'[]\n', b'Cannot connect to Docker daemon' if absence_failure
                                                   else ('Error response from daemon: No such container: ' + command[3] + '\n').encode())
            stack.enter_context(patch.object(q.subprocess, 'run', side_effect=inspect))
            stack.enter_context(patch.object(q, 'prerequisites', return_value=manifest['prerequisites']))
            if export_mutation:
                request_http = q.HTTP.request
                def altered_export(client, method, path, *args, **kwargs):
                    status, payload = request_http(client, method, path, *args, **kwargs)
                    if status != 200 or not path.endswith('/export'): return status, payload
                    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                        project = q.strict_json(archive.read('project.json'))
                        runs = q.strict_json(archive.read('runs.json'))
                        files = {name: archive.read(name) for name in archive.namelist() if name.startswith('runs/')}
                    succeeded = next(run for run in runs if run['status'] == 'succeeded')
                    prefix = 'runs/' + succeeded['id'] + '/'
                    if export_mutation == 'numeric':
                        succeeded['metrics']['mean'] += 1
                        for group in succeeded['metrics']['groups']: group['mean'] += 1
                        files[prefix + 'result.json'] = q.encoded(succeeded['metrics'])
                    elif export_mutation == 'unknown':
                        succeeded.update(status='failed', metrics={}, cleanup_verified=False,
                            compute=dict(reserved_cpu_seconds=None, charged_cpu_seconds=None, provenance='unknown', unresolved_liability=True))
                        files.pop(prefix + 'result.json')
                    elif export_mutation == 'project': project['budget_cpu_seconds'] = 600
                    elif export_mutation in ('input', 'recipe'):
                        name = 'input.csv' if export_mutation == 'input' else ('recipe.py' if succeeded['recipe'] == 'summary-python' else 'recipe.R')
                        files[prefix + name] += b'\n'
                        succeeded[export_mutation + '_sha256'] = q.digest(files[prefix + name])
                        if export_mutation == 'recipe':
                            succeeded['environment']['specification']['recipe_sha256'] = succeeded['recipe_sha256']
                    elif export_mutation == 'result-bytes': files[prefix + 'result.json'] += b'\n'
                    elif export_mutation == 'refusal':
                        refused = next(run for run in runs if run['status'] == 'cancelled')
                        refused['error'] = 'different refusal reason'
                    else: self.fail('Unexpected test mutation')
                    changed = build_bundle(project, runs, files)
                    self.assertTrue(q.verify_bundle(changed)['valid'], 'Mutation must remain structurally valid')
                    return status, changed
                stack.enter_context(patch.object(q.HTTP, 'request', altered_export))
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
            if close_failure:
                close = q.Server.server_close
                def bad_close(server):
                    close(server)
                    raise OSError('synthetic close acknowledgement failure')
                stack.enter_context(patch.object(q.Server, 'server_close', bad_close))
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
        self.assertEqual(report['attempts'][0]['outcome'], 'DISPATCH_BOUNDARY_ENTERED')
        self.assertFalse(q.accepted(report, synthetic_manifest()))


    def test_interruption_retains_prior_charge_and_no_resume_budget(self):
        report = self.simulated(interrupt=True, interrupt_at=1)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['charged_cpu_seconds'], 30)
        self.assertEqual(len(report['attempts']), 2)
        self.assertEqual(report['attempts'][0]['final_receipt']['compute']['charged_cpu_seconds'], 30)
        self.assertEqual(report['attempts'][1]['outcome'], 'DISPATCH_BOUNDARY_ENTERED')

    def test_partial_charge_stops_without_third_attempt_or_refund(self):
        report = self.simulated(bad_charge=True)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['charged_cpu_seconds'], 30)
        self.assertEqual(len(report['attempts']), 2)
        self.assertNotIn('final_receipt', report['attempts'][1])

    def test_cleanup_acknowledgement_failure_cannot_pass_after_four_good_runs(self):
        report = self.simulated(close_failure=True)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['charged_cpu_seconds'], 120)
        self.assertEqual(report['failure']['type'], 'OSError')
        self.assertFalse(q.accepted(report, synthetic_manifest()))


    def test_diagnostics_missing_wrong_hash_size_or_streams_refuse(self):
        for mutation in ('missing', 'wrong-hash', 'wrong-size', 'different'):
            with self.subTest(mutation=mutation):
                report = self.simulated(bad_diagnostics=mutation)
                self.assertEqual(report['status'], 'FAIL')
                self.assertEqual(len(report['attempts']), 1)
                self.assertEqual(report['charged_cpu_seconds'], 30)
                self.assertFalse(report['accounting_complete'])

    def test_exact_project_budgets_and_population_refuse_before_dispatch(self):
        for arguments in (dict(project_budget=600), dict(extra_project=True)):
            report = self.simulated(**arguments)
            self.assertEqual(report['status'], 'FAIL')
            self.assertEqual(len(report['attempts']), 0)
            self.assertEqual(report['charged_cpu_seconds'], 0)

    def test_absence_observation_failure_preserves_known_full_charge(self):
        report = self.simulated(absence_failure=True)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(len(report['attempts']), 1)
        self.assertEqual(report['charged_cpu_seconds'], 30)
        self.assertFalse(report['accounting_complete'])
        attempt = report['attempts'][0]
        self.assertTrue(attempt['dispatch_boundary_entered'])
        self.assertEqual(attempt['observed_charged_cpu_seconds'], 30)
        self.assertTrue(attempt['ledger_cleanup_verified_observation'])
        self.assertEqual(attempt['outcome'], 'ACCOUNTING_OBSERVED_CLEANUP_NOT_INDEPENDENTLY_VERIFIED')
        self.assertNotIn('container_absent', attempt)

    def test_structurally_valid_exports_must_match_captured_http_and_bytes(self):
        for mutation in ('numeric', 'unknown', 'project', 'input', 'recipe', 'result-bytes', 'refusal'):
            with self.subTest(mutation=mutation):
                report = self.simulated(export_mutation=mutation)
                self.assertEqual(report['status'], 'FAIL')
                self.assertEqual(len(report['attempts']), 4)
                self.assertEqual(report['charged_cpu_seconds'], 120)
                self.assertIn('Export ', report['failure']['message'])
                self.assertFalse(report['accounting_complete'])
                self.assertNotIn('first_export', report['checks'])


if __name__ == '__main__': unittest.main()
