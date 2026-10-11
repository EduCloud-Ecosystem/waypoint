#!/usr/bin/env python3
"""Check a versioned course contract and render private, reviewable configuration.

Checking is offline: it proves configuration consistency, never host readiness.
Secrets are references in the manifest and read only while rendering.
"""
import argparse
from copy import deepcopy
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
from urllib.parse import urlsplit

from health import DEFAULT_THRESHOLDS
from public_course import render as render_public
from deployment import validate_institutional

MAX_MANIFEST_BYTES = 256 * 1024
MAX_SECRET_BYTES = 4096
SUBMISSION_BUDGET = (32, 1024**2, 4 * 1024**2)
SCHEMA = {
    'images': {'hub', 'course'},
    'identity': {'issuer', 'client_id', 'client_secret_file', 'subjects'},
    'workspace': {'origin', 'port'},
    'storage': {'home_root', 'quota_mb', 'inode_limit'},
    'capacity': {'memory_mb', 'cpu', 'active_limit', 'idle_seconds', 'max_age_seconds'},
    'backup': {'repository', 'password_file', 'staging_dir', 'keep_last', 'keep_daily', 'keep_weekly'},
    'owners': {'support', 'restore'},
}
TOP_FIELDS = {'version', 'scope', 'instance', 'cairn'} | set(SCHEMA)
GUIDANCE = {
    'schema': 'Use exactly the documented version 1 fields; secret values are not accepted.',
    'value': 'Use the documented bounded identifier or numeric value.',
    'immutable_image': 'Pin a credential-free repository digest or local sha256 image ID.',
    'origin': 'Use a dedicated lowercase HTTPS DNS origin without path or credentials.',
    'issuer': 'Use a credential-free HTTPS Keycloak realm URL.',
    'subjects': 'Supply unique stable lowercase subject IDs, with no email addresses.',
    'path': 'Use a dedicated canonical absolute path with no symlink ancestors or traversal.',
    'overlap': 'Keep home storage, backup staging, repository, secret files, and deployment output separate.',
    'repository': 'Use credential-free SFTP or HTTPS S3; local repositories require synthetic scope.',
    'budget': 'Use the supported saved-file limits: 32 files, 1048576 bytes per file, 4194304 total bytes.',
    'binding': 'Match the Cairn workspace URL to its origin and backup staging to the render output directory.',
    'owner': 'Name an accountable support and restore owner.',
    'secret': 'Use an owner-only regular secret file with a bounded literal value.',
    'output': 'Use a new canonical output directory under an existing owner-controlled parent.',
    'parse': 'Supply a bounded regular UTF-8 JSON object with unique fields.',
    'render': 'Inspect private local configuration; rendering failed without accepted output.',
}


class ManifestError(ValueError):
    """Contains public-safe field paths and fixed codes, never input/parser text."""
    def __init__(self, issues):
        self.issues = issues
        super().__init__('course_manifest_refused')


def issue(field, code):
    return {'field': field, 'code': code, 'guidance': GUIDANCE[code]}


def _literal(value):
    return (isinstance(value, str) and bool(value) and len(value) <= MAX_SECRET_BYTES
            and all(33 <= ord(c) <= 126 for c in value)
            and not any(c in value for c in ('$', '"', "'", '#', '\\')))


def _path(value, check_links=True):
    if not isinstance(value, str) or len(value) > 512 or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value):
        return False
    path = Path(value)
    if str(path) != value or any(part in ('.', '..') for part in path.parts) or len(path.parts) < 3:
        return False
    if not check_links:
        return True
    try:
        for ancestor in reversed((path,) + tuple(path.parents)):
            if ancestor.is_symlink():
                return False
            if ancestor.exists() and ancestor != path and not ancestor.is_dir():
                return False
    except OSError:
        return False
    return True


def _origin(value, loopback=False):
    if not isinstance(value, str) or len(value) > 512 or not _literal(value):
        return False
    try:
        url = urlsplit(value)
        if url.username or url.password or url.query or url.fragment or url.path or not url.hostname:
            return False
        if loopback and url.scheme == 'http' and url.hostname == '127.0.0.1':
            return url.port is not None and 1024 <= url.port <= 65535 and value == 'http://127.0.0.1:' + str(url.port)
        return (url.scheme == 'https' and (url.port is None or 1024 <= url.port <= 65535)
                and re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', url.hostname)
                and value == 'https://' + url.hostname + (':' + str(url.port) if url.port is not None else ''))
    except ValueError:
        return False


def _image(value):
    if not isinstance(value, str) or len(value) > 512:
        return False
    if re.fullmatch(r'sha256:[0-9a-f]{64}', value):
        return True
    parts = value.split('@sha256:')
    if len(parts) != 2 or not re.fullmatch(r'[0-9a-f]{64}', parts[1]):
        return False
    repository = parts[0]
    if not re.fullmatch(r'[a-z0-9][a-z0-9._/:-]*', repository) or '//' in repository:
        return False
    segments = repository.split('/')
    names = segments[1:] if ':' in segments[0] else segments
    if any(not re.fullmatch(r'[a-z0-9]+(?:[._-][a-z0-9]+)*', segment) for segment in names):
        return False
    if ':' in repository:
        if len(segments) < 2 or any(':' in segment for segment in segments[1:]):
            return False
        host, _, port = segments[0].partition(':')
        if not re.fullmatch(r'[a-z0-9]+(?:[.-][a-z0-9]+)*', host) or not port.isdecimal() or not 1 <= int(port) <= 65535:
            return False
    return True


def _overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def validate_manifest(manifest):
    """Return bounded public-safe refusals without reading secrets or contacting hosts."""
    problems = []
    def reject(field, code):
        problems.append(issue(field, code))
    if not isinstance(manifest, dict) or set(manifest) != TOP_FIELDS:
        return [issue('$', 'schema')]
    if type(manifest['version']) is not int or manifest['version'] != 1:
        reject('version', 'schema')
    if manifest['scope'] not in ('institutional', 'synthetic'):
        reject('scope', 'value')
    if not isinstance(manifest['instance'], str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', manifest['instance']):
        reject('instance', 'value')
    for section, keys in SCHEMA.items():
        if not isinstance(manifest[section], dict) or set(manifest[section]) != keys:
            reject(section, 'schema')
    cairn = manifest['cairn']
    active_fields = {'enabled', 'origin', 'classroom_id', 'workspace_url', 'max_files', 'max_file_bytes', 'max_total_bytes'}
    if (not isinstance(cairn, dict) or type(cairn.get('enabled')) is not bool
            or set(cairn) != (active_fields if cairn.get('enabled') is True else {'enabled'})):
        reject('cairn', 'schema')
    if any(problem['code'] == 'schema' for problem in problems):
        return problems
    def numeric(section, key, low, high, integer=True):
        value = manifest[section][key]
        if (type(value) not in ((int,) if integer else (int, float))
                or not low <= value <= high or not math.isfinite(value)):
            reject(section + '.' + key, 'value')
    for role, reference in manifest['images'].items():
        if not _image(reference):
            reject('images.' + role, 'immutable_image')
    identity = manifest['identity']
    try:
        issuer = urlsplit(identity['issuer']) if isinstance(identity['issuer'], str) else None
        valid_issuer = (issuer is not None and _literal(identity['issuer']) and len(identity['issuer']) <= 512
            and issuer.scheme == 'https' and issuer.hostname and not issuer.username and not issuer.password
            and not issuer.query and not issuer.fragment and (issuer.port is None or 1024 <= issuer.port <= 65535)
            and re.fullmatch(r'/realms/[A-Za-z0-9_-]{1,128}', issuer.path)
            and _origin('https://' + issuer.hostname + (':' + str(issuer.port) if issuer.port is not None else ''))
            and identity['issuer'] == 'https://' + issuer.hostname + (':' + str(issuer.port) if issuer.port is not None else '') + issuer.path)
    except ValueError:
        valid_issuer = False
    if not valid_issuer:
        reject('identity.issuer', 'issuer')
    if not isinstance(identity['client_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identity['client_id']):
        reject('identity.client_id', 'value')
    subjects = identity['subjects']
    if (not isinstance(subjects, list) or not 1 <= len(subjects) <= 1000
            or any(not isinstance(s, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', s) for s in subjects)
            or len(set(s for s in subjects if isinstance(s, str))) != len(subjects)):
        reject('identity.subjects', 'subjects')
    if not _origin(manifest['workspace']['origin']):
        reject('workspace.origin', 'origin')
    numeric('workspace', 'port', 1024, 65535)
    if not isinstance(manifest['storage']['home_root'], str) or not re.fullmatch(r'/[A-Za-z0-9_/-]+', manifest['storage']['home_root']):
        reject('storage.home_root', 'path')
    numeric('storage', 'quota_mb', 16, 1024**2)
    numeric('storage', 'inode_limit', 128, 100000000)
    numeric('capacity', 'memory_mb', 128, 1024**2)
    numeric('capacity', 'cpu', .1, 1024, integer=False)
    numeric('capacity', 'active_limit', 1, 1000)
    numeric('capacity', 'idle_seconds', 60, 86400)
    numeric('capacity', 'max_age_seconds', 60, 7 * 86400)
    if (type(manifest['capacity']['idle_seconds']) is int and type(manifest['capacity']['max_age_seconds']) is int
            and manifest['capacity']['idle_seconds'] > manifest['capacity']['max_age_seconds']):
        reject('capacity.idle_seconds', 'value')
    for key in ('keep_last', 'keep_daily', 'keep_weekly'):
        numeric('backup', key, 1, 365)
    for role in ('support', 'restore'):
        owner = manifest['owners'][role]
        if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,127}', owner) or not owner.strip():
            reject('owners.' + role, 'owner')
    path_fields = [('storage.home_root', manifest['storage']['home_root']),
                   ('identity.client_secret_file', identity['client_secret_file']),
                   ('backup.password_file', manifest['backup']['password_file']),
                   ('backup.staging_dir', manifest['backup']['staging_dir'])]
    repository = manifest['backup']['repository']
    repository_valid = False
    if isinstance(repository, str) and len(repository) <= 512 and _literal(repository):
        if repository.startswith('sftp:'):
            match = re.fullmatch(r'sftp:(?:[A-Za-z0-9_-]+@)?[A-Za-z0-9_.-]+:(/[A-Za-z0-9_/-]+)', repository)
            repository_valid = bool(match and _path(match[1], check_links=False))
        elif repository.startswith('s3:'):
            try:
                url = urlsplit(repository[3:])
                repository_valid = bool(url.scheme == 'https' and url.hostname and not url.username and not url.password
                    and not url.query and not url.fragment and url.port is None
                    and re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9_/-]*', url.path)
                    and all(part not in ('.', '..') for part in url.path.split('/')))
            except ValueError:
                pass
        elif manifest['scope'] == 'synthetic' and _path(repository):
            repository_valid = True
            path_fields.append(('backup.repository', repository))
    if not repository_valid:
        reject('backup.repository', 'repository')
    for field, raw in [('storage.home_root', manifest['storage']['home_root']),
                       ('backup.staging_dir', manifest['backup']['staging_dir'])] + ([('backup.repository', repository)] if isinstance(repository, str) and repository.startswith('/') else []):
        if isinstance(raw, str) and _path(raw):
            path = Path(raw)
            try:
                if path.exists() and not path.is_dir():
                    reject(field, 'path')
            except OSError:
                reject(field, 'path')
            if any(raw == root or raw.startswith(root + '/') for root in ('/etc', '/bin', '/sbin', '/usr', '/proc', '/sys', '/dev', '/boot', '/var/lib/docker', '/System', '/Library')):
                reject(field, 'path')
    valid_paths = []
    for field, value in path_fields:
        if not _path(value):
            reject(field, 'path')
        else:
            valid_paths.append((field, Path(value)))
    for index, (field, path) in enumerate(valid_paths):
        for _, other in valid_paths[index + 1:]:
            if _overlap(path, other):
                reject(field, 'overlap')
                break
    if cairn['enabled']:
        if not _origin(cairn['origin'], loopback=manifest['scope'] == 'synthetic'):
            reject('cairn.origin', 'origin')
        if not isinstance(cairn['classroom_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', cairn['classroom_id']):
            reject('cairn.classroom_id', 'value')
        if cairn['workspace_url'] != manifest['workspace']['origin']:
            reject('cairn.workspace_url', 'binding')
        for key, limit in zip(('max_files', 'max_file_bytes', 'max_total_bytes'), SUBMISSION_BUDGET):
            if type(cairn[key]) is not int or cairn[key] != limit:
                reject('cairn.' + key, 'budget')
    return problems


def _read_regular(path, limit, private=False):
    """Open without following any path component; O_NONBLOCK prevents FIFO stalls."""
    if not _path(str(path)):
        raise OSError('unsafe_path')
    components = Path(path).parts[1:]
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    fd = None
    try:
        for part in components[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(components[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit
                or (private and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077))):
            raise OSError('unsafe_file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise OSError('oversized_file')
        return raw
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_field')
        value[key] = item
    return value


def load_manifest(path):
    try:
        value = json.loads(_read_regular(Path(path), MAX_MANIFEST_BYTES), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid_constant')))
        if not isinstance(value, dict):
            raise ValueError('invalid_object')
        return value
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ManifestError([issue('$', 'parse')]) from None


def validation_record(manifest, problems=None):
    problems = validate_manifest(manifest) if problems is None else problems
    return {'version': 1, 'status': 'refused' if problems else 'valid',
            'scope': 'offline-configuration-check', 'issues': problems,
            'limits': ['No host, identity, backup, capacity, or production acceptance.']}


def build_configuration(manifest, output_directory, client_secret):
    """Build contents without filesystem mutations; never returns a public report."""
    problems = validate_manifest(manifest)
    if problems:
        raise ManifestError(problems)
    if not _literal(client_secret):
        raise ManifestError([issue('identity.client_secret_file', 'secret')])
    output = Path(output_directory)
    if not _path(str(output)):
        raise ManifestError([issue('output', 'output')])
    if manifest['backup']['staging_dir'] != str(output):
        raise ManifestError([issue('backup.staging_dir', 'binding')])
    workspace, identity, storage, capacity = (manifest[k] for k in ('workspace', 'identity', 'storage', 'capacity'))
    nginx, client, env = render_public(urlsplit(workspace['origin']).hostname, identity['issuer'], identity['client_id'],
                                      manifest['instance'], identity['subjects'], storage['home_root'], workspace['port'])
    env['WORKSPACE_PUBLIC_URL'] = workspace['origin']
    client['redirectUris'] = [workspace['origin'] + '/hub/oauth_callback']
    client['webOrigins'] = [workspace['origin']]
    tls_port = urlsplit(workspace['origin']).port
    if tls_port is not None:
        nginx = nginx.replace('listen 443 ssl', 'listen ' + str(tls_port) + ' ssl')
        nginx = nginx.replace('https://$host$request_uri', 'https://$host:' + str(tls_port) + '$request_uri')
    env.update(WORKSPACE_HUB_IMAGE=manifest['images']['hub'], WORKSPACE_IMAGE=manifest['images']['course'],
               WORKSPACE_ENV_FILE=str(output / '.env'), WORKSPACE_OIDC_CLIENT_SECRET=client_secret,
               WORKSPACE_HOME_QUOTA_MB=str(storage['quota_mb']), WORKSPACE_HOME_INODE_LIMIT=str(storage['inode_limit']),
               WORKSPACE_MEMORY_MB=str(capacity['memory_mb']), WORKSPACE_CPU_LIMIT=str(capacity['cpu']),
               WORKSPACE_ACTIVE_LIMIT=str(capacity['active_limit']), WORKSPACE_IDLE_SECONDS=str(capacity['idle_seconds']),
               WORKSPACE_MAX_AGE_SECONDS=str(capacity['max_age_seconds']))
    cairn = manifest['cairn']
    if cairn['enabled']:
        env.update(WORKSPACE_CAIRN_ORIGIN=cairn['origin'], WORKSPACE_CAIRN_LOCAL_TEST='1' if manifest['scope'] == 'synthetic' else '0')
    validate_institutional(env)
    backup = manifest['backup']
    recovery = {'version': 1, 'repository': backup['repository'], 'password_file': backup['password_file'],
                'local_test': manifest['scope'] == 'synthetic', **{key: backup[key] for key in ('keep_last', 'keep_daily', 'keep_weekly')}}
    health = {'version': 1, 'instance': manifest['instance'], 'deployment_dir': str(output),
              'staging_dir': backup['staging_dir'], 'home_root': storage['home_root'],
              'docker_socket': '/var/run/docker.sock', 'docker_root': '/var/lib/docker',
              'hub_url': 'http://127.0.0.1:' + str(workspace['port']) + '/hub/health',
              'backup_status': str(output / 'recovery-status.json'),
              'quota_status': storage['home_root'] + '/.educloud-status/' + manifest['instance'] + '.json',
              'tls_url': workspace['origin'], 'timeout_seconds': 5, 'thresholds': deepcopy(DEFAULT_THRESHOLDS)}
    binding = {'version': 1, 'enabled': cairn['enabled'],
               'instruction': 'An instructor must review and apply the classroom workspace URL; enabling the approved classroom requires a Cairn operator.'}
    if cairn['enabled']:
        binding.update(classroom_id=cairn['classroom_id'], workspace_url=cairn['workspace_url'],
                       cairn_origin=cairn['origin'], operator_environment={'CAIRN_SUBMISSIONS': 'true', 'CAIRN_SUBMISSION_CLASSROOMS': cairn['classroom_id']},
                       budgets={key: cairn[key] for key in ('max_files', 'max_file_bytes', 'max_total_bytes')})
    normalized = deepcopy(manifest)
    normalized['identity']['subjects'] = sorted(normalized['identity']['subjects'])
    files = {'.env': ''.join(f'{key}={value}\n' for key, value in env.items()), 'nginx.conf': nginx}
    for name, data in [('keycloak-client.json', client), ('recovery.json', recovery), ('health.json', health),
                       ('cairn-binding.json', binding), ('manifest.json', normalized), ('validation.json', validation_record(manifest))]:
        files[name] = json.dumps(data, indent=2, allow_nan=False) + '\n'
    return files


def _publish_directory(staging, output):
    """Atomically publish without replacing even a concurrently created empty dir."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith('linux'):
        function = getattr(libc, 'renameat2', None)
        if function is None:
            raise OSError('atomic_publication_unavailable')
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(-100, os.fsencode(staging), -100, os.fsencode(output), 1)  # RENAME_NOREPLACE
    elif sys.platform == 'darwin':
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(os.fsencode(staging), os.fsencode(output), 4)  # RENAME_EXCL
    else:
        raise OSError('atomic_publication_unavailable')
    if result != 0:
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise ManifestError([issue('output', 'output')])
        raise OSError('atomic_publication_failed')


def render_manifest(manifest, output_directory):
    problems = validate_manifest(manifest)
    if problems:
        raise ManifestError(problems)
    output = Path(output_directory)
    protected = [manifest['storage']['home_root'],
                 manifest['identity']['client_secret_file'], manifest['backup']['password_file']]
    if manifest['backup']['repository'].startswith('/'):
        protected.append(manifest['backup']['repository'])
    try:
        if (not _path(str(output)) or output.exists() or output.is_symlink() or not output.parent.is_dir()
                or any(_overlap(output, Path(raw)) for raw in protected)):
            raise ManifestError([issue('output', 'output')])
    except OSError:
        raise ManifestError([issue('output', 'output')]) from None
    if manifest['backup']['staging_dir'] != str(output):
        raise ManifestError([issue('backup.staging_dir', 'binding')])
    try:
        parent_info = output.parent.stat()
        if parent_info.st_uid != os.geteuid() or parent_info.st_mode & 0o022:
            raise OSError('unsafe_output_parent')
        values = []
        for field, minimum in [('identity.client_secret_file', 1), ('backup.password_file', 24)]:
            section, key = field.split('.')
            try:
                value = _read_regular(Path(manifest[section][key]), MAX_SECRET_BYTES, private=True).decode('utf-8')
                # Accept a single conventional terminal newline, but not embedded or padded data.
                if value.endswith('\n'):
                    value = value[:-1]
                if not _literal(value) or len(value) < minimum:
                    raise ValueError('invalid_secret')
            except (OSError, ValueError, UnicodeError):
                raise ManifestError([issue(field, 'secret')]) from None
            values.append(value)
        files = build_configuration(manifest, output, values[0])
        staging = Path(tempfile.mkdtemp(prefix='.course-manifest-', dir=output.parent))
        try:
            os.chmod(staging, 0o700)
            for name, content in files.items():
                fd = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            _publish_directory(staging, output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return validation_record(manifest)
    except ManifestError:
        raise
    except (OSError, ValueError, TypeError):
        raise ManifestError([issue('output', 'render')]) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('check', 'render'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.operation == 'render':
            if args.output is None:
                raise ManifestError([issue('output', 'output')])
            record = render_manifest(manifest, args.output)
        else:
            problems = validate_manifest(manifest)
            if args.output is not None and not problems:
                if not _path(str(args.output)):
                    problems.append(issue('output', 'output'))
                elif manifest['backup']['staging_dir'] != str(args.output):
                    problems.append(issue('backup.staging_dir', 'binding'))
            record = validation_record(manifest, problems)
        print(json.dumps(record, indent=2))
        return 1 if record['issues'] else 0
    except ManifestError as exc:
        print(json.dumps({'version': 1, 'status': 'refused', 'scope': 'offline-configuration-check', 'issues': exc.issues}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
