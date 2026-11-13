"""Shared-runtime fixtures reuse exact built bytes; no Docker or network."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from notebook import Notebook
import verify_multiple_courses
import verify_resource_amendment
import verify_cutover


HUB = 'sha256:' + 'a' * 64
COURSE = 'sha256:' + 'b' * 64


class SharedNotebookImagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = Notebook(self.root / 'source')
        self.source.root.mkdir()
        self.source.homes = self.source.root / 'homes'; self.source.homes.mkdir()
        self.source.env = {'WORKSPACE_PORT': '18000', 'WORKSPACE_OIDC_CLIENT_SECRET': 'synthetic-source-secret'}
        with patch('notebook.run', side_effect=[HUB, COURSE]):
            self.source.render_setup()
        # Represent completed startup without mounting a disk or starting Docker.
        self.source.mounted = True; self.source._startup_complete = True
        self.source.report.append('course-manifest-startup-preflight')
        self.peer = Notebook(self.root / 'peer', domain='two.rehearsal.test',
                             auth_domain='authtwo.rehearsal.test', client_id='course-two')
        self.peer.root.mkdir()

    def test_reuse_skips_second_build_and_preserves_distinct_fixture_authority(self):
        before = {name: getattr(self.peer, name) for name in
                  ('instance', 'domain', 'auth_domain', 'client_id', 'password', 'network', 'original_dns', 'deployment')}
        self.assertIs(self.source.original_dns, self.peer.original_dns)
        self.source.compose = Mock(); self.source.prepare_images()
        self.source.compose.assert_called_once_with('--profile', 'build', 'build')
        self.peer.reuse_images_from(self.source)
        self.assertEqual(self.peer.requested_hub_image, HUB)
        self.assertEqual(self.peer.image, COURSE)
        self.assertEqual(before, {name: getattr(self.peer, name) for name in before})
        self.assertNotEqual(self.source.instance, self.peer.instance)
        self.peer.compose = Mock(); self.peer.prepare_images(); self.peer.compose.assert_not_called()
        self.peer.homes = self.peer.root / 'homes'; self.peer.homes.mkdir()
        self.peer.env = {'WORKSPACE_PORT': '18001', 'WORKSPACE_OIDC_CLIENT_SECRET': 'synthetic-peer-secret'}
        with patch('notebook.run', side_effect=[HUB, COURSE]) as inspect:
            self.peer.render_setup()
        self.assertEqual([call.args[3] for call in inspect.call_args_list], [HUB, COURSE])
        self.assertEqual(self.peer.manifest['images'], self.source.manifest['images'])
        self.assertNotEqual(self.peer.manifest['identity'], self.source.manifest['identity'])
        with patch('notebook.preflight', return_value={'status': 'ready', 'images': {'hub': HUB, 'course': COURSE}}) as preflight:
            self.peer.verify_startup_preflight(self.peer.deployment)
        preflight.assert_called_once_with(self.peer.deployment)

    def test_actual_recipient_inspection_and_preflight_still_reject_other_bytes(self):
        self.peer.reuse_images_from(self.source)
        self.peer.hub_image, self.peer.image = HUB, COURSE
        with patch('notebook.preflight', return_value={'status': 'ready', 'images': {'hub': HUB, 'course': 'sha256:' + 'c' * 64}}):
            with self.assertRaisesRegex(AssertionError, 'preflight'):
                self.peer.verify_startup_preflight(self.peer.deployment)

    def test_source_must_have_completed_startup_and_preflight_and_still_be_open(self):
        for field, value in (('_startup_complete', False), ('mounted', False), ('report', [])):
            old = getattr(self.source, field); setattr(self.source, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.peer.reuse_images_from(self.source)
            setattr(self.source, field, old)
        for source in (self.peer, object()):
            with self.assertRaises(ValueError): self.peer.reuse_images_from(source)
        self.assertIsNone(self.peer.requested_hub_image)

    def test_source_manifest_changes_or_detached_in_memory_pins_refuse_atomically(self):
        original = self.source.manifest_path.read_bytes()
        self.source.manifest_path.write_bytes(original + b'\n')
        with self.assertRaises(AssertionError): self.peer.reuse_images_from(self.source)
        self.source.manifest_path.write_bytes(original)
        for field, value in (('hub_image', 'sha256:' + 'c' * 64), ('image', 'course:latest'),
                             ('manifest', dict(self.source.manifest, images={'hub': HUB, 'course': HUB}))):
            previous = getattr(self.source, field); setattr(self.source, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.peer.reuse_images_from(self.source)
            setattr(self.source, field, previous)
        self.assertIsNone(self.peer.requested_hub_image)
        self.assertEqual(self.peer.image, 'educloud-python-r:2026-10-06')

    def test_mutable_or_registry_source_ids_refuse_even_when_manifest_is_rebound(self):
        for bad in ('course:latest', 'registry.example/course@sha256:' + 'c' * 64):
            manifest = copy.deepcopy(self.source.manifest); manifest['images']['course'] = bad
            self.source.manifest = manifest; self.source.image = bad
            self.source.manifest_path.write_text(json.dumps(manifest)); self.source.freeze_manifest()
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.peer.reuse_images_from(self.source)
        self.assertIsNone(self.peer.requested_hub_image)

    def test_partial_started_rendered_and_preconfigured_recipients_refuse(self):
        for field, value in (('_start_attempted', True), ('mounted', True),
                             ('manifest_path', self.peer.root / 'manifest.json'),
                             ('env', {}), ('requested_hub_image', HUB),
                             ('synthetic_candidate_images', True), ('image', 'different:tag')):
            existed = hasattr(self.peer, field); previous = getattr(self.peer, field, None)
            setattr(self.peer, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.peer.reuse_images_from(self.source)
            if existed: setattr(self.peer, field, previous)
            else: delattr(self.peer, field)
        configured = Notebook(self.root / 'configured', hub_image=HUB, course_image=COURSE)
        with self.assertRaises(ValueError): configured.reuse_images_from(self.source)
        self.assertEqual((configured.requested_hub_image, configured.image), (HUB, COURSE))
        self.peer.reuse_images_from(self.source)
        with self.assertRaises(ValueError): self.peer.reuse_images_from(self.source)

    def test_failed_start_attempt_cannot_leave_source_ready_or_recipient_fresh(self):
        with patch('notebook.sys.platform', 'not-linux'):
            with self.assertRaises(ValueError): self.peer.start()
            with self.assertRaises(ValueError): self.source.start()
        self.assertTrue(self.peer._start_attempted)
        self.assertFalse(self.source._startup_complete)
        with self.assertRaises(ValueError): self.peer.reuse_images_from(self.source)

    def test_three_callers_pin_after_first_success_before_second_start_and_keep_cleanup(self):
        for module in (verify_multiple_courses, verify_resource_amendment, verify_cutover):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); events = []
                first = Mock(instance='rehearsal-one'); peer = Mock(instance='rehearsal-two')
                first.start.side_effect = lambda: events.append('first-ready')
                peer.reuse_images_from.side_effect = lambda source: events.append(('pins', source))
                def stop_after_second_start():
                    events.append('second-start'); raise RuntimeError('stop hermetic fixture before services')
                peer.start.side_effect = stop_after_second_start
                cleanup_name = 'cleanup_cutover' if module is verify_cutover else 'cleanup_courses'
                argv = [module.__name__ + '.py', '--output', str(root / 'run')]
                with patch.object(module.sys, 'argv', argv), patch.object(module.sys, 'platform', 'linux'), \
                        patch.object(module.os, 'geteuid', return_value=0), patch.object(module, 'AUTHORITY', root / 'authority'), \
                        patch.object(module, '_source', return_value={'revision': 'a' * 40, 'dirty': False}), \
                        patch.object(module, 'sha256_file', return_value='b' * 64), \
                        patch.object(module, 'Notebook', side_effect=[first, peer]), \
                        patch.object(module, cleanup_name, return_value=True) as cleanup, \
                        patch.object(module, 'publish_pass') as publish, \
                        patch.dict(sys.modules, {'allocation': types.ModuleType('allocation'),
                                                'resource_amendment': types.ModuleType('resource_amendment')}):
                    with self.assertRaisesRegex(RuntimeError, 'stop hermetic fixture'):
                        module.main()
                self.assertEqual(events, ['first-ready', ('pins', first), 'second-start'])
                cleanup.assert_called_once(); publish.assert_not_called()


if __name__ == '__main__': unittest.main()
