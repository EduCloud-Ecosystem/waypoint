# SPDX-License-Identifier: Apache-2.0
"""Offline source/privacy boundaries; no browser, kernel, model or service."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('browser_bundle', ROOT / 'classroom/browser_bundle.py')
BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUNDLE)
FALLBACK = 'https://workspace.example.edu'


class BrowserBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profile = BUNDLE.get_profile('nhanes-v1')
        self.package = self.root / 'package'
        shutil.copytree(self.profile.package / 'learner', self.package / 'learner')
        self.bound = self.profile._replace(package=self.package)

    def build_fixture(self):
        with patch.object(BUNDLE, 'get_profile', return_value=self.bound):
            return BUNDLE.bundle('nhanes-v1', FALLBACK)

    def test_both_profiles_are_deterministic_and_bytes_are_digest_bound(self):
        for key in ('legacy', 'nhanes-v1'):
            raw, report = BUNDLE.bundle(key, FALLBACK)
            self.assertEqual(raw, BUNDLE.bundle(key, FALLBACK)[0])
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                self.assertEqual(archive.namelist(), sorted(archive.namelist()))
                for row in report['files']:
                    value = archive.read(row['path'])
                    self.assertEqual(hashlib.sha256(value).hexdigest(), row['sha256'])
                    self.assertEqual(len(value), row['bytes'])
                self.assertEqual(set(archive.namelist()), set(BUNDLE.CATALOG[key]) |
                                 {'bundle-manifest.json', 'BROWSER-READINESS.md'})
                self.assertTrue(all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist()))

    def test_operator_answers_hidden_outputs_and_unlisted_files_are_never_read(self):
        marker = b'PRIVATE_OPERATOR_SOLUTION_DO_NOT_BUNDLE'
        for name in ('operator/answers.md', 'learner/.env', 'learner/solution.py',
                     'learner/outputs/result.csv', 'learner/data/unapproved.csv'):
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(marker)
        raw, _ = self.build_fixture()
        self.assertNotIn(marker, raw)

    def test_tampered_reviewed_content_and_missing_file_refused(self):
        path = self.package / 'learner/README.md'
        path.write_text('Unreviewed content, perhaps a solution')
        with self.assertRaisesRegex(ValueError, 'reviewed digest'):
            self.build_fixture()
        path.unlink()
        with self.assertRaises(OSError):
            self.build_fixture()

    def test_file_directory_and_ancestor_symlinks_refused(self):
        learner = self.package / 'learner'
        name = learner / 'README.md'
        copy = self.root / 'copy.md'
        shutil.copyfile(name, copy)
        name.unlink(); name.symlink_to(copy)
        with self.assertRaises(OSError):
            self.build_fixture()
        name.unlink(); shutil.copyfile(copy, name)
        (learner / 'data').rename(self.root / 'data')
        (learner / 'data').symlink_to(self.root / 'data', target_is_directory=True)
        with self.assertRaises(OSError):
            self.build_fixture()
        alias = self.root / 'alias'
        alias.symlink_to(self.package, target_is_directory=True)
        self.bound = self.profile._replace(package=alias)
        with self.assertRaises(OSError):
            self.build_fixture()

    def test_nonregular_source_and_oversized_source_refused(self):
        path = self.package / 'learner/README.md'
        path.unlink(); os.mkfifo(path)
        with self.assertRaises(ValueError):
            self.build_fixture()
        path.unlink(); path.write_bytes(b'x' * (BUNDLE.MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            self.build_fixture()

    def test_catalog_cannot_select_hidden_or_traversal_paths(self):
        for name in ('../operator/answers.md', '.env', '/etc/passwd', 'data//file.csv'):
            catalog = {'nhanes-v1': {name: 'a' * 64}}
            with self.subTest(name=name), patch.object(BUNDLE, 'CATALOG', catalog):
                with self.assertRaises(ValueError):
                    self.build_fixture()

    def test_notebook_outputs_refused_even_if_catalog_is_updated(self):
        path = self.package / 'learner/nhanes-wrangling.ipynb'
        notebook = json.loads(path.read_bytes())
        cell = next(cell for cell in notebook['cells'] if cell['cell_type'] == 'code')
        cell['outputs'] = [{'output_type': 'stream', 'text': ['PRIVATE OUTPUT']}]
        path.write_text(json.dumps(notebook))
        catalog = copy.deepcopy(BUNDLE.CATALOG)
        catalog['nhanes-v1'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(BUNDLE, 'CATALOG', catalog):
            with self.assertRaisesRegex(ValueError, 'outputs'):
                self.build_fixture()

    def test_advisory_only_lists_dependencies_and_indicators(self):
        notebook = {'nbformat': 4, 'cells': [
            {'cell_type': 'code', 'source': 'import pandas as pd\nimport os, subprocess\nfrom jupyterhub import x'},
            {'cell_type': 'code', 'source': '%pip install pandas'}]}
        scan = BUNDLE.advisory({'starter.ipynb': json.dumps(notebook).encode()})
        self.assertEqual(scan['imports'], ['jupyterhub', 'os', 'pandas', 'subprocess'])
        self.assertEqual(scan['native_indicators'], ['os', 'subprocess'])
        self.assertEqual(scan['server_indicators'], ['jupyterhub'])
        self.assertEqual(scan['unparsed_cells'], [{'file': 'starter.ipynb', 'cell_index': 1}])
        self.assertEqual(scan['compatibility'], 'not_established')

    def test_no_positive_qualification_or_submission_claim(self):
        raw, report = self.build_fixture()
        self.assertEqual(set(report['qualification'].values()), {'unqualified'})
        self.assertEqual(report['state'], 'source_bundle_only')
        self.assertEqual(report['proof'], 'reviewed_source_bytes_only')
        self.assertEqual(report['advisory']['imports'], ['pandas'])
        self.assertEqual(report['hosted_fallback']['status'], 'operator_supplied_unverified')
        self.assertEqual(report['submission_files'], list(self.profile.files))
        self.assertNotIn(str(self.root).encode(), raw)

    def test_fallback_required_https_origin_without_secret_or_url_path(self):
        for origin in ('', 'http://localhost', 'https://user:secret@example.edu',
                       'https://example.edu?token=secret', 'https://example.edu/#token',
                       'https://example.edu/path'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                BUNDLE.bundle('nhanes-v1', origin)

    def test_unknown_profile_refused(self):
        with self.assertRaises(ValueError):
            BUNDLE.bundle('../operator', FALLBACK)

    def test_new_archive_permissions_no_overwrite_or_symlink_parent(self):
        raw, _ = self.build_fixture()
        path = self.root / 'starter.zip'
        BUNDLE.write_new(path, raw)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            BUNDLE.write_new(path, b'replacement')
        self.assertEqual(path.read_bytes(), raw)
        alias = self.root / 'alias'
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            BUNDLE.write_new(alias / 'other.zip', raw)
        with self.assertRaises(ValueError):
            BUNDLE.write_new(Path('relative.zip'), raw)

    def test_verification_rejects_content_metadata_duplicate_and_traversal_tampering(self):
        raw, _ = self.build_fixture()
        path = self.root / 'starter.zip'
        BUNDLE.write_new(path, raw)
        BUNDLE.verify(path, raw)
        for name, content in [('bundle-manifest.json', b'{"execution":"qualified"}'),
                              ('../escape.txt', b'no extraction'), ('README.md', b'changed')]:
            candidate = io.BytesIO(raw)
            with zipfile.ZipFile(candidate, 'a') as archive:
                # Duplicates deliberately exercise malicious ZIP metadata.
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    archive.writestr(name, content)
            path.write_bytes(candidate.getvalue())
            with self.assertRaises(ValueError):
                BUNDLE.verify(path, raw)
        self.assertFalse((self.root.parent / 'escape.txt').exists())

    def test_verification_rejects_wrong_profile_or_fallback(self):
        raw, _ = self.build_fixture()
        path = self.root / 'starter.zip'
        BUNDLE.write_new(path, raw)
        for expected in (BUNDLE.bundle('legacy', FALLBACK)[0],
                         BUNDLE.bundle('nhanes-v1', 'https://other.example.edu')[0]):
            with self.assertRaises(ValueError):
                BUNDLE.verify(path, expected)

    def test_cli_build_verify_and_refusal_keep_browser_unqualified(self):
        command = [sys.executable, str(ROOT / 'classroom/browser_bundle.py')]
        arguments = ['--assignment-profile', 'nhanes-v1', '--hosted-fallback', FALLBACK,
                     '--archive', str(self.root / 'starter.zip')]
        for action in ('build', 'verify'):
            result = subprocess.run([*command, action, *arguments], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIs(json.loads(result.stdout)['browser_qualified'], False)
        refused = subprocess.run([*command, 'build', *arguments], capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertNotIn(str(self.root), refused.stderr)
        self.assertEqual(refused.stdout, '')


if __name__ == '__main__':
    unittest.main()
