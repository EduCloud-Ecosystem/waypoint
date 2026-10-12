#!/usr/bin/env python3
"""Run two synthetic OIDC courses on one disposable native Linux worker."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import requests
from playwright.sync_api import sync_playwright
from notebook import Notebook, SUBJECTS, ROOT
from backup import docker
from course_manifest import render_manifest
from deployment import preflight, compose
from evidence import _source, sha256_file
from homes import inspect, run as xfs_run
from recovery import load_config, restic, restore_repository, RETIRED_TAG
from smoke import change
from verify import port
from verify_institutional_recovery import cleanup_rehearsal
from verify_retirement import refused, verify_work
import retirement
import retirement_export as exports

CHECKS = frozenset({
    'worker-budget-verified', 'two-courses-enrolled', 'distinct-origins-clients-storage',
    'same-subject-course-isolation', 'concurrent-python-r', 'saved-coursework-isolated',
    'cross-course-hub-cookie-denied', 'cross-course-learner-cookie-denied',
    'per-course-capacity-429', 'other-course-unaffected-by-capacity',
    'revoked-course-old-session-denied', 'revocation-retains-coursework',
    'other-course-unaffected-by-revocation', 'retirement-protected-managed-v4',
    'legacy-managed-restore-refused', 'retired-course-start-refused',
    'foreign-home-volume-writer-refused',
    'released-lease-remaining-course-active', 'released-tombstone-collision-refused',
    'retired-source-work-exported', 'other-course-unaffected-by-retirement',
    'fresh-namespace-restored-closed', 'managed-restore-enrollment-required',
    'restored-start-refused', 'restored-retired-enrollment-refused',
    'restored-work-exported', 'other-course-unaffected-by-closed-restoration',
})
HASH = re.compile(r'[0-9a-f]{64}\Z')
AUTHORITY = Path('/var/lib/educloud/worker-allocation')
ALLOCATION_FIELDS = {'active_courses', 'released_courses', 'memory_reserved_mb', 'cpu_reserved',
                     'memory_budget_mb', 'cpu_budget', 'host_reserve_mb', 'host_reserve_cpu'}


def bounded_allocation(value):
    return {key: value[key] for key in ALLOCATION_FIELDS}


def publish_pass(root, report, cleanup_verified):
    """Only publish bounded public fingerprints after every control and cleanup."""
    keys = {'waypoint_revision', 'fixture_sha256', 'allocator_sha256', 'source_manifests_sha256',
            'policy_sha256', 'snapshot_id', 'source_export_sha256', 'restored_export_sha256',
            'images', 'allocation', 'retired_allocation', 'checks'}
    if cleanup_verified is not True or not isinstance(report, dict) or set(report) != keys:
        raise ValueError('multiple-course PASS requires complete bounded evidence and verified cleanup')
    if (not isinstance(report['waypoint_revision'], str)
            or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', report['waypoint_revision'])
            or any(not isinstance(report[key], str) or not HASH.fullmatch(report[key]) for key in
                   ('fixture_sha256', 'allocator_sha256', 'policy_sha256', 'snapshot_id',
                    'source_export_sha256', 'restored_export_sha256'))
            or not isinstance(report['source_manifests_sha256'], list)
            or len(report['source_manifests_sha256']) != 2
            or len(set(report['source_manifests_sha256'])) != 2
            or any(not isinstance(value, str) or not HASH.fullmatch(value)
                   for value in report['source_manifests_sha256'])
            or not isinstance(report['images'], dict) or set(report['images']) != {'hub', 'course'}
            or any(not isinstance(value, str) or not exports.IMAGE.fullmatch(value) for value in report['images'].values())
            or not isinstance(report['checks'], list) or set(report['checks']) != CHECKS
            or len(report['checks']) != len(CHECKS)):
        raise ValueError('multiple-course evidence schema or completed controls invalid')
    for key in ('allocation', 'retired_allocation'):
        value = report[key]
        if (not isinstance(value, dict) or set(value) != ALLOCATION_FIELDS
                or any(type(number) not in (int, float) or not 0 <= number <= 1048576 for number in value.values())
                or any(type(value[field]) is not int for field in
                       ('active_courses', 'released_courses', 'memory_reserved_mb', 'memory_budget_mb', 'host_reserve_mb'))
                or value['memory_reserved_mb'] > value['memory_budget_mb']
                or value['cpu_reserved'] > value['cpu_budget']):
            raise ValueError('multiple-course resource evidence invalid')
    active, retired = report['allocation'], report['retired_allocation']
    if (active['active_courses'] != 2 or active['released_courses'] != 0
            or retired['active_courses'] != 1 or retired['released_courses'] != 1
            or active['memory_reserved_mb'] != 2 * retired['memory_reserved_mb']
            or active['cpu_reserved'] != 2 * retired['cpu_reserved']
            or any(active[field] != retired[field] for field in
                   ('memory_budget_mb', 'cpu_budget', 'host_reserve_mb', 'host_reserve_cpu'))):
        raise ValueError('multiple-course lease transition evidence invalid')
    data = (json.dumps(dict(report, version=1, status='PASS',
                           scope='same-worker-synthetic-multiple-courses',
                           closed_recovery_only=True, cleanup_verified=True),
                       sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
    if len(data) > 64 * 1024:
        raise ValueError('multiple-course evidence budget exceeded')
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('multiple-course evidence directory must be private')
    fd, temporary = tempfile.mkstemp(prefix='.multiple-course-evidence-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data); output.flush(); os.fsync(output.fileno())
        os.link(temporary, root / 'MULTI-COURSE-PASS.json')
    finally:
        os.unlink(temporary)


def cleanup_worker(policy_path, directories):
    """Dispose only this synthetic invocation's authority after resources stop.

    This is deliberately a rehearsal helper, not a production deletion API.
    Keep the global flock inode: replacing a lock file would split its authority.
    """
    import allocation
    policy_path = Path(policy_path)
    state = policy_path.parent / 'worker-state'
    if not AUTHORITY.exists() and not AUTHORITY.is_symlink() and not state.exists() and not state.is_symlink():
        return True  # Enrollment never reached durable worker state.
    with allocation.worker_lock():
        policy, policy_hash = allocation.policy(policy_path)
        _, daemon_hash = allocation.daemon()
        if (policy['state_directory'] != str(state) or policy['owner'] != 'Synthetic rehearsal operator'
                or not re.fullmatch(r'synthetic-[0-9a-f]{12}', policy['worker_id'])
                or allocation.AUTHORITY != AUTHORITY):
            raise RuntimeError('synthetic worker cleanup binding invalid')
        expected = allocation.binding(policy_path, policy, policy_hash, daemon_hash)
        if AUTHORITY.exists() or AUTHORITY.is_symlink():
            value = allocation.ledger(expected)
            if set(path.name for path in AUTHORITY.iterdir()) != {'authority.json'}:
                raise RuntimeError('unexpected authority file prevents synthetic disposal')
        else:
            # The production allocator writes an empty ledger before publishing
            # authority. Only that exact interrupted initialization may be removed.
            value = allocation.read(state / 'ledger.json')
            if value != dict(expected, claims={}):
                raise RuntimeError('unbound synthetic worker state is not empty')
        allocation.private_directory(state)
        if set(path.name for path in state.iterdir()) != {'ledger.json'}:
            raise RuntimeError('unexpected worker-state file prevents synthetic disposal')
        allowed = {str(Path(directory)) for directory in directories}
        for entry in value['claims'].values():
            if entry['claim']['directory'] not in allowed:
                raise RuntimeError('foreign course claim prevents synthetic disposal')
            allocation.stopped(entry['claim'], value['claims'])
        (state / 'ledger.json').unlink(); state.rmdir(); allocation.sync(state.parent)
        if AUTHORITY.exists():
            (AUTHORITY / 'authority.json').unlink(); AUTHORITY.rmdir(); allocation.sync(AUTHORITY.parent)
        if state.exists() or state.is_symlink() or AUTHORITY.exists() or AUTHORITY.is_symlink():
            raise RuntimeError('synthetic worker authority cleanup unverified')
    return True


def cleanup_courses(first, second, first_root, source_instance, recovered, policy_path):
    """Always clean both independently owned courses, even after partial start."""
    failures = []
    try:
        if cleanup_rehearsal(first, first_root, source_instance, recovered) is not True:
            failures.append('first-course-cleanup-unverified')
    except Exception:
        failures.append('first-course-cleanup-failed')
    try:
        if second.close() is not True or second.cleanup_verified is not True:
            failures.append('second-course-cleanup-unverified')
    except Exception:
        failures.append('second-course-cleanup-failed')
    if not failures:
        try:
            if cleanup_worker(policy_path, (first.deployment, second.deployment)) is not True:
                failures.append('worker-authority-cleanup-unverified')
        except Exception:
            failures.append('worker-authority-cleanup-failed')
    if failures:
        raise RuntimeError('multiple-course cleanup unverified: ' + ', '.join(failures))
    return True


def active_learners(fixture):
    return set(docker('ps', '-q', '--no-trunc', '--filter',
                      'label=educloud.workspace.instance=' + fixture.instance,
                      '--filter', 'label=educloud.workspace.kind=learner').split())


def foreign_writer_control(fixture, policy_path, retained_home):
    """Exercise a real writable alias without executing or writing learner work."""
    import allocation
    instance = fixture.instance
    if not re.fullmatch(r'rehearsal-[0-9a-f]{10}', instance):
        raise ValueError('foreign writer control requires invocation-owned fixture')
    retained_home = Path(retained_home)
    if (retained_home.is_symlink() or not retained_home.is_dir()
            or retained_home.parent != fixture.homes
            or not re.fullmatch(re.escape(instance) + r'-home-[0-9a-f]{24}', retained_home.name)):
        raise ValueError('foreign writer control requires exact retained home')
    alias = instance + '-foreign-home-alias'; helper = instance + '-foreign-home-writer'
    def containers():
        return docker('ps', '-aq', '--no-trunc', '--filter', 'name=^/' + helper + '$').split()
    def volumes():
        return docker('volume', 'ls', '-q', '--filter', 'name=^' + alias + '$').split()
    if containers() or volumes():
        raise ValueError('foreign writer fixture names already exist')
    try:
        created = docker('volume', 'create', '--driver', 'local', '--label', 'educloud.rehearsal=' + instance,
                         '--opt', 'type=none', '--opt', 'o=bind', '--opt', 'device=' + str(retained_home), alias)
        assert created == alias
        identity = docker('run', '-d', '--name', helper, '--label', 'educloud.rehearsal=' + instance,
                          '--network', 'none', '--read-only', '--user', '1000:1000', '--cap-drop', 'ALL',
                          '--security-opt', 'no-new-privileges:true', '--memory', '64m', '--memory-swap', '64m',
                          '--cpus', '0.1', '--pids-limit', '16', '--mount', 'type=volume,src=' + alias + ',dst=/home-alias',
                          '--entrypoint', 'python3', fixture.image, '-I', '-c', 'import time; time.sleep(120)')
        assert re.fullmatch(r'[0-9a-f]{64}', identity)
        running = json.loads(docker('inspect', identity))[0]
        assert running['State']['Running'] is True and running['Image'] == fixture.image
        assert running['Config']['Labels'].get('educloud.rehearsal') == instance
        assert not running['Config']['Labels'].get('educloud.workspace.instance')
        assert any(mount.get('Name') == alias and mount.get('RW') is True for mount in running['Mounts'])
        try:
            allocation.release(fixture.deployment)
        except allocation.AllocationError as exc:
            assert exc.code == 'stop_all_home_volume_writers', 'release refused for an unrelated reason'
        else:
            raise AssertionError('running foreign home-volume writer was accepted')
        inventory = allocation.inventory(policy_path)
        assert inventory['active_courses'] == 2 and inventory['released_courses'] == 0
    finally:
        failures = []
        # Inspect exact names and ownership before removing either resource.
        # Attempt both even if one cleanup operation fails; never broaden filters.
        for kind in ('container', 'volume'):
            try:
                rows = containers() if kind == 'container' else volumes()
                if len(rows) > 1:
                    raise RuntimeError('ambiguous foreign writer cleanup inventory')
                if rows:
                    record = json.loads(docker(*(('inspect', rows[0]) if kind == 'container'
                                                else ('volume', 'inspect', rows[0]))))[0]
                    if kind == 'container':
                        if record['Name'] != '/' + helper or record['Config']['Labels'].get('educloud.rehearsal') != instance:
                            raise RuntimeError('foreign writer helper ownership changed')
                        docker('rm', '--force', rows[0])
                    else:
                        if record['Name'] != alias or record['Labels'] != {'educloud.rehearsal':instance}:
                            raise RuntimeError('foreign writer volume ownership changed')
                        docker('volume', 'rm', alias)
                if (containers() if kind == 'container' else volumes()):
                    raise RuntimeError('foreign writer resource remains')
            except Exception:
                failures.append(kind + '-cleanup-failed')
        if failures:
            raise RuntimeError('foreign writer cleanup unverified: ' + ', '.join(failures))


def wait_hub(fixture):
    for _ in range(90):
        try:
            if fixture.session.get(fixture.origin + '/hub/login', timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise AssertionError('managed course did not become ready')


def assert_live(fixture, session, url, content, container_ids):
    response = session.get(url + '/api/contents/course.py', timeout=20)
    assert response.status_code == 200 and response.json()['content'] == content
    assert active_learners(fixture) == container_ids, 'unrelated course learner was replaced or stopped'
    fixture.kernel(session, url, 'python3',
                   'from pathlib import Path\nassert Path("course.py").read_text() == ' + repr(content) + '\nprint("COURSE_OK")')


def transplanted_session(source, fixture, url):
    """Attempt deliberate cross-course credential replay, rather than empty cookies."""
    session = requests.Session()
    session.trust_env = False; session.verify = str(fixture.root / 'ca.pem')
    target = urlsplit(url).hostname
    for cookie in source.cookies:
        session.cookies.set(cookie.name, cookie.value, domain=target, path=cookie.path)
    assert len(session.cookies) > 0, 'cross-course replay control has no source cookies'
    return session


def save_coursework(fixture, session, url, label):
    expected = {'course.py': label + ' Python coursework\n', 'course-r.txt': label + ' R coursework\n'}
    fixture.kernel(session, url, 'python3',
                   'from pathlib import Path\nPath("course.py").write_text(' + repr(expected['course.py']) + ')\nprint("COURSE_OK")')
    fixture.kernel(session, url, 'ir',
                   'writeLines(' + json.dumps(expected['course-r.txt'].rstrip('\n')) + ', "course-r.txt"); cat("COURSE_OK\\n")')
    return expected


def verify_managed_bundle(cfg, snapshot, root):
    """The previously shipped validator must refuse managed-worker v4 packages."""
    baseline = '3c1c20f844bd3e504931207c7f344f806daed9da'
    source = subprocess.check_output(['git', '-c', 'safe.directory=' + str(ROOT.parent),
                                     '-C', str(ROOT.parent), 'show', baseline + ':course-workspace/recovery.py'], text=True)
    namespace = {'__file__': str(ROOT / 'recovery.py'), '__name__': 'legacy_managed_reference'}
    exec(compile(source, 'trusted-legacy-managed-reference', 'exec'), namespace)
    with tempfile.TemporaryDirectory(prefix='.managed-bundle-control-', dir=root) as scratch:
        restic(cfg, 'restore', snapshot, '--target', scratch, '--verify')
        bundle = Path(scratch) / 'bundle'
        metadata = json.loads((bundle / 'bundle.json').read_text())
        assert metadata['version'] == 4 and metadata['retirement_protected'] is True
        assert (bundle / 'runtime/allocation.py').is_file()
        assert (bundle / 'worker-allocation.json').is_file()
        try:
            namespace['validate_bundle'](bundle)
        except ValueError as exc:
            assert 'unsupported recovery bundle' in str(exc)
        else:
            raise AssertionError('legacy validator accepted managed worker recovery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if not __debug__:
        parser.error('optimized Python cannot enforce multiple-course acceptance assertions')
    parser.add_argument('--output', type=Path, required=True); args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('use root on a disposable native Linux runner')
    if AUTHORITY.exists() or AUTHORITY.is_symlink():
        parser.error('multiple-course rehearsal requires a fresh disposable worker authority')
    import allocation
    source_state = _source(ROOT.parent)
    assert source_state['dirty'] is False, 'multiple-course acceptance needs clean source'
    fixture_hash = sha256_file(__file__); allocator_hash = sha256_file(ROOT / 'allocation.py')
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False); root = args.output.resolve()
    policy = {'version': 1, 'worker_id': 'synthetic-' + uuid.uuid4().hex[:12],
              'memory_budget_mb': 8192, 'cpu_budget': 3, 'host_reserve_mb': 4096,
              'host_reserve_cpu': 1, 'state_directory': str(root / 'worker-state'),
              'owner': 'Synthetic rehearsal operator'}
    policy_path = root / 'worker-policy.json'
    policy_path.write_text(json.dumps(policy)); policy_path.chmod(0o600)
    roots = [root / 'course-one', root / 'course-two']
    for course_root in roots:
        course_root.mkdir(mode=0o700); (course_root / 'source').mkdir(mode=0o700)
    capacity = {'memory_mb': 512, 'cpu': 0.25, 'active_limit': 1}
    # Both capture the same original resolver before either fixture installs it.
    first = Notebook(roots[0] / 'source', domain='one.rehearsal.test', auth_domain='authone.rehearsal.test',
                     client_id='course-one', capacity=capacity, allocation_policy=policy_path)
    second = Notebook(roots[1] / 'source', domain='two.rehearsal.test', auth_domain='authtwo.rehearsal.test',
                      client_id='course-two', capacity=capacity, allocation_policy=policy_path)
    recovered = None; source_instance = first.instance; checks = []
    try:
        first.start(); second.start()
        inventory = allocation.inventory(policy_path)
        assert inventory['active_courses'] == 2 and inventory['released_courses'] == 0
        assert inventory['memory_reserved_mb'] == 3072 and inventory['cpu_reserved'] == 2.5
        assert all((fixture.deployment / '.worker-allocation.json').is_file() for fixture in (first, second))
        checks += ['worker-budget-verified', 'two-courses-enrolled']
        assert first.origin != second.origin and first.auth != second.auth and first.client_id != second.client_id
        assert first.homes != second.homes and first.instance != second.instance
        assert allocation.home_filesystem_uuid(first.homes) != allocation.home_filesystem_uuid(second.homes)
        checks.append('distinct-origins-clients-storage')
        assert first.image == second.image
        assert first.hub_image == second.hub_image
        images = {'hub': first.hub_image, 'course': first.image}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                contexts = [browser.new_context(ignore_https_errors=True) for _ in range(2)]
                pages = [context.new_page() for context in contexts]
                sessions = []
                for fixture, page in zip((first, second), pages):
                    fixture.login(page, 'alice'); sessions.append(fixture.ready(page, 'alice'))
                (one, one_url), (two, two_url) = sessions
                assert SUBJECTS['alice'] in one_url and SUBJECTS['alice'] in two_url and one_url != two_url
                suffix = 'home-' + hashlib.sha256(SUBJECTS['alice'].encode()).hexdigest()[:24]
                assert docker('volume', 'inspect', first.instance + '-' + suffix)
                assert docker('volume', 'inspect', second.instance + '-' + suffix)
                checks.append('same-subject-course-isolation')
                expected = {}; second_expected = None
                with ThreadPoolExecutor(max_workers=2) as pool:
                    first_future = pool.submit(save_coursework, first, one, one_url, 'course one alice')
                    second_future = pool.submit(save_coursework, second, two, two_url, 'course two alice')
                    expected['alice'] = first_future.result(); second_expected = second_future.result()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    mixed = [pool.submit(first.kernel, one, one_url, 'python3', 'print("COURSE_OK")'),
                             pool.submit(second.kernel, two, two_url, 'ir', 'cat("COURSE_OK\\n")')]
                    for future in mixed: future.result()
                checks.append('concurrent-python-r')
                assert expected['alice']['course.py'] != second_expected['course.py']
                second_containers = active_learners(second); assert len(second_containers) == 1
                assert_live(first, one, one_url, expected['alice']['course.py'], active_learners(first))
                assert_live(second, two, two_url, second_expected['course.py'], second_containers)
                checks.append('saved-coursework-isolated')
                with transplanted_session(one, second, second.origin) as replay:
                    response = replay.get(second.origin + '/hub/api/users/' + SUBJECTS['alice'], timeout=15, allow_redirects=False)
                    assert response.status_code in (401, 403), 'another course accepted transplanted Hub cookies'
                checks.append('cross-course-hub-cookie-denied')
                with transplanted_session(one, second, two_url) as replay:
                    response = replay.get(two_url + '/api/contents/course.py', timeout=15, allow_redirects=False)
                    assert response.status_code in (302, 303, 401, 403, 404), 'another course accepted transplanted learner cookies'
                checks.append('cross-course-learner-cookie-denied')
                bob_context = browser.new_context(ignore_https_errors=True); bob_page = bob_context.new_page()
                first.login(bob_page, 'bob'); bob = first.http_session(bob_context)
                response = change(bob, 'POST', first.origin + '/hub/api/users/' + SUBJECTS['bob'] + '/server')
                assert response.status_code == 429 and len(active_learners(first)) == 1
                checks.append('per-course-capacity-429')
                assert_live(second, two, two_url, second_expected['course.py'], second_containers)
                checks.append('other-course-unaffected-by-capacity')
                first.compose('stop', 'hub')
                first.env['WORKSPACE_ALLOWED_SUBJECTS'] = SUBJECTS['bob']; first.write_env()
                first.compose('up', '-d', '--force-recreate', '--no-build', '--pull', 'never', 'hub'); wait_hub(first)
                old = one.get(one_url + '/api/contents/course.py', timeout=15, allow_redirects=False)
                assert old.status_code in (302, 303, 401, 403, 404, 503)
                pages[0].goto(first.origin + '/hub/spawn/' + SUBJECTS['alice'])
                for _ in range(30):
                    message = ' '.join(pages[0].locator('body').inner_text().lower().split())
                    if any(reason in message for reason in ('403', 'not allowed', 'not in this course roster')):
                        break
                    time.sleep(1)
                else:
                    raise AssertionError('revoked learner did not reach roster respawn denial')
                assert not active_learners(first)
                checks.append('revoked-course-old-session-denied')
                assert (first.homes / (first.instance + '-' + suffix) / 'work/course.py').read_text() == expected['alice']['course.py']
                checks.append('revocation-retains-coursework')
                assert_live(second, two, two_url, second_expected['course.py'], second_containers)
                checks.append('other-course-unaffected-by-revocation')
                # A fresh login uses the current roster and creates Bob's retained work.
                bob_context.close(); bob_context = browser.new_context(ignore_https_errors=True)
                bob_page = bob_context.new_page(); first.login(bob_page, 'bob')
                bob, bob_url = first.ready(bob_page, 'bob')
                expected['bob'] = save_coursework(first, bob, bob_url, 'course one bob')
                first.compose('stop', 'hub'); inspect(first.homes, first.instance)
                recovery_path = first.deployment / 'recovery.json'; cfg = load_config(recovery_path)
                assert cfg['local_test'] is True and Path(cfg['repository']).is_relative_to(root)
                restic(cfg, 'init')
                review_after = (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat()
                plan_path = roots[0] / 'retirement-plan.json'
                plan = retirement.preview(first.deployment, recovery_path, plan_path,
                                          'Synthetic retention operator', review_after)
                snapshots = json.loads(restic(cfg, 'snapshots', '--json', plan['snapshot_id']))
                assert RETIRED_TAG in snapshots[0]['tags']
                verify_managed_bundle(cfg, plan['snapshot_id'], root)
                checks += ['retirement-protected-managed-v4', 'legacy-managed-restore-refused']
                retirement.apply(first.deployment, plan_path, recovery_path)
                refused(lambda: preflight(first.deployment))
                refused(lambda: compose(first.deployment, 'up', '-d', '--no-build', '--pull', 'never', 'hub'))
                checks.append('retired-course-start-refused')
                foreign_writer_control(first, policy_path, first.homes / (first.instance + '-' + suffix))
                checks.append('foreign-home-volume-writer-refused')
                allocation.release(first.deployment)
                retired_inventory = allocation.inventory(policy_path)
                assert retired_inventory['active_courses'] == 1 and retired_inventory['released_courses'] == 1
                checks.append('released-lease-remaining-course-active')
                duplicate = copy.deepcopy(first.manifest); duplicate_dir = roots[0] / 'collision-deployment'
                duplicate['backup']['staging_dir'] = str(duplicate_dir)
                render_manifest(duplicate, duplicate_dir)
                refused(lambda: allocation.enroll(duplicate_dir, policy_path), allocation.AllocationError)
                checks.append('released-tombstone-collision-refused')
                source_export = roots[0] / 'coursework'
                exports.export_course(first.deployment, source_export); exported = verify_work(source_export, expected)
                checks.append('retired-source-work-exported')
                assert_live(second, two, two_url, second_expected['course.py'], second_containers)
                checks.append('other-course-unaffected-by-retirement')
                disk = roots[0] / 'replacement.img'
                with disk.open('xb') as output: output.truncate(2 * 1024**3)
                homes = roots[0] / 'replacement-homes'; homes.mkdir()
                xfs_run('mkfs.xfs', '-f', str(disk)); xfs_run('mount', '-o', 'loop,prjquota', str(disk), str(homes))
                destination = roots[0] / 'restored'
                recovered = restore_repository(cfg, plan['snapshot_id'], destination, port(), homes, first.env_file)
                assert recovered['WORKSPACE_INSTANCE'] != source_instance
                assert retirement.read_retirement(destination)['status'] == 'RETIRED'
                assert not docker('ps', '-q', '--filter', 'label=educloud.workspace.instance=' + recovered['WORKSPACE_INSTANCE'])
                checks.append('fresh-namespace-restored-closed')
                assert (destination / '.worker-allocation-required.json').is_file()
                assert not (destination / '.worker-allocation.json').exists()
                checks.append('managed-restore-enrollment-required')
                refused(lambda: preflight(destination))
                result = subprocess.run([sys.executable, str(destination / 'runtime/deployment.py'),
                                         'start', '--directory', str(destination)], capture_output=True, text=True)
                assert result.returncode != 0 and ('retired' in result.stderr.lower() or 'enrollment' in result.stderr.lower())
                checks.append('restored-start-refused')
                refused(lambda: allocation.enroll(destination, policy_path), allocation.AllocationError)
                checks.append('restored-retired-enrollment-refused')
                restored_export = roots[0] / 'restored-coursework'
                exports.export_course(destination, restored_export); restored = verify_work(restored_export, expected)
                checks.append('restored-work-exported')
                assert_live(second, two, two_url, second_expected['course.py'], second_containers)
                checks.append('other-course-unaffected-by-closed-restoration')
                for fixture in (first, second): fixture.verify_manifest_frozen()
                assert _source(ROOT.parent) == source_state and sha256_file(__file__) == fixture_hash
                assert sha256_file(ROOT / 'allocation.py') == allocator_hash
                report = {'waypoint_revision': source_state['revision'], 'fixture_sha256': fixture_hash,
                          'allocator_sha256': allocator_hash,
                          'source_manifests_sha256': [first.manifest_digest, second.manifest_digest],
                          'policy_sha256': inventory['policy_sha256'], 'snapshot_id': plan['snapshot_id'],
                          'source_export_sha256': exported['manifest_sha256'],
                          'restored_export_sha256': restored['manifest_sha256'], 'images': images,
                          'allocation': bounded_allocation(inventory),
                          'retired_allocation': bounded_allocation(retired_inventory), 'checks': sorted(checks)}
            finally:
                browser.close()
    finally:
        cleanup_verified = cleanup_courses(first, second, roots[0], source_instance, recovered, policy_path)
    publish_pass(root, report, cleanup_verified)
    print('PASS: two synthetic courses stayed isolated under capacity, revocation, retirement and closed restoration; cleanup verified.', flush=True)


if __name__ == '__main__':
    main()
