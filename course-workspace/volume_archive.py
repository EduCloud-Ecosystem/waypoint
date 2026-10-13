"""Runs only in a disposable, network-disabled archive helper container."""
import os
import stat
import sys
import tarfile


def ordinary_permissions(member):
    permissions = stat.S_IMODE(member.mode)
    if permissions & ~0o777:
        raise ValueError('archive special permission bits are unsupported')
    return member.replace(mode=permissions, deep=False)


def preserved_data_filter(member, destination):
    """Retain ordinary metadata only after the standard containment checks."""
    member = ordinary_permissions(member)
    filtered = tarfile.data_filter(member, destination)
    if filtered is None:
        return None
    if filtered.isfile() or filtered.isdir() or filtered.islnk():
        filtered = filtered.replace(mode=member.mode, deep=False)
    if filtered.issym() or filtered.islnk():
        # data_filter may normalize linkname. Check the original filesystem
        # interpretation too before preserving the source's literal target.
        base = destination if filtered.islnk() else os.path.dirname(os.path.join(destination, filtered.name))
        target = os.path.realpath(os.path.join(base, member.linkname))
        root = os.path.realpath(destination)
        if os.path.isabs(member.linkname) or os.path.commonpath((root, target)) != root:
            raise ValueError('archive link target escapes the destination')
        filtered = filtered.replace(linkname=member.linkname, deep=False)
    return filtered


def pack(source, archive, uid, gid):
    with tarfile.open(archive, 'w:gz') as out:
        out.add(source, arcname='.', recursive=True, filter=ordinary_permissions)
    # chmod while still owned: the helper intentionally lacks CAP_FOWNER.
    os.chmod(archive, 0o600)
    os.chown(archive, uid, gid)


def unpack(destination, archive, uid, gid):
    if not stat.S_ISDIR(os.lstat(destination).st_mode) or os.listdir(destination):
        raise RuntimeError('restore destination must be an empty real directory')
    # A new quota home is already learner-owned. Temporarily own only its
    # verified empty root, so ordinary modes can be set without CAP_FOWNER.
    os.lchown(destination, os.geteuid(), os.getegid())
    try:
        with tarfile.open(archive, 'r:gz') as source:
            source.extractall(destination, filter=preserved_data_filter)
    finally:
        # Normalize ownership only inside this new volume, never follow links.
        # Partial failure remains closed and retains the established owner.
        for parent, dirs, files in os.walk(destination, followlinks=False):
            for name in dirs + files:
                os.lchown(os.path.join(parent, name), uid, gid)
            os.lchown(parent, uid, gid)


def main():
    mode, uid, gid = sys.argv[1:]
    uid, gid = int(uid), int(gid)
    archive = '/archive/data.tar.gz'
    if mode == 'pack':
        pack('/data', archive, uid, gid)
    elif mode == 'unpack':
        unpack('/data', archive, uid, gid)
    else:
        raise ValueError('unknown archive operation')


if __name__ == '__main__':
    main()
