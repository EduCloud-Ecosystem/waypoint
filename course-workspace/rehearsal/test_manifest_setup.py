"""The Linux fixture must consume rendered manifest pins and real classroom data."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from notebook import Notebook
from course_manifest import ManifestError


class ManifestSetupTests(unittest.TestCase):
    def fixture(self, root):
        book = Notebook(root)
        book.homes = root / 'homes'
        book.homes.mkdir()
        book.env = {'WORKSPACE_PORT': '18000', 'WORKSPACE_OIDC_CLIENT_SECRET': 'synthetic-oidc-secret'}
        with patch('notebook.run', side_effect=['sha256:' + 'a' * 64, 'sha256:' + 'b' * 64]):
            book.render_setup()
        return book

    def test_render_consumes_immutable_images_and_keeps_secrets_out_of_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            book = self.fixture(Path(directory).resolve())
            self.assertEqual(book.env['WORKSPACE_HUB_IMAGE'], 'sha256:' + 'a' * 64)
            self.assertEqual(book.env['WORKSPACE_IMAGE'], 'sha256:' + 'b' * 64)
            self.assertEqual(book.env['WORKSPACE_PUBLIC_URL'], book.origin)
            self.assertEqual(book.env_file, book.deployment / '.env')
            self.assertEqual(book.manifest['backup']['staging_dir'], str(book.deployment))
            self.assertNotIn('synthetic-oidc-secret', book.manifest_path.read_text())
            self.assertNotIn('synthetic-oidc-secret', (book.deployment / 'validation.json').read_text())
            self.assertEqual(book.manifest_digest, hashlib.sha256(book.manifest_path.read_bytes()).hexdigest())
            book.verify_manifest_frozen()
            book.manifest_path.write_text('{}')
            with self.assertRaisesRegex(AssertionError, 'manifest changed'):
                book.verify_manifest_frozen()

    def test_real_identity_and_proxy_fixture_use_generated_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            book = self.fixture(Path(directory).resolve())
            generated_client = json.loads((book.deployment / 'keycloak-client.json').read_text())
            generated_client['name'] = 'Rendered operator client marker'
            (book.deployment / 'keycloak-client.json').write_text(json.dumps(generated_client))
            with patch('notebook.run') as run:
                book.configure_fixture_edges()
            client = json.loads((book.root / 'realm.json').read_text())['clients'][0]
            self.assertEqual(client['name'], 'Rendered operator client marker')
            self.assertEqual(client['secret'], book.env['WORKSPACE_OIDC_CLIENT_SECRET'])
            self.assertEqual(client['redirectUris'], [book.origin + '/hub/oauth_callback'])
            self.assertEqual(client['attributes']['pkce.code.challenge.method'], 'S256')
            self.assertFalse(client['implicitFlowEnabled'])
            proxy = (book.root / 'nginx.conf').read_text()
            self.assertIn('listen ' + str(book.port) + ' ssl', proxy)
            self.assertIn('http://' + book.instance + '-hub:8000', proxy)
            self.assertNotIn('http://127.0.0.1:' + book.env['WORKSPACE_PORT'], proxy)
            self.assertIn('ssl_reject_handshake on', proxy)
            self.assertIn('server_name work.rehearsal.test *.work.rehearsal.test', proxy)
            self.assertIn('course-manifest-identity-consumed', book.report)
            self.assertIn('course-manifest-proxy-consumed', book.report)
            self.assertIn(str(book.root / 'realm.json') + ':/opt/keycloak/data/import/course.json:ro', run.call_args.args)

    def test_actual_classroom_binding_renders_fresh_configuration_and_recreates_pinned_hub(self):
        with tempfile.TemporaryDirectory() as directory:
            book = self.fixture(Path(directory).resolve())
            initial = book.deployment
            initial_bytes = (initial / 'manifest.json').read_bytes()
            book.cairn_origin = 'http://127.0.0.1:18001'
            book.compose = Mock()
            book.session = Mock()
            book.session.get.return_value.status_code = 200
            with patch('notebook.preflight', return_value={'status':'ready', 'images':book.manifest['images']}) as preflight:
                approval = book.bind_classroom('actual-classroom', book.origin)
            preflight.assert_called_once_with(book.root / 'bound-deployment')
            self.assertEqual(approval, {'CAIRN_SUBMISSIONS':'true', 'CAIRN_SUBMISSION_CLASSROOMS':'actual-classroom'})
            self.assertEqual(book.deployment, book.root / 'bound-deployment')
            binding = json.loads((book.deployment / 'cairn-binding.json').read_text())
            self.assertEqual(binding['classroom_id'], 'actual-classroom')
            self.assertEqual(binding['workspace_url'], book.origin)
            self.assertEqual(book.env['WORKSPACE_CAIRN_ORIGIN'], book.cairn_origin)
            self.assertEqual(book.env['WORKSPACE_CAIRN_LOCAL_TEST'], '1')
            self.assertEqual(book.manifest['backup']['staging_dir'], str(book.deployment))
            self.assertEqual((initial / 'manifest.json').read_bytes(), initial_bytes)
            self.assertEqual(book.compose.call_args_list[0].args, ('stop', 'hub'))
            self.assertEqual(book.compose.call_args_list[1].args,
                             ('up', '-d', '--force-recreate', '--no-build', '--pull', 'never', 'hub'))
            self.assertIn('course-manifest-cairn-bound', book.report)
            with self.assertRaises(ManifestError):
                book.bind_classroom('another-classroom', book.origin)

    def test_mismatched_binding_does_not_start_or_render(self):
        with tempfile.TemporaryDirectory() as directory:
            book = self.fixture(Path(directory).resolve())
            book.cairn_origin = 'http://127.0.0.1:18001'
            book.compose = Mock()
            for cid, workspace in [('actual-classroom', 'https://other.example.test'), ('bad/classroom', book.origin)]:
                with self.subTest(classroom=cid), self.assertRaises(AssertionError):
                    book.bind_classroom(cid, workspace)
                book.compose.assert_not_called()
                self.assertFalse((book.root / 'bound-deployment').exists())
                self.assertNotIn('course-manifest-cairn-bound', book.report)

    def test_startup_preflight_must_verify_exact_manifest_images(self):
        with tempfile.TemporaryDirectory() as directory:
            book = self.fixture(Path(directory).resolve())
            for result in ({'status':'ready', 'images':{}}, {'status':'refused', 'images':book.manifest['images']}):
                with patch('notebook.preflight', return_value=result), self.assertRaises(AssertionError):
                    book.verify_startup_preflight(book.deployment)
                self.assertNotIn('course-manifest-startup-preflight', book.report)
            with patch('notebook.preflight', side_effect=ValueError('unprovisioned homes')):
                with self.assertRaisesRegex(ValueError, 'unprovisioned homes'):
                    book.verify_startup_preflight(book.deployment)
            with patch('notebook.preflight', return_value={'status':'ready', 'images':book.manifest['images']}):
                book.verify_startup_preflight(book.deployment)
            self.assertIn('course-manifest-startup-preflight', book.report)

    def test_image_consumption_requires_started_hub_and_learner_exact_pins(self):
        with tempfile.TemporaryDirectory() as directory:
            book = self.fixture(Path(directory).resolve())
            for hub, learners, course in [('sha256:'+'c'*64, 'learner', book.image),
                                          (book.hub_image, '', book.image),
                                          (book.hub_image, 'learner', 'sha256:'+'c'*64)]:
                def docker(*args):
                    if args[0] == 'ps': return learners
                    return json.dumps([{'Image': hub if args[1].endswith('-hub') else course}])
                with self.subTest(hub=hub, learners=learners, course=course), patch('notebook.docker', side_effect=docker):
                    with self.assertRaises(AssertionError): book.verify_runtime_images()
                self.assertNotIn('course-manifest-images-consumed', book.report)
            with patch('notebook.docker', side_effect=[json.dumps([{'Image':book.hub_image}]), 'learner', json.dumps([{'Image':book.image}])]):
                book.verify_runtime_images()
            self.assertIn('course-manifest-images-consumed', book.report)


if __name__ == '__main__': unittest.main()
