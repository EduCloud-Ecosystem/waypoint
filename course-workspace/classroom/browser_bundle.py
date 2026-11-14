# SPDX-License-Identifier: Apache-2.0
"""Offline, reviewed learner bundles; no browser runtime or execution authority."""
import argparse
import ast
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from assignment_profiles import get_profile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'course'))
from waypoint_cairn import cairn_origin, open_root, read_saved, safe_path

CATALOG = json.loads((Path(__file__).parent / 'browser_bundle_sources.json').read_text())
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024  # Matches the existing safe saved-file reader.
GATES = ('execution', 'packages', 'storage_reopen', 'export_restore', 'network_privacy',
         'accessibility', 'hosted_fallback_access', 'submission_handoff')
NATIVE = {'ctypes', 'multiprocessing', 'os', 'subprocess', 'socket'}
SERVER = {'jupyter_server', 'jupyterhub', 'waypoint_belay', 'waypoint_cairn'}


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + '\n').encode()


def advisory(files):
    """Static hints only, never execution, completeness or egress proof."""
    imports, native, server, unparsed = set(), set(), set(), []
    outputs = False
    for name, raw in files.items():
        if not name.endswith('.ipynb'):
            continue
        notebook = json.loads(raw)
        if notebook.get('nbformat') != 4 or not isinstance(notebook.get('cells'), list):
            raise ValueError('reviewed source is not a version-4 notebook')
        for index, cell in enumerate(notebook['cells']):
            if cell.get('cell_type') != 'code':
                continue
            outputs |= bool(cell.get('outputs')) or cell.get('execution_count') is not None
            source = cell.get('source', '')
            if isinstance(source, list):
                source = ''.join(source)
            try:
                tree = ast.parse(source)
            except SyntaxError:
                unparsed.append({'file': name, 'cell_index': index})
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split('.')[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split('.')[0])
    native.update(imports & NATIVE)
    server.update(imports & SERVER)
    return {'scope': 'reviewed_starter_code_only', 'imports': sorted(imports),
            'native_indicators': sorted(native), 'server_indicators': sorted(server),
            'unparsed_cells': unparsed, 'notebook_outputs_present': outputs,
            'compatibility': 'not_established', 'dynamic_imports_and_future_work': 'not_assessed'}


def bundle(profile_key, hosted_fallback):
    profile = get_profile(profile_key)
    fallback = cairn_origin(hosted_fallback)
    expected = CATALOG[profile.key]
    # Exact reviewed paths only: never walk the package or operator tree.
    selected = read_saved(profile.package / 'learner', sorted(expected))
    files, manifest = {}, []
    for item in selected:
        raw = base64.b64decode(item['content_base64'], validate=True)
        if len(raw) > MAX_FILE_BYTES or item['sha256'] != expected[item['path']]:
            raise ValueError('learner source differs from reviewed digest catalog')
        files[item['path']] = raw
        manifest.append({'path': item['path'], 'bytes': len(raw), 'sha256': item['sha256']})
    if sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        raise ValueError('learner source exceeds 1 MiB')
    scan = advisory(files)
    if scan['notebook_outputs_present']:
        raise ValueError('starter notebooks must not contain outputs or execution counts')
    report = {'schema_version': 1, 'profile': profile.key, 'task_version': profile.task,
              'state': 'source_bundle_only', 'files': manifest,
              'advisory': scan, 'qualification': {gate: 'unqualified' for gate in GATES},
              'hosted_fallback': {'origin': fallback, 'status': 'operator_supplied_unverified'},
              'submission_files': list(profile.files),
              'identity_credentials_tutoring': 'not_included',
              'proof': 'reviewed_source_bytes_only'}
    files['bundle-manifest.json'] = encoded(report)
    files['BROWSER-READINESS.md'] = (
        '# Learner source bundle; browser use is unqualified\n\n'
        'This archive contains approved starter materials, not JupyterLite, a kernel, '
        'a hosted service or an automatic submission client. No model is called.\n\n'
        'Keep an independent download of your work. Browser storage can be lost. '
        'Do not enter private data before the actual runtime and its outgoing '
        'requests have been reviewed. Static imports cannot prove compatibility or privacy.\n\n'
        'The operator supplied this hosted fallback: ' + fallback + '\n'
        'Access, capacity and availability have not been verified by this tool. '
        'Contact your course operator for access and human help.\n\n'
        'You may complete the same task without AI. Save and export your files, '
        'then use the course\'s authorized explicit submission and receipt workflow. '
        'Saving, exporting and checking this archive do not submit or grade work. '
        'Existing server-side tutoring/submission extensions do not run in this bundle.\n'
    ).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, files[name])
    raw = output.getvalue()
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError('archive exceeds 1 MiB')
    return raw, report


def write_new(path, raw):
    path = Path(path)
    safe_path(path.name)
    parent = open_root(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(path.name, dir_fd=parent)
            raise
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def verify(path, expected):
    # No ZIP extraction or trust in its embedded manifest. Recreate exact bytes
    # from the local reviewed catalog and caller-selected profile/fallback.
    path = Path(path)
    selected = read_saved(path.parent, [path.name])
    raw = base64.b64decode(selected[0]['content_base64'], validate=True)
    if raw != expected:
        raise ValueError('archive differs from reviewed deterministic bundle')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'verify'))
    parser.add_argument('--assignment-profile', choices=tuple(CATALOG), required=True)
    parser.add_argument('--hosted-fallback', required=True, help='Operator-supplied HTTPS origin; not contacted')
    parser.add_argument('--archive', type=Path, required=True, help='Absolute path; parent must already exist')
    args = parser.parse_args()
    try:
        raw, report = bundle(args.assignment_profile, args.hosted_fallback)
        if args.action == 'build':
            write_new(args.archive, raw)
        else:
            verify(args.archive, raw)
        print(json.dumps({'action': args.action, 'profile': report['profile'],
                          'state': report['state'], 'bytes': len(raw),
                          'sha256': hashlib.sha256(raw).hexdigest(),
                          'browser_qualified': False}, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        # Never print selected file content, credentials or a raw private path.
        parser.exit(1, 'Bundle refused: ' + type(exc).__name__ + '. Check reviewed sources, HTTPS fallback and fresh absolute archive path.\n')


if __name__ == '__main__':
    raise SystemExit(main())
