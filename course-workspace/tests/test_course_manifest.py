from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'hub')]
from course_manifest import (MAX_MANIFEST_BYTES, ManifestError, build_configuration,
                             load_manifest, main, render_manifest, validate_manifest, validation_record)
from deployment import read_environment, validate_institutional
from health import load_config as load_health
from recovery import load_config as load_recovery


class CourseManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='manifest-test-')
        self.root = Path(self.temporary.name).resolve()
        self.output = self.root / 'deployment'
        self.client_secret = 'synthetic-client-secret'
        self.backup_secret = 'synthetic-backup-password-32chars'
        for filename, value in [('oidc-secret', self.client_secret), ('backup-secret', self.backup_secret)]:
            target = self.root / filename
            target.write_text(value + '\n')
            target.chmod(0o600)
        self.manifest = {
            'version': 1, 'scope': 'synthetic', 'instance': 'course-test',
            'images': {'hub': 'sha256:' + 'a' * 64, 'course': 'registry.example.test:5000/course@sha256:' + 'b' * 64},
            'identity': {'issuer': 'https://auth.example.test:14443/realms/course', 'client_id': 'course',
                         'client_secret_file': str(self.root / 'oidc-secret'), 'subjects': ['subject-b', 'subject-a']},
            'workspace': {'origin': 'https://work.example.test:24443', 'port': 18000},
            'storage': {'home_root': str(self.root / 'homes'), 'quota_mb': 256, 'inode_limit': 100000},
            'capacity': {'memory_mb': 1024, 'cpu': 1, 'active_limit': 2, 'idle_seconds': 1800, 'max_age_seconds': 28800},
            'backup': {'repository': str(self.root / 'restic'), 'password_file': str(self.root / 'backup-secret'),
                       'staging_dir': str(self.output), 'keep_last': 7, 'keep_daily': 7, 'keep_weekly': 4},
            'cairn': {'enabled': True, 'origin': 'http://127.0.0.1:28000', 'classroom_id': 'classroom-123',
                      'workspace_url': 'https://work.example.test:24443',
                      'max_files': 32, 'max_file_bytes': 1048576, 'max_total_bytes': 4194304},
            'owners': {'support': 'Synthetic support', 'restore': 'Synthetic restore'},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def refused(self, manifest, field=None):
        problems = validate_manifest(manifest)
        self.assertTrue(problems)
        if field:
            self.assertIn(field, [p['field'] for p in problems])
        rendered = json.dumps(problems)
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn('subject-a', rendered)
        self.assertNotIn(self.client_secret, rendered)
        return problems

    def test_rendered_private_configuration_agrees_with_existing_consumers(self):
        self.assertEqual(validate_manifest(self.manifest), [])
        record = render_manifest(self.manifest, self.output)
        self.assertEqual(record['status'], 'valid')
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for file in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        env = read_environment(self.output / '.env')
        self.assertEqual(env['WORKSPACE_HUB_IMAGE'], self.manifest['images']['hub'])
        self.assertEqual(env['WORKSPACE_IMAGE'], self.manifest['images']['course'])
        self.assertEqual(env['WORKSPACE_OIDC_CLIENT_SECRET'], self.client_secret)
        self.assertEqual(env['WORKSPACE_PUBLIC_URL'], self.manifest['workspace']['origin'])
        self.assertEqual(env['WORKSPACE_CAIRN_LOCAL_TEST'], '1')
        self.assertEqual(validate_institutional(env)['subjects'], {'subject-a', 'subject-b'})
        health = load_health(self.output / 'health.json')
        self.assertEqual(health['staging_dir'], str(self.output))
        self.assertEqual(health['home_root'], self.manifest['storage']['home_root'])
        recovery = load_recovery(self.output / 'recovery.json')
        self.assertTrue(recovery['local_test'])
        self.assertEqual(recovery['repository'], self.manifest['backup']['repository'])
        client = json.loads((self.output / 'keycloak-client.json').read_text())
        self.assertEqual(client['redirectUris'], [self.manifest['workspace']['origin'] + '/hub/oauth_callback'])
        self.assertIn('https://$host:24443$request_uri', (self.output / 'nginx.conf').read_text())
        self.assertIn('listen 24443 ssl;', (self.output / 'nginx.conf').read_text())
        normalized = json.loads((self.output / 'manifest.json').read_text())
        self.assertEqual(normalized['identity']['subjects'], ['subject-a', 'subject-b'])
        for name in ['manifest.json', 'validation.json', 'keycloak-client.json', 'cairn-binding.json']:
            raw = (self.output / name).read_text()
            self.assertNotIn(self.client_secret, raw)
            self.assertNotIn(self.backup_secret, raw)

    def test_institutional_requires_offhost_and_https_cairn_and_can_disable_binding(self):
        value = deepcopy(self.manifest)
        value['scope'] = 'institutional'
        self.refused(value, 'backup.repository')
        self.refused(value, 'cairn.origin')
        value['backup']['repository'] = 'sftp:backup-course:/srv/educloud/course'
        value['cairn'] = {'enabled': False}
        self.assertEqual(validate_manifest(value), [])
        files = build_configuration(value, self.output, self.client_secret)
        self.assertNotIn('WORKSPACE_CAIRN_ORIGIN', files['.env'])
        self.assertFalse(json.loads(files['recovery.json'])['local_test'])
        self.assertFalse(json.loads(files['cairn-binding.json'])['enabled'])

    def test_unknown_embedded_secret_and_non_object_sections_are_safe_refusals(self):
        for mutate in [lambda v: v.update(api_key='SENTINEL-PRIVATE-KEY'),
                       lambda v: v['identity'].update(client_secret='SENTINEL-PRIVATE-KEY'),
                       lambda v: v.update(identity=[]), lambda v: v.update(cairn=None),
                       lambda v: v['cairn'].update(token='SENTINEL-PRIVATE-KEY')]:
            value = deepcopy(self.manifest)
            mutate(value)
            issues = self.refused(value)
            self.assertNotIn('SENTINEL-PRIVATE-KEY', json.dumps(issues))

    def test_budgets_roster_ownership_binding_and_resource_mismatches_refuse(self):
        cases = [('cairn', 'max_files', 33), ('cairn', 'max_file_bytes', 1048577),
                 ('cairn', 'max_total_bytes', 4194305), ('cairn', 'workspace_url', 'https://other.example.test'),
                 ('owners', 'restore', ''), ('identity', 'subjects', ['student@example.test']),
                 ('identity', 'subjects', ['subject-a', 'subject-a']), ('capacity', 'cpu', float('inf')),
                 ('capacity', 'memory_mb', 10**400), ('capacity', 'active_limit', True),
                 ('workspace', 'origin', 'http://127.0.0.1:18000'),
                 ('identity', 'issuer', 'https://user:SECRET@auth.example.test/realms/a')]
        for section, key, setting in cases:
            with self.subTest(section=section, key=key):
                value = deepcopy(self.manifest)
                value[section][key] = setting
                self.refused(value, section + '.' + key)
        value = deepcopy(self.manifest)
        value['capacity']['idle_seconds'] = value['capacity']['max_age_seconds'] + 1
        self.refused(value, 'capacity.idle_seconds')

    def test_mutable_or_credential_bearing_images_are_refused(self):
        for reference in ['course:latest', 'example.test/course:v1@sha256:' + 'a'*64,
                          'https://user:SECRET@example.test/course@sha256:' + 'a'*64,
                          'example.test/../course@sha256:' + 'a'*64, 'sha256:' + 'A'*64,
                          'example.test/.-_@sha256:' + 'a'*64]:
            value = deepcopy(self.manifest)
            value['images']['hub'] = reference
            self.refused(value, 'images.hub')

    def test_unsafe_paths_overlaps_and_symlink_ancestors_are_refused(self):
        for path in ['/srv/../homes', '/srv//homes', '/', '/srv', '/' + 'x' * 600, str(self.root / 'homes') + '/']:
            value = deepcopy(self.manifest)
            value['storage']['home_root'] = path
            self.refused(value, 'storage.home_root')
        for section, key in [('storage', 'home_root'), ('backup', 'staging_dir'), ('backup', 'repository')]:
            value = deepcopy(self.manifest)
            value[section][key] = '/' + 'x' * 600
            self.refused(value, section + '.' + key)
            value = deepcopy(self.manifest)
            value[section][key] = str(self.root / 'oidc-secret')
            self.refused(value, section + '.' + key)
        value = deepcopy(self.manifest)
        value['backup']['repository'] = str(self.output / 'restic')
        self.refused(value)
        actual = self.root / 'actual'
        actual.mkdir()
        link = self.root / 'linked'
        link.symlink_to(actual, target_is_directory=True)
        value = deepcopy(self.manifest)
        value['identity']['client_secret_file'] = str(link / 'secret')
        self.refused(value, 'identity.client_secret_file')

    def test_secret_permissions_symlink_fifo_and_literals_never_publish(self):
        secret = self.root / 'oidc-secret'
        for value in ['SECRET with spaces', 'SECRET$HOME', 'SECRET\nSECONDLINE', "SECRET'quoted", '']:
            secret.write_text(value)
            with self.assertRaises(ManifestError) as error:
                render_manifest(self.manifest, self.output)
            self.assertEqual(error.exception.issues[0]['code'], 'secret')
            self.assertNotIn('SECRET', str(error.exception))
            self.assertFalse(self.output.exists())
        secret.write_text(self.client_secret)
        secret.chmod(0o644)
        with self.assertRaises(ManifestError):
            render_manifest(self.manifest, self.output)
        secret.unlink()
        secret.symlink_to(self.root / 'backup-secret')
        with self.assertRaises(ManifestError):
            render_manifest(self.manifest, self.output)
        secret.unlink()
        os.mkfifo(secret, mode=0o600)
        with self.assertRaises(ManifestError):
            render_manifest(self.manifest, self.output)
        self.assertFalse(self.output.exists())

    def test_backup_password_bound_and_staging_output_agreement(self):
        password = self.root / 'backup-secret'
        password.write_text('too-short')
        with self.assertRaises(ManifestError) as error:
            render_manifest(self.manifest, self.output)
        self.assertEqual(error.exception.issues[0]['field'], 'backup.password_file')
        value = deepcopy(self.manifest)
        value['backup']['staging_dir'] = str(self.root / 'wrong-staging')
        with self.assertRaises(ManifestError) as error:
            build_configuration(value, self.output, self.client_secret)
        self.assertEqual(error.exception.issues[0]['field'], 'backup.staging_dir')

    def test_existing_output_and_failed_render_preserve_other_content(self):
        self.output.mkdir()
        marker = self.output / 'existing'
        marker.write_text('preserve')
        with self.assertRaises(ManifestError):
            render_manifest(self.manifest, self.output)
        self.assertEqual(marker.read_text(), 'preserve')
        marker.unlink()
        self.output.rmdir()
        with patch('course_manifest._publish_directory', side_effect=OSError('SENTINEL-SECRET')):
            with self.assertRaises(ManifestError) as error:
                render_manifest(self.manifest, self.output)
        self.assertNotIn('SENTINEL-SECRET', str(error.exception))
        self.assertFalse(self.output.exists())
        self.assertFalse(list(self.root.glob('.course-manifest-*')))
        self.assertFalse(list(self.root.glob('*.course-manifest-lock')))

    def test_concurrently_created_empty_output_is_never_replaced(self):
        from course_manifest import _publish_directory
        def racing_publish(staging, output):
            output.mkdir()
            _publish_directory(staging, output)
        with patch('course_manifest._publish_directory', side_effect=racing_publish):
            with self.assertRaises(ManifestError) as error:
                render_manifest(self.manifest, self.output)
        self.assertEqual(error.exception.issues[0]['code'], 'output')
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(list(self.root.glob('.course-manifest-*')))

    def test_bounded_unique_json_and_cli_refusals_never_echo_input(self):
        source = self.root / 'manifest.json'
        for raw in ['{"version":1,"version":2,"token":"SENTINEL-SECRET"}', '{"SENTINEL-SECRET"',
                    '[]', '{"value":NaN}', ' ' * (MAX_MANIFEST_BYTES + 1)]:
            source.write_text(raw)
            with self.assertRaises(ManifestError):
                load_manifest(source)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                self.assertEqual(main(['check', '--manifest', str(source)]), 1)
            self.assertNotIn('SENTINEL-SECRET', captured.getvalue())
            self.assertEqual(json.loads(captured.getvalue())['issues'][0]['code'], 'parse')
        source.write_text(json.dumps(self.manifest))
        self.assertEqual(load_manifest(source), self.manifest)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            self.assertEqual(main(['check', '--manifest', str(source), '--output', str(self.root / 'other-output')]), 1)
        self.assertEqual(json.loads(captured.getvalue())['issues'][0]['field'], 'backup.staging_dir')
        self.assertNotIn(str(self.root), captured.getvalue())
        source.unlink()
        os.mkfifo(source, mode=0o600)
        with self.assertRaises(ManifestError):
            load_manifest(source)

    def test_check_does_not_read_secrets_or_proclaim_live_readiness(self):
        (self.root / 'oidc-secret').unlink()
        self.assertEqual(validate_manifest(self.manifest), [])
        record = validation_record(self.manifest)
        self.assertEqual(record['scope'], 'offline-configuration-check')
        self.assertNotIn(str(self.root), json.dumps(record))
        self.assertNotIn('subject-', json.dumps(record))
        self.assertIn('No host', record['limits'][0])


if __name__ == '__main__':
    unittest.main()
