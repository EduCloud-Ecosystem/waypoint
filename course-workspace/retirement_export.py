#!/usr/bin/env python3
"""Private portable coursework exports from completed retired institutional courses.

Only work/ from every retained home is exported. Other home state remains in the
protected encrypted snapshot. Student-authored work may itself contain secrets;
this tool does not classify its contents or publish/upload any learner data.
"""
from functools import wraps
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid

from course_manifest import _publish_directory, _read_regular, _unique_object
from maintenance import safe_directory, home_lock, snapshot, stopped, digest, sync_directory, PENDING as MAINTENANCE_PENDING, ROOT_PENDING
from pilot import locked
from backup import docker
import upgrade

VERSION = 'educloud-retirement-work-export-v1'
SCOPE = 'learner-work-directories'
MAX_ENTRIES = 50000
MAX_FILE_BYTES = 256 * 1024**2
MAX_TOTAL_BYTES = 4 * 1024**3
MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_READ = 1024**2
HASH = re.compile(r'[0-9a-f]{64}\Z')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
SUFFIX = re.compile(r'home-[0-9a-f]{24}\Z')
PACK_SCRIPT = '''import os, stat, tarfile
path='/data/work'
if os.path.lexists(path):
    info=os.lstat(path)
    if not stat.S_ISDIR(info.st_mode): raise ValueError('unsafe_work_root')
with tarfile.open('/archive/data.tar.gz','w:gz',format=tarfile.PAX_FORMAT,dereference=False) as out:
    if os.path.exists(path): out.add(path,arcname='work',recursive=True)
os.chmod('/archive/data.tar.gz',0o600)
'''


class ExportError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def guarded(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ExportError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError,
                UnicodeError, RecursionError, subprocess.SubprocessError, tarfile.TarError, EOFError):
            raise ExportError('export_refused_or_incomplete') from None
    return run


def canonical_directory(path, private=True):
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_dir()
            or path.stat().st_uid != os.geteuid() or (private and path.stat().st_mode & 0o077)):
        raise ExportError('private_canonical_directory_required')
    return path


def file_hash(path, limit=MAX_ARCHIVE_BYTES):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                or before.st_uid != os.geteuid() or before.st_mode & 0o077):
            raise ExportError('private_bounded_file_required')
        result = hashlib.sha256(); count = 0
        while block := os.read(descriptor, MAX_READ):
            count += len(block)
            if count > limit: raise ExportError('export_byte_budget_exceeded')
            result.update(block)
        if stamp(before) != stamp(os.fstat(descriptor)) or count != before.st_size:
            raise ExportError('file_changed_during_read')
        return result.hexdigest(), count
    finally:
        os.close(descriptor)


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def safe_name(name):
    if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 1024
            or any(ord(char) < 32 or ord(char) == 127 for char in name) or '\\' in name):
        raise ExportError('unsafe_archive_path')
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ExportError('unsafe_archive_path')
    if path.parts[0] != 'work':
        raise ExportError('archive_outside_work_scope')
    return name


def inventory(home, budget):
    """Read selected source bytes via no-follow descriptors; never execute files."""
    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    entries = []; outside = 0; present = False
    def count():
        budget['entries'] += 1
        if budget['entries'] > MAX_ENTRIES: raise ExportError('export_entry_budget_exceeded')
    def omitted(parent):
        total = 0
        for name in os.listdir(parent):
            count(); total += 1
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try: total += omitted(child)
                finally: os.close(child)
        return total
    def selected(parent, prefix):
        for name in sorted(os.listdir(parent)):
            path = safe_name(prefix+'/'+name); count()
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    if stamp(before) != stamp(os.fstat(child)): raise ExportError('source_changed_during_read')
                    entries.append({'path':path, 'type':'directory'})
                    selected(child, path)
                    if stamp(before) != stamp(os.fstat(child)): raise ExportError('source_changed_during_read')
                finally: os.close(child)
            elif stat.S_ISREG(before.st_mode):
                if before.st_size > MAX_FILE_BYTES: raise ExportError('export_file_budget_exceeded')
                child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                try:
                    if stamp(before) != stamp(os.fstat(child)): raise ExportError('source_changed_during_read')
                    value = hashlib.sha256(); size = 0
                    while block := os.read(child, MAX_READ):
                        size += len(block); budget['bytes'] += len(block)
                        if size > MAX_FILE_BYTES or budget['bytes'] > MAX_TOTAL_BYTES:
                            raise ExportError('export_byte_budget_exceeded')
                        value.update(block)
                    if size != before.st_size or stamp(before) != stamp(os.fstat(child)):
                        raise ExportError('source_changed_during_read')
                    entries.append({'path':path, 'type':'file', 'bytes':size, 'sha256':value.hexdigest()})
                finally: os.close(child)
            else:
                raise ExportError('work_links_or_special_files_refused')
    try:
        before = os.fstat(descriptor)
        for name in sorted(os.listdir(descriptor)):
            if name != 'work':
                count(); outside += 1
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try: outside += omitted(child)
                    finally: os.close(child)
        if 'work' in os.listdir(descriptor):
            present = True; count()
            info = os.stat('work', dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode): raise ExportError('work_root_not_regular_directory')
            child = os.open('work', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                root_before = os.fstat(child)
                entries.append({'path':'work', 'type':'directory'}); selected(child, 'work')
                if stamp(root_before) != stamp(os.fstat(child)): raise ExportError('source_changed_during_read')
            finally: os.close(child)
        if stamp(before) != stamp(os.fstat(descriptor)): raise ExportError('source_changed_during_read')
    finally:
        os.close(descriptor)
    return {'work_present':present, 'outside_work_entries':outside, 'entries':sorted(entries, key=lambda item:item['path'])}


class BoundedReader:
    def __init__(self, source): self.source, self.total = source, 0
    def read(self, size=-1):
        if size < 0 or size > MAX_READ: raise ExportError('unsafe_archive_header_budget')
        value = self.source.read(size); self.total += len(value)
        if self.total > MAX_TOTAL_BYTES + MAX_ENTRIES*4096: raise ExportError('archive_uncompressed_budget_exceeded')
        return value


class BoundedTarInfo(tarfile.TarInfo):
    """Reject oversized extension metadata before tarfile allocates its body."""
    def _proc_pax(self, archive):
        if self.type == tarfile.XGLTYPE or not 0 <= self.size <= 16 * 1024:
            raise ExportError('unsafe_archive_metadata_budget')
        return super()._proc_pax(archive)
    def _proc_gnulong(self, *args, **kwargs):
        raise ExportError('gnu_long_metadata_refused')
    def _proc_sparse(self, *args, **kwargs):
        raise ExportError('sparse_archive_refused')
    def _proc_gnusparse_00(self, *args, **kwargs):
        raise ExportError('sparse_archive_refused')
    def _proc_gnusparse_01(self, *args, **kwargs):
        raise ExportError('sparse_archive_refused')
    def _proc_gnusparse_10(self, *args, **kwargs):
        raise ExportError('sparse_archive_refused')


def archive_inventory(path):
    """Verify untrusted tar metadata and hash every selected member; never extract."""
    archive_before = os.stat(path, follow_symlinks=False)
    archive_hash, archive_bytes = file_hash(path)
    entries = []; names = set(); payload_bytes = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        original = os.fstat(descriptor)
        if stamp(archive_before) != stamp(original): raise ExportError('archive_changed_during_read')
        with os.fdopen(descriptor, 'rb', closefd=False) as raw, gzip.GzipFile(fileobj=raw, mode='rb') as compressed:
            bounded = BoundedReader(compressed)
            with tarfile.open(fileobj=bounded, mode='r|', bufsize=10240, tarinfo=BoundedTarInfo) as archive:
                for member in archive:
                    if len(entries) >= MAX_ENTRIES: raise ExportError('export_entry_budget_exceeded')
                    name = safe_name(member.name[:-1] if member.isdir() and member.name.endswith('/') else member.name)
                    if name in names: raise ExportError('duplicate_archive_member')
                    names.add(name)
                    if member.sparse is not None or set(member.pax_headers)-{'path','mtime','atime','ctime'}:
                        raise ExportError('unsafe_archive_metadata')
                    for key, value in member.pax_headers.items():
                        if key == 'path' and value != member.name: raise ExportError('unsafe_archive_metadata')
                        if key != 'path' and (not isinstance(value, str) or len(value)>64
                                or not re.fullmatch(r'-?[0-9]+(?:\.[0-9]+)?', value)):
                            raise ExportError('unsafe_archive_metadata')
                    if member.isdir():
                        if member.size != 0: raise ExportError('unsafe_archive_directory')
                        entries.append({'path':name, 'type':'directory'})
                    elif member.isfile():
                        if type(member.size) is not int or not 0 <= member.size <= MAX_FILE_BYTES:
                            raise ExportError('export_file_budget_exceeded')
                        payload_bytes += member.size
                        if payload_bytes > MAX_TOTAL_BYTES: raise ExportError('export_byte_budget_exceeded')
                        stream = archive.extractfile(member)
                        if stream is None: raise ExportError('archive_member_missing')
                        value = hashlib.sha256(); count = 0
                        with stream:
                            while block := stream.read(MAX_READ): count += len(block); value.update(block)
                        if count != member.size: raise ExportError('truncated_archive_member')
                        entries.append({'path':name, 'type':'file', 'bytes':count, 'sha256':value.hexdigest()})
                    else:
                        raise ExportError('archive_links_or_special_files_refused')
                # Consume through tar's buffered reader: this includes bytes
                # read ahead beyond the first EOF block, plus gzip CRC checks.
                while remainder := archive.fileobj.read(MAX_READ):
                    if any(remainder): raise ExportError('trailing_archive_payload')
        if stamp(original) != stamp(os.fstat(descriptor)):
            raise ExportError('archive_changed_during_read')
    finally:
        os.close(descriptor)
    types = {item['path']:item['type'] for item in entries}
    if entries and types.get('work') != 'directory': raise ExportError('archive_work_root_missing')
    for name in types:
        parent = PurePosixPath(name).parent
        while str(parent) != '.':
            if types.get(str(parent)) != 'directory': raise ExportError('archive_parent_directory_missing_or_conflicting')
            parent = parent.parent
    return {'entries':sorted(entries,key=lambda item:item['path']), 'archive_sha256':archive_hash, 'archive_bytes':archive_bytes}


def pack_work(image, volume, scratch):
    name = 'educloud-export-'+uuid.uuid4().hex[:12]
    command = ['docker','run','--name',name,'--rm','--network','none','--read-only',
        '--cap-drop','ALL','--cap-add','DAC_OVERRIDE','--cap-add','CHOWN','--security-opt','no-new-privileges:true',
        '--memory','512m','--cpus','1','--pids-limit','64','--pull','never','--user','0:0',
        '--label','educloud.workspace.instance='+volume.rsplit('-home-',1)[0],
        '--label','educloud.workspace.kind=export',
        '--mount','type=volume,source='+volume+',target=/data,readonly',
        '--mount','type=bind,source='+str(scratch)+',target=/archive',
        image,'python','-c',PACK_SCRIPT+f"os.chown('/archive/data.tar.gz',{os.getuid()},{os.getgid()})\n"]
    try:
        subprocess.run(command, capture_output=True, timeout=300, check=True)
    finally:
        subprocess.run(['docker','rm','--force',name],capture_output=True,timeout=30,check=False)
        if docker('ps','-aq','--filter','name=^/'+name+'$'):
            raise ExportError('export_helper_cleanup_unverified')


def retained_state(directory, env, ledger):
    current = snapshot(directory, env)
    root = Path(env['WORKSPACE_HOME_ROOT']); names = sorted(current['homes'])
    stopped(env['WORKSPACE_INSTANCE'], names, False)
    if ledger['source'] == 'operator_retirement':
        observed, counts = upgrade.state(directory, env, ledger['images'])
        expected = {key:ledger['bindings'][key] for key in ('env_sha256','policy_sha256','files_sha256','source_manifest_sha256')}
        if observed != expected or counts != ledger['counts']: raise ExportError('retired_source_changed')
        identity = ledger['images']['course']; manifest_hash = observed['source_manifest_sha256']
        stamp_value = observed
    else:
        identity = json.loads(docker('image','inspect',env['WORKSPACE_IMAGE']))[0]['Id']
        path = directory/'source-manifest.json'
        manifest_hash = hashlib.sha256(_read_regular(path,256*1024,private=True)).hexdigest() if path.exists() else hashlib.sha256(b'no-source-manifest').hexdigest()
        stamp_value = {'policy_sha256':digest(current), 'files_sha256':upgrade.tree_stamp([root/name for name in names])['sha256'],
                       'source_manifest_sha256':manifest_hash}
    actual = json.loads(docker('image','inspect',env['WORKSPACE_IMAGE']))[0]
    if not IMAGE.fullmatch(identity) or actual.get('Id') != identity or actual.get('Os') != 'linux':
        raise ExportError('retired_course_image_mismatch')
    return {'names':names,'course_image':identity,'policy_sha256':digest(current),
            'source_manifest_sha256':manifest_hash,'stamp':stamp_value}


def read_manifest(directory):
    raw = _read_regular(directory/'manifest.json', MAX_MANIFEST_BYTES, private=True)
    value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    expected = {'version','scope','course_image','policy_sha256','source_manifest_sha256','homes','digest'}
    if (not isinstance(value,dict) or set(value)!=expected or value['version']!=VERSION or value['scope']!=SCOPE
            or not isinstance(value['digest'],str) or value['digest']!=digest({k:v for k,v in value.items() if k!='digest'})
            or not isinstance(value['course_image'],str) or not IMAGE.fullmatch(value['course_image'])
            or any(not isinstance(value[k],str) or not HASH.fullmatch(value[k]) for k in ('policy_sha256','source_manifest_sha256'))
            or not isinstance(value['homes'],list) or not 1<=len(value['homes'])<=10000):
        raise ExportError('export_manifest_schema_or_digest_invalid')
    return value, raw


@guarded
def verify_export(destination):
    directory = canonical_directory(destination)
    manifest, raw = read_manifest(directory)
    names = {'manifest.json'}; suffixes=set(); total_bytes=0; file_count=0; entry_count=0; omitted=0
    for home in manifest['homes']:
        if (not isinstance(home,dict) or set(home)!={'suffix','work_present','outside_work_entries','entries','archive_sha256','archive_bytes'}
                or not isinstance(home['suffix'],str) or not SUFFIX.fullmatch(home['suffix']) or home['suffix'] in suffixes
                or type(home['work_present']) is not bool or type(home['outside_work_entries']) is not int
                or not 0<=home['outside_work_entries']<=MAX_ENTRIES or not isinstance(home['entries'],list)
                or len(home['entries'])>MAX_ENTRIES or not isinstance(home['archive_sha256'],str) or not HASH.fullmatch(home['archive_sha256'])
                or type(home['archive_bytes']) is not int or not 0<=home['archive_bytes']<=MAX_ARCHIVE_BYTES):
            raise ExportError('export_home_schema_invalid')
        suffixes.add(home['suffix']); name=home['suffix']+'.tar.gz'; names.add(name)
        observed=archive_inventory(directory/name)
        if any(observed[k]!=home[k] for k in ('entries','archive_sha256','archive_bytes')):
            raise ExportError('export_archive_or_inventory_mismatch')
        if not home['work_present'] and observed['entries']: raise ExportError('export_work_presence_mismatch')
        if home['work_present'] != any(item.get('path')=='work' and item.get('type')=='directory' for item in observed['entries']):
            raise ExportError('export_work_presence_mismatch')
        omitted += home['outside_work_entries']; entry_count += len(observed['entries'])
        for item in observed['entries']:
            if item['type']=='file': file_count+=1; total_bytes+=item['bytes']
        if total_bytes>MAX_TOTAL_BYTES or entry_count+omitted>MAX_ENTRIES:
            raise ExportError('export_aggregate_budget_exceeded')
    if set(os.listdir(directory))!=names or hashlib.sha256(_read_regular(directory/'manifest.json',MAX_MANIFEST_BYTES,private=True)).hexdigest()!=hashlib.sha256(raw).hexdigest():
        raise ExportError('export_directory_changed_or_contains_extra_files')
    return {'version':VERSION,'status':'VERIFIED','scope':SCOPE,'manifest_sha256':hashlib.sha256(raw).hexdigest(),
            'retained_homes':len(suffixes),'work_files':file_count,'work_bytes':total_bytes,'outside_work_entries':omitted}


@guarded
def export_course(directory, destination):
    import retirement
    directory, destination = Path(directory), Path(destination)
    safe_directory(directory)
    if (not destination.is_absolute() or destination.resolve()!=destination or destination.exists() or destination.is_symlink()
            or not destination.parent.is_dir() or destination.parent.stat().st_uid!=os.geteuid()
            or destination.parent.stat().st_mode&0o022):
        raise ExportError('new_private_destination_required')
    with locked(directory):
        ledger = retirement.read_retirement(directory)
        if ledger is None or (directory/retirement.PENDING).exists() or (directory/retirement.PENDING).is_symlink():
            raise ExportError('completed_retirement_required')
        seal_hash=hashlib.sha256(_read_regular(directory/retirement.MARKER,upgrade.MAX_JSON,private=True)).hexdigest()
        env=retirement.environment(directory); root=Path(env['WORKSPACE_HOME_ROOT'])
        if any(destination==path or destination in path.parents or path in destination.parents for path in (directory,root)):
            raise ExportError('export_destination_overlaps_source')
        with home_lock(root):
            pending_paths=(directory/retirement.PENDING,directory/upgrade.PENDING,directory/MAINTENANCE_PENDING,root/ROOT_PENDING,directory/'.resource-amendment-pending.json')
            if any(path.exists() or path.is_symlink() for path in pending_paths):
                raise ExportError('unfinished_operation_prevents_export')
            before=retained_state(directory,env,ledger)
            if ledger['source']=='operator_retirement':
                hub=upgrade.hub_data(env,ledger['images'])
                if destination==hub or destination in hub.parents or hub in destination.parents:
                    raise ExportError('export_destination_overlaps_hub_data')
            budget={'entries':0,'bytes':0}; inventories={name:inventory(root/name,budget) for name in before['names']}
            staging=Path(tempfile.mkdtemp(prefix='.coursework-export-',dir=destination.parent)); os.chmod(staging,0o700)
            try:
                manifest={'version':VERSION,'scope':SCOPE,'course_image':before['course_image'],
                          'policy_sha256':before['policy_sha256'],'source_manifest_sha256':before['source_manifest_sha256'],'homes':[]}
                for name in before['names']:
                    suffix=name.removeprefix(env['WORKSPACE_INSTANCE']+'-')
                    if not SUFFIX.fullmatch(suffix): raise ExportError('retained_home_identity_invalid')
                    with tempfile.TemporaryDirectory(prefix='.pack-',dir=staging) as scratch:
                        pack_work(before['course_image'],name,Path(scratch))
                        archived=archive_inventory(Path(scratch)/'data.tar.gz')
                        if archived['entries']!=inventories[name]['entries']: raise ExportError('archive_does_not_match_source_work')
                        os.rename(Path(scratch)/'data.tar.gz',staging/(suffix+'.tar.gz'))
                    manifest['homes'].append({'suffix':suffix,**inventories[name],
                        'archive_sha256':archived['archive_sha256'],'archive_bytes':archived['archive_bytes']})
                manifest['digest']=digest(manifest)
                raw=(json.dumps(manifest,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
                if len(raw)>MAX_MANIFEST_BYTES: raise ExportError('export_manifest_budget_exceeded')
                fd=os.open(staging/'manifest.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as out: out.write(raw); out.flush(); os.fsync(out.fileno())
                after=retained_state(directory,retirement.environment(directory),retirement.read_retirement(directory))
                if (any(path.exists() or path.is_symlink() for path in pending_paths)
                        or hashlib.sha256(_read_regular(directory/retirement.MARKER,upgrade.MAX_JSON,private=True)).hexdigest()!=seal_hash):
                    raise ExportError('retirement_state_changed_during_export')
                recheck_budget={'entries':0,'bytes':0}
                if (before!=after or inventories!={name:inventory(root/name,recheck_budget) for name in after['names']}):
                    raise ExportError('retired_source_changed_during_export')
                result=verify_export(staging)
                for path in staging.iterdir():
                    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                sync_directory(staging); _publish_directory(staging,destination); sync_directory(destination.parent)
                return dict(result,status='EXPORTED')
            finally:
                if staging.exists(): shutil.rmtree(staging)
