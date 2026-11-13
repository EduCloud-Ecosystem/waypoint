# SPDX-License-Identifier: Apache-2.0
"""Build the Waypoint operator source release from a committed Git tree."""
import argparse
from datetime import UTC, datetime
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
CLASSROOM = 'course-workspace/classroom/'
CATALOG = CLASSROOM + 'browser_bundle_sources.json'
PROFILES = {
    'legacy': CLASSROOM + 'info511-lab03-q4-v1/learner/',
    'nhanes-v1': CLASSROOM + 'info511-nhanes-v1/learner/',
}
# The 20 file paths from the published rc.1 operator source manifest.
FILES = tuple(sorted((
    CLASSROOM + 'BROWSER-BUNDLE.md',
    CLASSROOM + 'assignment_profiles.py',
    CLASSROOM + 'browser_bundle.LICENSE',
    CLASSROOM + 'browser_bundle.py',
    CATALOG,
    *(PROFILES['legacy'] + name for name in (
        'README.md', 'assistance.md', 'data/info511-inflation-synthetic.csv',
        'explanation.md', 'lab3-q4.ipynb', 'tidy-inflation.csv',
    )),
    *(PROFILES['nhanes-v1'] + name for name in (
        'README.md', 'assistance.md', 'data/BPXO_L_selected.csv',
        'data/DEMO_L_selected.csv', 'explanation.md', 'nhanes-wrangling.ipynb',
        'tidy-nhanes.csv',
    )),
    'course-workspace/course/waypoint_cairn/__init__.py',
    'course-workspace/tests/test_browser_bundle.py',
)))
PREFIX = 'waypoint-browser-bundle/'
REPO_URL = 'https://github.com/EduCloud-Ecosystem/waypoint'
BUILDER = CLASSROOM + 'release_browser_bundle.py'


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root)


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def json_bytes(data):
    return (json.dumps(data, indent=2, sort_keys=True) + '\n').encode()


def archive_files(raw, revision):
    """Read only the exact allowlisted file members of the emitted ZIP."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [member for member in archive.infolist() if not member.is_dir()]
        if (archive.comment != revision.encode()
                or sorted(member.filename for member in members) != [PREFIX + path for path in FILES]
                or any(stat.S_IFMT(member.external_attr >> 16) not in (0, stat.S_IFREG)
                       for member in members)):
            raise ValueError('ZIP does not match the pinned regular-file allowlist')
        return {path: archive.read(PREFIX + path) for path in FILES}


def spdx_document(manifest, files, created):
    package_id = 'SPDXRef-Waypoint-Operator-Source'
    entries = []
    for index, (path, raw) in enumerate(files.items(), start=1):
        declared = 'NOASSERTION'
        if path.endswith('.py') and b'# SPDX-License-Identifier: Apache-2.0' in raw.splitlines()[:2]:
            declared = 'Apache-2.0'
        entries.append({
            'SPDXID': f'SPDXRef-File-{index}',
            'fileName': './' + PREFIX + path,
            'checksums': [
                {'algorithm': 'SHA1', 'checksumValue': hashlib.sha1(raw, usedforsecurity=False).hexdigest()},
                {'algorithm': 'SHA256', 'checksumValue': manifest['files_sha256'][path]},
            ],
            'licenseInfoInFiles': [declared],
            'licenseConcluded': 'NOASSERTION',
            'copyrightText': 'NOASSERTION',
        })
    file_sha1s = sorted(entry['checksums'][0]['checksumValue'] for entry in entries)
    document = {
        'spdxVersion': 'SPDX-2.3',
        'dataLicense': 'CC0-1.0',
        'SPDXID': 'SPDXRef-DOCUMENT',
        'name': manifest['archive'],
        'creationInfo': {
            'created': created,
            'creators': ['Tool: waypoint-browser-bundle-release-' + manifest['version']],
        },
        'packages': [{
            'SPDXID': package_id,
            'name': 'waypoint-browser-bundle-operator-source',
            'versionInfo': manifest['version'],
            'packageFileName': manifest['archive'],
            'downloadLocation': 'NOASSERTION',
            'filesAnalyzed': True,
            'packageVerificationCode': {
                'packageVerificationCodeValue': hashlib.sha1(
                    ''.join(file_sha1s).encode('ascii'), usedforsecurity=False
                ).hexdigest(),
            },
            'checksums': [{'algorithm': 'SHA256', 'checksumValue': manifest['archive_sha256']}],
            'sourceInfo': 'Waypoint Git revision ' + REPO_URL + '/commit/' + manifest['source_revision'],
            'licenseDeclared': 'NOASSERTION',
            'licenseConcluded': 'NOASSERTION',
            'licenseComments': 'Records explicit file headers. See the scoped license notice in BROWSER-BUNDLE.md.',
            'copyrightText': 'NOASSERTION',
        }],
        'files': entries,
        'relationships': [
            {'spdxElementId': 'SPDXRef-DOCUMENT', 'relationshipType': 'DESCRIBES',
             'relatedSpdxElement': package_id},
            {'spdxElementId': package_id, 'relationshipType': 'DEPENDS_ON',
             'relatedSpdxElement': 'NOASSERTION',
             'comment': 'The operator tool uses external Python 3.12+ and its standard library. '
                        'Browser and hosted runtime dependencies are outside this source inventory.'},
            *({'spdxElementId': package_id, 'relationshipType': 'CONTAINS',
               'relatedSpdxElement': entry['SPDXID']} for entry in entries),
        ],
    }
    document['documentNamespace'] = REPO_URL + '/spdx/operator-source/' + sha256(json_bytes(document))
    return document


def build(root, version, created):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-rc\.[0-9]+)?', version):
        raise ValueError('use vMAJOR.MINOR.PATCH or vMAJOR.MINOR.PATCH-rc.N')
    if git(root, 'status', '--porcelain', '--', BUILDER, *FILES):
        raise ValueError('commit the release builder and allowlisted source files before building')
    revision = git(root, 'rev-parse', 'HEAD').decode().strip()
    raw = git(root, 'archive', '--format=zip', '-0', '--prefix=' + PREFIX, revision, '--', *FILES)
    files = archive_files(raw, revision)
    if any(payload != git(root, 'cat-file', 'blob', f'{revision}:{path}') for path, payload in files.items()):
        raise ValueError('ZIP file bytes differ from committed Git blobs')
    digests = {path: sha256(payload) for path, payload in files.items()}
    catalog = json.loads(files[CATALOG])
    expected = {
        prefix + path: digest
        for profile, prefix in PROFILES.items()
        for path, digest in catalog[profile].items()
    }
    learner = {path: digest for path, digest in digests.items() if path.startswith(tuple(PROFILES.values()))}
    if set(catalog) != PROFILES.keys() or learner != expected:
        raise ValueError('learner files differ from the reviewed digest catalog')

    stem = 'waypoint-browser-bundle-' + version
    manifest = {
        'schema': 'waypoint-browser-bundle-source-release-v1',
        'version': version,
        'source_revision': revision,
        'audience': 'operator_source_tool_not_learner_ready_runtime',
        'browser_qualified': False,
        'files_sha256': digests,
        'archive': stem + '.zip',
        'archive_sha256': sha256(raw),
    }
    artifacts = {
        manifest['archive']: raw,
        'SOURCE-MANIFEST.json': json_bytes(manifest),
        stem + '.spdx.json': json_bytes(spdx_document(manifest, files, created)),
    }
    artifacts['SHA256SUMS'] = ''.join(
        f'{sha256(payload)}  {name}\n' for name, payload in sorted(artifacts.items())
    ).encode()
    return artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--out', required=True, type=Path, help='Fresh output directory')
    args = parser.parse_args()
    try:
        created = datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
        artifacts = build(ROOT, args.version, created)
        args.out.mkdir()
        written = []
        try:
            for name, payload in artifacts.items():
                path = args.out / name
                with path.open('xb') as stream:
                    written.append(path)
                    stream.write(payload)
        except OSError:
            for path in written:
                path.unlink()
            args.out.rmdir()
            raise
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
        parser.exit(2, 'Release refused: ' + str(exc) + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
