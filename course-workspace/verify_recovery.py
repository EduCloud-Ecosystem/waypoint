#!/usr/bin/env python3
"""Two-phase synthetic recovery; run source and target on separate clean workers."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from backup import docker
from pilot import ROOT, initialize, start
from recovery import private_json, load_config, restic, backup_repository, restore_repository, image_info
from verify import cleanup, port
from verify_pilot import check

# Public synthetic fixture, deliberately NOT suitable for real records. Keeping
# it outside the transferred repository proves the recovery-key dependency.
FIXTURE_PASSWORD = 'public-ci-synthetic-recovery-fixture-never-use-for-real-data'


def configuration(directory, repository):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    password = directory / 'password'
    password.write_text(FIXTURE_PASSWORD)
    password.chmod(0o600)
    path = directory / 'recovery.json'
    private_json(path, {'version': 1, 'repository': str(repository),
                       'password_file': str(password), 'local_test': True})
    return load_config(path)


def source(transfer, reuse_image=None):
    transfer.mkdir(parents=True, mode=0o700, exist_ok=False)
    directory = ROOT / 'output' / ('recovery-source-' + uuid.uuid4().hex[:8])
    env = initialize(directory, port())
    cfg = configuration(directory / 'credentials', transfer / 'repository')
    try:
        restic(cfg, 'init')
        if reuse_image:
            docker('tag', reuse_image, env['WORKSPACE_INSTANCE'] + '-hub:pilot')
        start(directory, env, build=not reuse_image)
        check(env)
        ids = [image_info(env['WORKSPACE_INSTANCE'] + '-hub:pilot')['Id'], image_info(env['WORKSPACE_IMAGE'])['Id']]
        snapshot = backup_repository(directory, cfg)
        assert not list(directory.glob('.recovery-*')), 'plaintext staging left behind'
        restic(cfg, 'check', '--read-data')
        private_json(transfer / 'transfer.json', {'snapshot': snapshot, 'images': ids})
        print('Encrypted repository ready for transfer; source volumes are being removed.', flush=True)
    finally:
        cleanup(env['WORKSPACE_INSTANCE'], directory / 'pilot.env')


def target(transfer, require_clean):
    info = json.loads((transfer / 'transfer.json').read_text())
    if require_clean:
        for image in info['images']:
            probe = subprocess.run(['docker', 'image', 'inspect', image], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
            assert probe.returncode != 0, 'target already has source runtime image'
    output = ROOT / 'output' / ('recovery-target-' + uuid.uuid4().hex[:8])
    cfg = configuration(output / 'credentials', transfer / 'repository')
    target_dir = output / 'restored'
    wrong = output / 'wrong-password'
    wrong.write_text('wrong-synthetic-key-with-sufficient-length')
    wrong.chmod(0o600)
    try:
        restore_repository(dict(cfg, password_file=str(wrong)), info['snapshot'], target_dir, port())
    except RuntimeError:
        assert not target_dir.exists()
    else:
        raise AssertionError('incorrect recovery key was accepted')
    env = None
    try:
        env = restore_repository(cfg, info['snapshot'], target_dir, port())
        # Use only the restored deployment scripts/images to start the replacement.
        subprocess.run(['python3', str(target_dir / 'runtime/pilot.py'), 'start', '--no-build',
                        '--directory', str(target_dir)], check=True)
        check(env, restored=True)
        try:
            restore_repository(cfg, info['snapshot'], target_dir, port())
        except ValueError:
            pass
        else:
            raise AssertionError('restore overwrote a destination')
        restic(cfg, 'check', '--read-data')
        (output / 'PASS.txt').write_text('Encrypted recovery, wrong-key refusal, preserved images, saved files, kernels and overwrite refusal passed.\n')
        print('PASS: replacement started from the encrypted package with saved files and Python/R.', flush=True)
    finally:
        if env:
            cleanup(env['WORKSPACE_INSTANCE'], target_dir / 'pilot.env')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['source', 'target'])
    parser.add_argument('--transfer', required=True, type=Path)
    parser.add_argument('--reuse-hub-image')
    parser.add_argument('--require-clean', action='store_true')
    args = parser.parse_args()
    if args.phase == 'source':
        source(args.transfer.resolve(), args.reuse_hub_image)
    else:
        target(args.transfer.resolve(), args.require_clean)


if __name__ == '__main__':
    main()
