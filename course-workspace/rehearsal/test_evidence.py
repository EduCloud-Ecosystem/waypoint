"""The success artifact is a gate, not an unconditional report formatter."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evidence import collect_provenance, write_pass, REQUIRED_PHASES, FIXTURE_VERSION, FIXTURE_FILES, dashboard_digest


def report(scope='local-subset'):
    result = {
        'provenance': {
            'cairn': {'revision': 'a' * 40, 'dirty': False},
            'waypoint': {'revision': 'b' * 40, 'dirty': False},
            'cairn_binary_sha256': 'c' * 64, 'lti_binary_sha256': 'd' * 64,
            'dashboard_bundle_sha256': 'f' * 64, 'dashboard_file_count': 3,
            'axe_core_sha256': 'a' * 64,
            'fixture_version': FIXTURE_VERSION,
            'fixture_files_sha256': {k: 'e' * 64 for k in FIXTURE_FILES},
            'image_ids': {k: 'sha256:' + 'f' * 64 for k in
                          ('course', 'checkout', 'forgejo', 'keycloak', 'nginx', 'hub')},
            'hardware': {'os': 'Linux', 'architecture': 'x86_64', 'cpu_count': 4, 'memory_bytes': 8000000000},
            'runtime': {'python': '3.12.0', 'docker_server': '29.4.3'},
        },
        'phases': {name: True for name in REQUIRED_PHASES[scope]},
        'started_at': '2026-10-11T10:00:00Z', 'finished_at': '2026-10-11T10:05:00Z',
        'duration_seconds': 300.0, 'checks': ['synthetic-fixture'],
        'metrics': {'distinct_users': 15, 'p95_seconds': 0.15},
    }
    if scope == 'full-linux':
        result['course_manifest_sha256'] = 'a' * 64
    return result


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def rejected(self, value, scope='local-subset', cleanup=True):
        with self.assertRaises((ValueError, TypeError)):
            write_pass(self.root, value, scope, cleanup)
        self.assertFalse((self.root / 'PASS.json').exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_success_scope_permissions_and_no_overwrite(self):
        path = write_pass(self.root, report(), 'local-subset', True)
        original = path.read_bytes()
        saved = json.loads(original)
        self.assertEqual(saved['scope'], 'local-subset')
        self.assertEqual(saved['status'], 'PASS')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            write_pass(self.root, report(), 'local-subset', True)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_every_required_phase_gates_publication(self):
        for scope, phases in REQUIRED_PHASES.items():
            for phase in phases:
                with self.subTest(scope=scope, phase=phase):
                    value = report(scope)
                    del value['phases'][phase]
                    self.rejected(value, scope)
                    value = report(scope)
                    value['phases'][phase] = False
                    self.rejected(value, scope)

    def test_cleanup_requires_true_not_truthiness(self):
        for cleanup in (False, None, 1, 'yes'):
            self.rejected(report(), cleanup=cleanup)

    def test_local_cannot_claim_full(self):
        self.rejected(report(), 'full-linux')
        value = report('full-linux')
        value['provenance']['hardware']['os'] = 'Darwin'
        self.rejected(value, 'full-linux')

    def test_full_manifest_digest_required_and_local_may_not_claim_it(self):
        value = report('full-linux')
        del value['course_manifest_sha256']
        self.rejected(value, 'full-linux')
        for invalid in ('unknown', 'a' * 63, '/private/manifest.json', None):
            value = report('full-linux')
            value['course_manifest_sha256'] = invalid
            self.rejected(value, 'full-linux')
        value = report()
        value['course_manifest_sha256'] = 'a' * 64
        self.rejected(value)

    def test_no_env_raw_logs_or_credentials(self):
        for key in ('environment', 'logs', 'api_key', 'raw_report'):
            value = report()
            value[key] = 'secret'
            self.rejected(value)
        value = report()
        value['checks'] = ['https://user:secret@example.invalid']
        self.rejected(value)
        value = report()
        value['metrics'] = {'request_body': {'password': 'secret'}}
        self.rejected(value)

    def test_provenance_is_complete_and_allowlisted(self):
        for key in report()['provenance']:
            value = report()
            del value['provenance'][key]
            self.rejected(value)
        value = report()
        value['provenance']['image_ids'].pop('checkout')
        self.rejected(value)
        value = report()
        value['provenance']['hardware']['hostname'] = 'private-device'
        self.rejected(value)
        value = report()
        value['provenance']['cairn_binary_sha256'] = 'unknown'
        self.rejected(value)

    def test_every_executed_fixture_module_is_required(self):
        for module in ('assessment_passback.py', 'course_load.py', 'webhook_relay.py', 'accessibility.py'):
            value = report()
            del value['provenance']['fixture_files_sha256'][module]
            self.rejected(value)

    def test_dashboard_digest_tracks_assets_and_rejects_links(self):
        dashboard = self.root / 'dist'
        dashboard.mkdir()
        with self.assertRaises(ValueError):
            dashboard_digest(dashboard)
        (dashboard / 'index.html').write_text('fixture html')
        asset = dashboard / 'app.js'
        asset.write_text('before')
        before, count = dashboard_digest(dashboard)
        self.assertEqual(count, 2)
        asset.write_text('after')
        self.assertNotEqual(dashboard_digest(dashboard)[0], before)
        asset.unlink()
        asset.symlink_to(dashboard / 'index.html')
        with self.assertRaises(ValueError):
            dashboard_digest(dashboard)

    def test_invalid_timing_and_unbounded_fields(self):
        for duration in (float('nan'), float('inf'), -1, True):
            value = report()
            value['duration_seconds'] = duration
            self.rejected(value)
        value = report()
        value['finished_at'] = '2026-10-11T09:00:00Z'
        self.rejected(value)
        value = report()
        value['started_at'] = '2026-10-11T10:00:00'
        self.rejected(value)
        value = report()
        value['checks'] = ['valid'] * 201
        self.rejected(value)

    def test_output_size_bound_fails_closed(self):
        with patch('evidence.MAX_BYTES', 100):
            self.rejected(report())
        value = report()
        value['metrics'] = {'huge': 10**1000}
        self.rejected(value)
        value = report()
        value['metrics'] = {f'measure_{i}': i for i in range(201)}
        self.rejected(value)

    def test_failed_atomic_publish_leaves_no_partial_artifact(self):
        with patch('evidence.os.link', side_effect=OSError('simulated write failure')):
            with self.assertRaises(OSError):
                write_pass(self.root, report(), 'local-subset', True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_output_directory_must_be_private_and_not_symlink(self):
        os.chmod(self.root, 0o755)
        self.rejected(report())
        os.chmod(self.root, 0o700)
        link = self.root / 'alias'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            write_pass(link, report(), 'local-subset', True)
        self.assertFalse((self.root / 'PASS.json').exists())

    def test_collect_reads_only_allowlisted_commands_and_no_environment_dump(self):
        binary = self.root / 'binary'
        binary.write_bytes(b'synthetic-executable')
        dashboard = self.root / 'web' / 'dist'
        dashboard.mkdir(parents=True)
        (dashboard / 'index.html').write_text('synthetic dashboard')
        axe = self.root / 'web' / 'node_modules' / 'axe-core' / 'axe.min.js'
        axe.parent.mkdir(parents=True)
        axe.write_bytes(b'synthetic axe execution asset')
        calls = []

        def command(*args):
            calls.append(args)
            if args[0] == 'git':
                return 'a' * 40 if args[-1] == 'HEAD' else ''
            if args[:3] == ('docker', 'image', 'inspect'):
                return 'sha256:' + 'b' * 64
            if args[0] == 'sysctl':
                return '8000000000'
            return '29.4.3'

        with patch('evidence._command', side_effect=command), patch.dict(os.environ, {'PRIVATE_TEST_KEY': 'never-copy-this'}):
            value = collect_provenance(self.root, binary, binary, waypoint_source=self.root,
                                       images={'course': 'operator/course:fixture'})
        self.assertNotIn('never-copy-this', json.dumps(value))
        self.assertNotIn(str(self.root), json.dumps(value))
        self.assertNotIn('operator/course', json.dumps(value))
        self.assertEqual(set(value['image_ids']), {'course'})
        self.assertEqual(set(value['fixture_files_sha256']), FIXTURE_FILES)
        self.assertEqual(value['dashboard_file_count'], 1)
        self.assertEqual(value['axe_core_sha256'], hashlib.sha256(axe.read_bytes()).hexdigest())
        with patch('evidence._command', side_effect=command):
            axe.write_bytes(b'changed executed asset')
            changed = collect_provenance(self.root, binary, binary, waypoint_source=self.root)
            self.assertNotEqual(value['axe_core_sha256'], changed['axe_core_sha256'])
            axe.unlink()
            with self.assertRaises(FileNotFoundError):
                collect_provenance(self.root, binary, binary, waypoint_source=self.root)
        self.assertTrue(all(args[0] in ('git', 'docker', 'sysctl') for args in calls))


if __name__ == '__main__':
    unittest.main()
