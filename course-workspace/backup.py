#!/usr/bin/env python3
"""Offline, provider-independent recovery archives. Never overwrites a volume."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

LABEL = 'educloud.workspace.instance'
KIND = 'educloud.workspace.kind'


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def identifier(value):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', value):
        raise ValueError('invalid instance identifier')
    return value


def validate_manifest(manifest, directory):
    source = identifier(manifest['instance'])
    if manifest.get('version') != 1:
        raise ValueError('unsupported archive version')
    seen = set()
    hub_count = 0
    for item in manifest['volumes']:
        suffix = item['suffix']
        if suffix in seen or not re.fullmatch(r'hub-data|home-[a-f0-9]{24}', suffix):
            raise ValueError('invalid or duplicate volume suffix')
        seen.add(suffix)
        expected_kind = 'hub' if suffix == 'hub-data' else 'home'
        if item['kind'] != expected_kind:
            raise ValueError('invalid volume kind')
        hub_count += expected_kind == 'hub'
        path = directory / (suffix + '.tar.gz')
        if path.is_symlink() or not path.is_file() or digest(path) != item['sha256']:
            raise ValueError('archive is missing or checksum differs')
    if hub_count != 1:
        raise ValueError('archive must contain exactly one Hub database volume')
    return source


def helper(image, mode, volume, directory, uid, gid):
    volume_mount = f'type=volume,source={volume},target=/data'
    if mode == 'pack':
        volume_mount += ',readonly'
    docker('run', '--rm', '--network', 'none', '--read-only',
           '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE',
           '--security-opt', 'no-new-privileges:true',
           '--mount', volume_mount,
           '--mount', f'type=bind,source={directory},target=/archive' + (',readonly' if mode == 'unpack' else ''),
           '--mount', f'type=bind,source={Path(__file__).with_name("volume_archive.py")},target=/tool.py,readonly',
           '--user', '0:0', image, 'python', '/tool.py', mode, str(uid), str(gid))


def backup(instance, output, image):
    names = docker('volume', 'ls', '--filter', f'label={LABEL}={instance}', '--format', '{{.Name}}').splitlines()
    if not names:
        raise ValueError('no course volumes found')
    # Inspect every writer, not just labelled containers, before copying SQLite
    # or learner files. Quiescing this course is an explicit operator action.
    for name in names:
        if docker('ps', '-q', '--filter', 'volume=' + name):
            raise ValueError('stop every container using course volumes before backup')
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    manifest = {'version': 1, 'instance': instance, 'volumes': [],
                'helper_image_id': json.loads(docker('image', 'inspect', image))[0]['Id']}
    for name in sorted(names):
        info = json.loads(docker('volume', 'inspect', name))[0]
        suffix = name.removeprefix(instance + '-')
        kind = info.get('Labels', {}).get(KIND)
        if not name.startswith(instance + '-') or not re.fullmatch(r'hub-data|home-[a-f0-9]{24}', suffix):
            raise ValueError('unexpected volume name; archive incomplete')
        with tempfile.TemporaryDirectory(dir=output) as scratch:
            helper(image, 'pack', name, scratch, os.getuid(), os.getgid())
            dest = output / (suffix + '.tar.gz')
            Path(scratch, 'data.tar.gz').rename(dest)
        manifest['volumes'].append({'suffix': suffix, 'kind': kind, 'sha256': digest(dest)})
    validate_manifest(manifest, output)
    target = output / 'manifest.json'
    target.write_text(json.dumps(manifest, indent=2) + '\n')
    target.chmod(0o600)
    print(f'Archived {len(names)} volumes. Protect this directory as learner data and credentials.')


def restore(instance, source, image):
    manifest = json.loads((source / 'manifest.json').read_text())
    validate_manifest(manifest, source)
    targets = [instance + '-' + item['suffix'] for item in manifest['volumes']]
    existing = set(docker('volume', 'ls', '--format', '{{.Name}}').splitlines())
    if existing.intersection(targets):
        raise ValueError('restore requires new volume names; refusing to overwrite existing data')
    created = []
    try:
        for item, name in zip(manifest['volumes'], targets):
            docker('volume', 'create', '--label', f'{LABEL}={instance}', '--label', f'{KIND}={item["kind"]}', name)
            created.append(name)
            with tempfile.TemporaryDirectory() as scratch:
                # Read-only archive bind; no untrusted extraction on the host.
                shutil.copyfile(source / (item['suffix'] + '.tar.gz'), Path(scratch, 'data.tar.gz'))
                owner = 1000 if item['kind'] == 'home' else 0
                helper(image, 'unpack', name, scratch, owner, owner)
    except Exception:
        # Only new, unmounted volumes created by this invocation are removed.
        for name in reversed(created):
            docker('volume', 'rm', name)
        raise
    print(f'Restored {len(created)} volumes to {instance}. Start with the matching course image and identity provider.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['backup', 'restore'])
    parser.add_argument('--instance', required=True, type=identifier)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--image', default='educloud-python-r:2026-10-06')
    args = parser.parse_args()
    try:
        action = backup if args.operation == 'backup' else restore
        action(args.instance, args.directory.resolve(), args.image)
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Recovery operation refused or failed: {exc}\n')


if __name__ == '__main__':
    main()
