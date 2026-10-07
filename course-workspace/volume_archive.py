"""Runs only in a disposable, network-disabled archive helper container."""
import os
import sys
import tarfile

mode, uid, gid = sys.argv[1:]
archive = '/archive/data.tar.gz'
if mode == 'pack':
    with tarfile.open(archive, 'w:gz') as out:
        out.add('/data', arcname='.', recursive=True)
    # Set permissions while the helper still owns the file. After chown,
    # Linux requires CAP_FOWNER for chmod; we deliberately do not grant it.
    os.chmod(archive, 0o600)
    os.chown(archive, int(uid), int(gid))
elif mode == 'unpack':
    if os.listdir('/data'):
        raise RuntimeError('restore destination is not empty')
    with tarfile.open(archive, 'r:gz') as source:
        source.extractall('/data', filter='data')
    # Own only this new volume, never follow links to change another path.
    for parent, dirs, files in os.walk('/data', followlinks=False):
        for name in dirs + files:
            os.lchown(os.path.join(parent, name), int(uid), int(gid))
        os.lchown(parent, int(uid), int(gid))
else:
    raise ValueError('unknown archive operation')
