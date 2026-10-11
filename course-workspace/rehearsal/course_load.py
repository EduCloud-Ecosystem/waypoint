"""Bounded synthetic Cairn HTTP baseline; this does not measure notebook capacity."""
from concurrent.futures import ThreadPoolExecutor
import math
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlparse

import requests

LEARNERS = 15
ROUNDS = 5
REQUEST_TIMEOUT = 10


def summarize(samples):
    """Nearest-rank percentiles, including failed requests in elapsed time."""
    ordered = sorted(samples)
    if not ordered:
        raise ValueError('latency samples must not be empty')
    return {key: round(ordered[max(0, math.ceil(len(ordered) * percentile) - 1)], 3)
            for key, percentile in [('p50_ms', .5), ('p95_ms', .95), ('max_ms', 1)]}


def local_origin(url):
    parsed = urlparse(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password:
        raise ValueError('load rehearsal requires disposable HTTP loopback fixtures')
    return url.rstrip('/')


def session_for(cookies):
    session = requests.Session()
    session.trust_env = False
    for cookie in cookies:
        session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'],
                            path=cookie['path'], secure=cookie.get('secure', False))
    return session


def check_work(body, submission_id, detail=False):
    if not isinstance(body, dict):
        raise AssertionError('student response is not a JSON object')
    items = [body] if detail else body.get('work')
    if not isinstance(items, list) or len(items) != 1:
        raise AssertionError('student list did not contain exactly one owned submission')
    item = items[0]
    if not isinstance(item, dict):
        raise AssertionError('student work item is not a JSON object')
    if item.get('submission_id') != submission_id or item.get('status') != 'active':
        raise AssertionError('student work identity or active state differs')
    if item.get('latest_grade') is not None or item.get('history') or item.get('assessments'):
        raise AssertionError('ungraded learner received a grade or assessment')


def probe_learner(url, learner, other_id, barrier, rounds=ROUNDS):
    # Each thread owns its requests session. No Playwright object crosses threads.
    checks = [('list', '/me/work', 200), ('own', '/me/work/' + learner['submission_id'], 200),
              ('other', '/me/work/' + other_id, 404), ('operator', '/classrooms', 401)]
    results = []
    with session_for(learner['cookies']) as session:
        barrier.wait(timeout=30)
        for _ in range(rounds):
            for name, path, expected in checks:
                started = time.monotonic()
                status, error = None, None
                try:
                    response = session.get(url + path, timeout=REQUEST_TIMEOUT, allow_redirects=False)
                    status = response.status_code
                    if status != expected:
                        raise AssertionError('unexpected HTTP status')
                    if name in ('list', 'own'):
                        check_work(response.json(), learner['submission_id'], name == 'own')
                except (requests.RequestException, ValueError, AssertionError) as exc:
                    # Never include response bodies, cookies, or session credentials.
                    error = type(exc).__name__
                results.append({'route': name, 'status': status, 'error': error,
                                'elapsed_ms': (time.monotonic() - started) * 1000})
    return results


def phase(url, learners, rounds=ROUNDS):
    local_origin(url)
    count = len(learners)
    if count not in (5, 15) or not 1 <= rounds <= ROUNDS:
        raise ValueError('only bounded five or fifteen learner phases are supported')
    if len({entry['submission_id'] for entry in learners}) != count:
        raise ValueError('load requires distinct learner submissions')
    barrier = threading.Barrier(count)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(probe_learner, url, learner,
                               learners[(index + 1) % count]['submission_id'], barrier, rounds)
                   for index, learner in enumerate(learners)]
        samples = [sample for future in futures for sample in future.result()]
    errors = [sample for sample in samples if sample['error']]
    return {'concurrent_learners': count, 'rounds_per_learner': rounds,
            'requests': len(samples), 'errors': len(errors),
            'elapsed_seconds': round(time.monotonic() - started, 3),
            **summarize([sample['elapsed_ms'] for sample in samples]),
            'routes': {route: {'requests': sum(s['route'] == route for s in samples),
                              'errors': sum(s['route'] == route and bool(s['error']) for s in samples),
                              **summarize([s['elapsed_ms'] for s in samples if s['route'] == route])}
                       for route in ('list', 'own', 'other', 'operator')},
            'error_examples': errors[:10]}


def snapshot(course):
    """Read-only queue snapshot and point-in-time RSS, not a resource peak."""
    database = Path(course.root) / 'cairn.db'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        queue = {table: dict(conn.execute(f'SELECT status, count(*) FROM {table} GROUP BY status'))
                 for table in ('provisioning_jobs', 'grading_runs')}
    result = subprocess.run(['ps', '-o', 'rss=', '-p', str(course.process.pid)],
                            text=True, capture_output=True, timeout=5)
    rss = int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip().isdigit() else None
    return {'cairn_rss_kib_point_sample': rss, 'sqlite_bytes': database.stat().st_size,
            'queue_status_counts': queue}


def verify_bindings(course, cid, learners):
    database = Path(course.root) / 'cairn.db'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        for learner in learners:
            row = conn.execute('SELECT host_user_id FROM roster_entries WHERE classroom_id=? '
                               'AND host=? AND host_username=?',
                               (cid, 'forgejo', learner['username'])).fetchone()
            if row is None or row[0] != learner['provider_user_id']:
                raise AssertionError('roster immutable identity differs from provider identity')


def run_load(course, teacher, browser, cid, aid):
    """Create 15 disposable learners, probe five then fifteen, and fail closed."""
    local_origin(course.url)
    local_origin(course.forge)
    contexts, learners, measurements = [], [], []
    deadline = time.monotonic() + 300
    try:
        for index in range(1, LEARNERS + 1):
            if time.monotonic() >= deadline:
                raise AssertionError('load learner setup exceeded five minutes')
            username = f'load{index:02d}'
            account = course.api('POST', '/admin/users', {
                'username': username, 'email': username + '@synthetic.invalid',
                'password': course.password, 'must_change_password': False,
                'send_notify': False})
            course.request(teacher, 'POST', f'/classrooms/{cid}/roster', {'username': username}, 201)
            context, _ = course.login(browser, username, f'/assignments/{aid}/accept')
            contexts.append(context)
            while time.monotonic() < deadline:
                work = course.request(context, 'GET', '/me/work')['work']
                if len(work) == 1 and work[0]['status'] == 'active':
                    break
                if any(item['status'] == 'failed' for item in work):
                    raise AssertionError('load learner repository provisioning failed')
                time.sleep(.25)
            else:
                raise AssertionError('load learner provisioning exceeded setup deadline')
            learners.append({'username': username, 'provider_user_id': str(account['id']),
                             'submission_id': work[0]['submission_id'],
                             'cookies': context.cookies([course.url])})
        if len({entry['provider_user_id'] for entry in learners}) != LEARNERS:
            raise AssertionError('load did not create distinct provider identities')
        verify_bindings(course, cid, learners)
        for count in (5, 15):
            before = snapshot(course)
            result = phase(course.url, learners[:count])
            result['resources_before'] = before
            result['resources_after'] = snapshot(course)
            measurements.append(result)
            if result['errors']:
                # Preserve safe failure measurements for diagnosis while failing the run.
                import json
                (course.root / 'load-FAIL.json').write_text(json.dumps(measurements, indent=2) + '\n')
                raise AssertionError(f'{count}-learner HTTP baseline failed; inspect load-FAIL.json')
        return {'kind': 'local-sqlite-authenticated-http-read-baseline',
                'notebook_capacity_tested': False, 'grading_concurrency_tested': False,
                'latency_gate': 'measurement only; no established historical regression threshold',
                'learners': [{k: v for k, v in learner.items() if k != 'cookies'} for learner in learners],
                'phases': measurements, 'immutable_roster_bindings_verified': LEARNERS,
                'checks': ['distinct-provider-identities', 'owned-submission-only',
                           'cross-learner-404', 'operator-401', 'no-unearned-grades']}
    finally:
        for context in contexts:
            context.close()


def verify_restart(course, browser, report):
    """Reauthenticate after caller restarts Cairn and verify every persisted claim."""
    local_origin(course.url)
    for learner in report['learners']:
        context, _ = course.login(browser, learner['username'], '/student/login?host=forgejo')
        try:
            check_work(course.request(context, 'GET', '/me/work'), learner['submission_id'])
        finally:
            context.close()
    return {'persisted_active_submissions': len(report['learners']), 'reauthenticated_after_restart': True}
