"""Bounded public evidence for synthetic rehearsals; never serialize environment/logs."""
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile

VERSION = 4
FIXTURE_VERSION = 'course-pilot-v4'
MAX_BYTES = 64 * 1024
FIXTURE_FILES = frozenset({'evidence.py', 'cairn_journey.py', 'verify_course.py',
                           'notebook.py', 'requirements.txt', 'assessment_passback.py',
                           'course_load.py', 'webhook_relay.py', 'accessibility.py', 'submission_handoff.py'})
LOCAL_PHASES = frozenset({'forgejo_oauth', 'roster_isolation', 'submission_grading',
                          'webhook_replay', 'reviewed_assessment', 'lti_simulation',
                          'course_load', 'restart_persistence', 'submission_handoff'})
REQUIRED_PHASES = {
    'local-subset': LOCAL_PHASES,
    'full-linux': LOCAL_PHASES | {'notebook_python_r', 'notebook_persistence',
                                 'notebook_isolation', 'notebook_revocation', 'bounded_load', 'course_manifest'},
}
IMAGE_ROLES = frozenset({'course', 'checkout', 'forgejo', 'keycloak', 'nginx', 'hub'})
HASH = re.compile(r'[0-9a-f]{64}\Z')
REVISION = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')
SLUG = re.compile(r'[a-z][a-z0-9_-]{0,79}\Z')
PUBLIC_VALUE = re.compile(r'[A-Za-z0-9._+() -]{1,120}\Z')


def _command(*args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('provenance command failed') from exc
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError('provenance command failed or exceeded output bound')
    return result.stdout.strip()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def dashboard_digest(directory):
    """Hash built asset names and bytes without exposing local paths or filenames."""
    directory = Path(directory)
    if directory.is_symlink() or not (directory / 'index.html').is_file():
        raise ValueError('build the reviewed Cairn dashboard before collecting evidence')
    entries = sorted(directory.rglob('*'))
    if len(entries) > 1000 or any(path.is_symlink() for path in entries):
        raise ValueError('dashboard bundle contains links or too many entries')
    files = [path for path in entries if path.is_file()]
    if sum(path.stat().st_size for path in files) > 100 * 1024 * 1024:
        raise ValueError('dashboard bundle exceeds evidence input budget')
    digest = hashlib.sha256()
    for path in files:
        record = json.dumps([path.relative_to(directory).as_posix(), sha256_file(path)],
                            ensure_ascii=True, separators=(',', ':')).encode()
        digest.update(len(record).to_bytes(8, 'big'))
        digest.update(record)
    return digest.hexdigest(), len(files)


def _source(path):
    path = str(Path(path).resolve(strict=True))
    git = ('git', '-c', 'safe.directory=' + path, '-C', path)
    revision = _command(*git, 'rev-parse', 'HEAD')
    if not REVISION.fullmatch(revision):
        raise ValueError('invalid source revision')
    # Report dirtiness, never filenames or diff contents (which may contain keys).
    dirty = bool(_command(*git, 'status', '--porcelain', '--untracked-files=normal'))
    return {'revision': revision, 'dirty': dirty}


def _memory_bytes():
    if platform.system() == 'Darwin':
        return int(_command('sysctl', '-n', 'hw.memsize'))
    return int(os.sysconf('SC_PHYS_PAGES')) * int(os.sysconf('SC_PAGE_SIZE'))


def collect_provenance(cairn_source, cairn_binary, lti_binary, *, waypoint_source=None,
                       images=None, fixture_version=FIXTURE_VERSION):
    """Collect only allowlisted identifiers; images maps public role to local image ref.

    Sources may be dirty during development; that fact remains explicit. The
    binary digest identifies exact executed bytes, not proof they match source.
    Call after fixture images exist, before disposing any invocation-built image.
    """
    if fixture_version != FIXTURE_VERSION:
        raise ValueError('unknown fixture version')
    waypoint_source = waypoint_source or Path(__file__).resolve().parents[2]
    images = images or {}
    if not isinstance(images, dict) or not set(images) <= IMAGE_ROLES:
        raise ValueError('images must map supported public roles to image references')
    image_ids = {}
    for role, reference in sorted(images.items()):
        if not isinstance(reference, str) or not reference or reference.startswith('-'):
            raise ValueError('invalid image reference')
        identity = _command('docker', 'image', 'inspect', reference, '--format', '{{.Id}}')
        if not identity.startswith('sha256:') or not HASH.fullmatch(identity[7:]):
            raise ValueError('invalid image identity')
        image_ids[role] = identity
    here = Path(__file__).resolve().parent
    fixtures = {name: sha256_file(here / name) for name in sorted(FIXTURE_FILES)}
    dashboard_hash, dashboard_count = dashboard_digest(Path(cairn_source) / 'web' / 'dist')
    provenance = {
        'cairn': _source(cairn_source), 'waypoint': _source(waypoint_source),
        'cairn_binary_sha256': sha256_file(cairn_binary),
        'lti_binary_sha256': sha256_file(lti_binary),
        'axe_core_sha256': sha256_file(Path(cairn_source) / 'web' / 'node_modules' / 'axe-core' / 'axe.min.js'),
        'dashboard_bundle_sha256': dashboard_hash, 'dashboard_file_count': dashboard_count,
        'fixture_version': fixture_version, 'fixture_files_sha256': fixtures,
        'image_ids': image_ids,
        'hardware': {'os': platform.system(), 'architecture': platform.machine(),
                     'cpu_count': os.cpu_count(), 'memory_bytes': _memory_bytes()},
        'runtime': {'python': platform.python_version(),
                    'docker_server': _command('docker', 'version', '--format', '{{.Server.Version}}')},
    }
    _validate_provenance(provenance)
    return provenance


def _exact(mapping, keys):
    if not isinstance(mapping, dict) or set(mapping) != set(keys):
        raise ValueError('evidence schema contains missing or unexpected fields')


def _positive_int(value):
    return type(value) is int and 0 < value < 2**63


def _validate_provenance(p):
    _exact(p, {'cairn', 'waypoint', 'cairn_binary_sha256', 'lti_binary_sha256',
               'fixture_version', 'fixture_files_sha256', 'image_ids', 'hardware', 'runtime',
               'dashboard_bundle_sha256', 'dashboard_file_count', 'axe_core_sha256'})
    for component in ('cairn', 'waypoint'):
        _exact(p[component], {'revision', 'dirty'})
        if not REVISION.fullmatch(str(p[component]['revision'])) or type(p[component]['dirty']) is not bool:
            raise ValueError('invalid source identity')
    for field in ('cairn_binary_sha256', 'lti_binary_sha256', 'dashboard_bundle_sha256', 'axe_core_sha256'):
        if not isinstance(p[field], str) or not HASH.fullmatch(p[field]):
            raise ValueError('invalid binary digest')
    if type(p['dashboard_file_count']) is not int or not 1 <= p['dashboard_file_count'] <= 1000:
        raise ValueError('invalid dashboard file count')
    if p['fixture_version'] != FIXTURE_VERSION:
        raise ValueError('unknown fixture version')
    _exact(p['fixture_files_sha256'], FIXTURE_FILES)
    if any(not isinstance(v, str) or not HASH.fullmatch(v) for v in p['fixture_files_sha256'].values()):
        raise ValueError('invalid fixture digest')
    if not isinstance(p['image_ids'], dict) or not set(p['image_ids']) <= IMAGE_ROLES:
        raise ValueError('invalid image roles')
    for identity in p['image_ids'].values():
        if not isinstance(identity, str) or not identity.startswith('sha256:') or not HASH.fullmatch(identity[7:]):
            raise ValueError('invalid image identity')
    _exact(p['hardware'], {'os', 'architecture', 'cpu_count', 'memory_bytes'})
    if p['hardware']['os'] not in {'Linux', 'Darwin'} or not PUBLIC_VALUE.fullmatch(str(p['hardware']['architecture'])):
        raise ValueError('unsupported hardware evidence')
    if not _positive_int(p['hardware']['cpu_count']) or not _positive_int(p['hardware']['memory_bytes']):
        raise ValueError('invalid hardware capacity')
    _exact(p['runtime'], {'python', 'docker_server'})
    if any(not isinstance(v, str) or not PUBLIC_VALUE.fullmatch(v) for v in p['runtime'].values()):
        raise ValueError('invalid runtime version')


def _timestamp(raw):
    if not isinstance(raw, str) or len(raw) > 40:
        raise ValueError('invalid evidence timestamp')
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError('evidence timestamps must use UTC')
    return value


def write_pass(root, report, scope, cleanup_verified):
    """Publish PASS only after required phases AND independently checked cleanup.

    The caller must verify resource absence, child-process exit and (full Linux)
    filesystem unmount. This function intentionally cannot infer those facts from
    command return codes. Unknown fields and raw application reports are refused.
    """
    if scope not in REQUIRED_PHASES or cleanup_verified is not True:
        raise ValueError('unknown scope or cleanup not verified')
    required = {'provenance', 'phases', 'started_at', 'finished_at', 'duration_seconds'}
    if not isinstance(report, dict) or not required <= set(report) or set(report) - required - {'checks', 'metrics', 'course_manifest_sha256'}:
        raise ValueError('invalid or unfiltered evidence report')
    _validate_provenance(report['provenance'])
    phases = report['phases']
    if not isinstance(phases, dict) or set(phases) != REQUIRED_PHASES[scope] or any(v is not True for v in phases.values()):
        raise ValueError('required rehearsal phase missing or unsuccessful')
    manifest_hash = report.get('course_manifest_sha256')
    if scope == 'full-linux':
        if not isinstance(manifest_hash, str) or not HASH.fullmatch(manifest_hash):
            raise ValueError('full rehearsal requires the consumed course manifest digest')
    elif manifest_hash is not None:
        raise ValueError('local subset does not execute a course manifest')
    needed_images = {'course', 'checkout', 'forgejo'}
    if scope == 'full-linux':
        needed_images |= {'keycloak', 'nginx', 'hub'}
        if report['provenance']['hardware']['os'] != 'Linux':
            raise ValueError('full rehearsal requires Linux evidence')
    if not needed_images <= set(report['provenance']['image_ids']):
        raise ValueError('required image identity missing')
    start, finish = _timestamp(report['started_at']), _timestamp(report['finished_at'])
    duration = report['duration_seconds']
    if finish < start or type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise ValueError('invalid rehearsal timing')
    checks = report.get('checks', [])
    if not isinstance(checks, list) or len(checks) > 200 or any(not isinstance(v, str) or not SLUG.fullmatch(v) for v in checks):
        raise ValueError('checks must be bounded public identifiers')
    metrics = report.get('metrics', {})
    if not isinstance(metrics, dict) or len(metrics) > 200 or any(
            not isinstance(k, str) or not SLUG.fullmatch(k) or type(v) not in (int, float)
            or not 0 <= v <= 10**18 or not math.isfinite(v) for k, v in metrics.items()):
        raise ValueError('metrics must be bounded nonnegative numbers')
    result = {'version': VERSION, 'status': 'PASS', 'scope': scope, 'cleanup_verified': True, **report}
    data = (json.dumps(result, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    if len(data) > MAX_BYTES:
        raise ValueError('evidence exceeds output budget')
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('output root must be an existing private directory')
    target = root / 'PASS.json'
    # Hard-link publication gives atomic visibility and O_EXCL semantics. Unlike
    # replace(), it cannot silently overwrite a previous run or follow a symlink.
    fd, temporary = tempfile.mkstemp(prefix='.evidence-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return target
