#!/usr/bin/env python3
"""Linux root-only quota rehearsal on a new disposable loop-backed XFS filesystem."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from backup import docker
from homes import provision, inspect, registry, run
from pilot import ROOT, initialize, start
from verify import port, cleanup
from verify_pilot import check


def main():
    if os.geteuid() != 0:
        raise SystemExit('run this synthetic test as root on a disposable Linux worker')
    with tempfile.TemporaryDirectory(prefix='educloud-quota-') as scratch:
        temp = Path(scratch)
        disk = temp / 'test.img'
        with disk.open('wb') as file:
            file.truncate(2 * 1024**3)
        root = temp / 'homes'
        root.mkdir()
        run('mkfs.xfs', '-f', str(disk))
        run('mount', '-o', 'loop,prjquota', str(disk), str(root))
        directory = ROOT / 'output' / ('quota-' + temp.name)
        env = initialize(directory, port())
        env.update(WORKSPACE_HOME_ROOT=str(root), WORKSPACE_HOME_QUOTA_MB='64')
        (directory / 'pilot.env').write_text(''.join(f'{k}={v}\n' for k, v in env.items()))
        try:
            provision(root, env['WORKSPACE_INSTANCE'], ['alice', 'bob'], 64, 1000)
            start(directory, env)
            # Image builds can exceed the health freshness window.
            inspect(root, env['WORKSPACE_INSTANCE'])
            check(env)
            def home(user):
                return env['WORKSPACE_INSTANCE'] + '-home-' + hashlib.sha256(user.encode()).hexdigest()[:24]
            def probe(user, code):
                return docker('run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                              '--security-opt', 'no-new-privileges:true', '--user', '1000:1000',
                              '--mount', f'type=volume,source={home(user)},target=/home/learner',
                              env['WORKSPACE_IMAGE'], 'python', '-c', code)
            fill = '''import errno, os
try:
    with open('/home/learner/quota-probe', 'wb') as f:
        for _ in range(128):
            f.write(b'x' * 1024**2)
            f.flush()
            os.fsync(f.fileno())
except OSError as e:
    assert e.errno in (errno.EDQUOT, errno.ENOSPC), e
    print('QUOTA_ENFORCED')
else:
    raise AssertionError('write exceeded configured quota')
'''
            assert 'QUOTA_ENFORCED' in probe('alice', fill)
            probe('bob', "from pathlib import Path; p=Path('/home/learner/peer-ok'); p.write_text('peer remains writable'); assert p.read_text()=='peer remains writable'")
            probe('alice', "import os; os.unlink('/home/learner/quota-probe')")
            inode = '''import errno, os, shutil
path='/home/learner/inode-probe'
os.mkdir(path)
try:
    for n in range(2000):
        open(path+'/'+str(n), 'w').close()
except OSError as e:
    assert e.errno in (errno.EDQUOT, errno.ENOSPC), e
    print('INODE_QUOTA_ENFORCED')
else:
    raise AssertionError('inode limit was not enforced')
finally:
    shutil.rmtree(path)
'''
            assert 'INODE_QUOTA_ENFORCED' in probe('alice', inode)
            inspect(root, env['WORKSPACE_INSTANCE'])
            check(env, restored=True)
            project = registry(root)['homes'][home('alice')]['project_id']
            run('xfs_quota', '-x', '-c', f'limit -p bhard=0 {project}', str(root))
            try:
                inspect(root, env['WORKSPACE_INSTANCE'])
            except ValueError:
                status = json.loads((root / '.educloud-status' / (env['WORKSPACE_INSTANCE'] + '.json')).read_text())
                assert status['healthy'] is False
            else:
                raise AssertionError('quota drift was not detected')
            print('PASS: hard byte/inode quotas, peer independence, notebook kernels and drift detection.')
        finally:
            cleanup(env['WORKSPACE_INSTANCE'], directory / 'pilot.env')
            run('umount', str(root))


if __name__ == '__main__':
    main()
