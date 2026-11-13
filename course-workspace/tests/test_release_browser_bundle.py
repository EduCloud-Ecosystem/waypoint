# SPDX-License-Identifier: Apache-2.0
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / 'classroom/release_browser_bundle.py'
SPEC = importlib.util.spec_from_file_location('release_browser_bundle', SCRIPT)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)

VERSION = 'v0.0.0-rc.1'
CREATED = '2026-11-13T20:00:00Z'
STEM = 'waypoint-browser-bundle-' + VERSION
LICENSED = {
    release.CLASSROOM + 'assignment_profiles.py',
    release.CLASSROOM + 'browser_bundle.py',
    'course-workspace/tests/test_browser_bundle.py',
}


class OperatorReleaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve() / 'checkout'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Release Test')
        self.git('config', 'user.email', 'release@example.invalid')
        self.git('config', 'commit.gpgsign', 'false')
        for path in release.FILES:
            payload = ('# SPDX-License-Identifier: Apache-2.0\n' if path in LICENSED else '') + path + '\n'
            self.write(path, payload.encode())
        catalog = {
            profile: {
                path.removeprefix(prefix): hashlib.sha256((self.root / path).read_bytes()).hexdigest()
                for path in release.FILES if path.startswith(prefix)
            }
            for profile, prefix in release.PROFILES.items()
        }
        self.write(release.CATALOG, json.dumps(catalog).encode())
        self.write(release.BUILDER, SCRIPT.read_bytes())
        self.write(release.CLASSROOM + 'info511-nhanes-v1/operator/answer.txt', b'operator answer')
        self.write(release.PROFILES['legacy'] + 'unreviewed.txt', b'unreviewed')
        self.commit()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root)

    def write(self, path, raw):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')

    def test_archive_sbom_and_manifest_agree(self):
        artifacts = release.build(self.root, VERSION, CREATED)
        self.assertEqual(artifacts, release.build(self.root, VERSION, CREATED))
        self.assertEqual(set(artifacts), {
            STEM + '.zip', STEM + '.spdx.json', 'SOURCE-MANIFEST.json', 'SHA256SUMS',
        })
        raw = artifacts[STEM + '.zip']
        manifest = json.loads(artifacts['SOURCE-MANIFEST.json'])
        doc = json.loads(artifacts[STEM + '.spdx.json'])
        package, = doc['packages']
        revision = self.git('rev-parse', 'HEAD').decode().strip()
        self.assertEqual(manifest['source_revision'], revision)
        self.assertEqual(manifest['version'], VERSION)
        self.assertFalse(manifest['browser_qualified'])
        self.assertEqual(manifest['archive_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(package['checksums'], [
            {'algorithm': 'SHA256', 'checksumValue': manifest['archive_sha256']},
        ])
        self.assertEqual(package['packageFileName'], manifest['archive'])
        self.assertTrue(package['sourceInfo'].endswith('/commit/' + revision))
        self.assertEqual(package['licenseDeclared'], 'NOASSERTION')
        self.assertEqual(package['licenseConcluded'], 'NOASSERTION')
        self.assertEqual(doc['spdxVersion'], 'SPDX-2.3')
        self.assertEqual(doc['dataLicense'], 'CC0-1.0')
        self.assertEqual(doc['creationInfo']['created'], CREATED)
        sha1s = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if not name.endswith('/')]
            self.assertEqual(archive.comment, revision.encode())
            self.assertEqual(len(names), 20)
            self.assertCountEqual(names, [release.PREFIX + path for path in manifest['files_sha256']])
            self.assertCountEqual(names, [entry['fileName'][2:] for entry in doc['files']])
            for entry in doc['files']:
                name = entry['fileName'][2:]
                path = name.removeprefix(release.PREFIX)
                contents = archive.read(name)
                self.assertEqual(contents, self.git('cat-file', 'blob', f'{revision}:{path}'))
                sha1 = hashlib.sha1(contents, usedforsecurity=False).hexdigest()
                sha1s.append(sha1)
                self.assertEqual(manifest['files_sha256'][path], hashlib.sha256(contents).hexdigest())
                self.assertEqual({c['algorithm']: c['checksumValue'] for c in entry['checksums']},
                                 {'SHA1': sha1, 'SHA256': manifest['files_sha256'][path]})
                expected = 'Apache-2.0' if path in LICENSED else 'NOASSERTION'
                self.assertEqual(entry['licenseInfoInFiles'], [expected])
        self.assertEqual(package['packageVerificationCode']['packageVerificationCodeValue'],
                         hashlib.sha1(''.join(sorted(sha1s)).encode(), usedforsecurity=False).hexdigest())
        ids = [entry['SPDXID'] for entry in doc['files']]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertCountEqual(
            [(r['spdxElementId'], r['relationshipType'], r['relatedSpdxElement'])
             for r in doc['relationships']],
            [(doc['SPDXID'], 'DESCRIBES', package['SPDXID']),
             (package['SPDXID'], 'DEPENDS_ON', 'NOASSERTION'),
             *((package['SPDXID'], 'CONTAINS', file_id) for file_id in ids)],
        )
        checksums = {name: digest for digest, name in
                     (line.split('  ') for line in artifacts['SHA256SUMS'].decode().splitlines())}
        self.assertEqual(checksums, {name: hashlib.sha256(data).hexdigest()
                                    for name, data in artifacts.items() if name != 'SHA256SUMS'})

    def test_dirty_source_and_committed_catalog_mismatch_refused(self):
        path = release.PROFILES['legacy'] + 'README.md'
        self.write(path, b'changed')
        with self.assertRaisesRegex(ValueError, 'commit'):
            release.build(self.root, VERSION, CREATED)
        self.commit()
        with self.assertRaisesRegex(ValueError, 'digest catalog'):
            release.build(self.root, VERSION, CREATED)

    def test_committed_symlink_refused(self):
        target = self.root / (release.PROFILES['legacy'] + 'README.md')
        target.unlink()
        target.symlink_to('unreviewed.txt')
        self.commit()
        with self.assertRaisesRegex(ValueError, 'allowlist'):
            release.build(self.root, VERSION, CREATED)

    def test_cli_refuses_invalid_version_and_existing_output(self):
        out = self.root.parent / 'release'
        command = [sys.executable, str(self.root / release.BUILDER), '--out', str(out)]
        invalid = subprocess.run([*command, '--version', '../unsafe'], capture_output=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertFalse(out.exists())
        subprocess.run([*command, '--version', VERSION], check=True)
        original = {p.name: p.read_bytes() for p in out.iterdir()}
        refused = subprocess.run([*command, '--version', VERSION], capture_output=True)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(original, {p.name: p.read_bytes() for p in out.iterdir()})


if __name__ == '__main__':
    unittest.main()
