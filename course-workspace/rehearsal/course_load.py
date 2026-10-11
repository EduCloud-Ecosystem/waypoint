"""Bounded synthetic Cairn HTTP baseline; this does not measure notebook capacity."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import math
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse

import requests

LEARNERS = 15
ROUNDS = 5
REQUEST_TIMEOUT = 10
MEMORY_INTERVAL_SECONDS = .05
MEMORY_MAX_SAMPLES = 6000


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


def read_memory_sample(pid, proc_root=Path('/proc')):
    """Only this Cairn process: Linux kernel fields or portable ps RSS, in KiB."""
    if type(pid) is not int or pid <= 0:
        raise ValueError('invalid Cairn process identifier')
    if sys.platform.startswith('linux'):
        with (proc_root / str(pid) / 'status').open() as status:
            raw = status.read(65537)
        if len(raw) > 65536:
            raise ValueError('process status exceeds input bound')
        fields = {}
        for line in raw.splitlines():
            key, separator, value = line.partition(':')
            if separator and key in ('VmRSS', 'VmHWM'):
                amount, unit = value.split()
                if unit != 'kB' or not amount.isdigit():
                    raise ValueError('invalid kernel memory observation')
                fields[key] = int(amount)
        if 'VmRSS' not in fields:
            raise ValueError('process RSS is unavailable')
        return {'rss_kib': fields['VmRSS'], 'lifetime_hwm_kib': fields.get('VmHWM'),
                'source': 'linux-proc-status'}
    result = subprocess.run(['ps', '-o', 'rss=', '-p', str(pid)],
                            text=True, capture_output=True, timeout=2)
    if result.returncode or not result.stdout.strip().isdigit():
        raise ValueError('process RSS is unavailable')
    return {'rss_kib': int(result.stdout.strip()), 'lifetime_hwm_kib': None,
            'source': 'ps-rss'}


class MemorySampler:
    """Bounded scalar aggregation; sampled maxima may miss between-sample spikes."""
    def __init__(self, pid, *, reader=read_memory_sample, clock=time.monotonic,
                 interval=MEMORY_INTERVAL_SECONDS, max_samples=MEMORY_MAX_SAMPLES):
        if not .01 <= interval <= 1 or type(max_samples) is not int or not 2 <= max_samples <= MEMORY_MAX_SAMPLES:
            raise ValueError('invalid memory sampling budget')
        self.pid, self.reader, self.clock = pid, reader, clock
        self.interval, self.limit = interval, max_samples
        self.stop_event = threading.Event()
        self.thread = None
        self.attempts = self.successes = self.failures = 0
        self.observed_max = self.lifetime_hwm = None
        self.max_gap = 0.0
        self.previous = self.first = self.last = None
        self.sources = set()
        self.budget_exhausted = False

    def take(self):
        if self.attempts >= self.limit:
            self.budget_exhausted = True
            return
        self.attempts += 1
        now = self.clock()
        if self.previous is not None:
            self.max_gap = max(self.max_gap, now - self.previous)
        self.previous = now
        try:
            value = self.reader(self.pid)
            rss = value['rss_kib']
            hwm = value['lifetime_hwm_kib']
            if type(rss) is not int or rss < 0 or (hwm is not None and (type(hwm) is not int or hwm < 0)):
                raise ValueError('invalid process memory sample')
            if value['source'] not in ('linux-proc-status', 'ps-rss'):
                raise ValueError('unknown memory observation source')
            self.successes += 1
            self.sources.add(value['source'])
            self.observed_max = rss if self.observed_max is None else max(self.observed_max, rss)
            if hwm is not None:
                self.lifetime_hwm = hwm if self.lifetime_hwm is None else max(self.lifetime_hwm, hwm)
            if self.first is None:
                self.first = now
            self.last = now
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            self.failures += 1

    def loop(self):
        while not self.stop_event.wait(self.interval):
            if self.attempts >= self.limit - 1:
                self.budget_exhausted = True
                return
            self.take()

    def __enter__(self):
        self.take()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError('memory sampling thread did not finish')
        self.take()

    def result(self):
        return {'scope': 'cairn-process-only-excludes-containers-and-children',
                'sources': sorted(self.sources), 'target_interval_ms': self.interval * 1000,
                'sample_attempts': self.attempts, 'sample_count': self.successes,
                'sample_errors': self.failures, 'sample_budget_exhausted': self.budget_exhausted,
                'max_attempt_gap_ms': round(self.max_gap * 1000, 3),
                'sampled_window_seconds': round(self.last - self.first, 6) if self.first is not None else 0,
                'rss_observed_max_kib': self.observed_max,
                'kernel_process_lifetime_hwm_kib': self.lifetime_hwm,
                'peak_is_exact': False,
                'bounds': 'Observed RSS maximum is a sampled lower bound; Linux VmHWM is a kernel-reported process-lifetime high-water mark, not a phase peak.'}


def timestamp_age(raw, now):
    """Unknown timestamps stay unknown; future schedules are not overdue jobs."""
    if not isinstance(raw, str) or not raw or len(raw) > 64:
        return None
    try:
        value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if value.tzinfo is None:
            return None
        return (now - value).total_seconds()
    except (ValueError, TypeError, OverflowError):
        return None


def queue_observations(conn, now):
    # These fixture queues are small. Explicit row bounds prevent accidental
    # unbounded ingestion, and no IDs, messages, or SQL text enter the report.
    jobs = list(conn.execute("SELECT scheduled_at FROM provisioning_jobs WHERE status='pending' LIMIT 1001"))
    runs = list(conn.execute("SELECT started_at FROM grading_runs WHERE status='running' LIMIT 1001"))
    if len(jobs) > 1000 or len(runs) > 1000:
        raise ValueError('synthetic queue observation exceeds row budget')
    pending = [timestamp_age(row[0], now) for row in jobs]
    running = [timestamp_age(row[0], now) for row in runs]
    due = [age for age in pending if age is not None and age >= 0]
    active = [age for age in running if age is not None and age >= 0]
    return {'pending_jobs': len(jobs), 'due_jobs': len(due),
            'future_scheduled_jobs': sum(age is not None and age < 0 for age in pending),
            'pending_job_timestamps_unavailable': sum(age is None for age in pending),
            'oldest_due_job_lateness_seconds': max(due) if due else None,
            'running_grading_runs': len(runs),
            'running_grade_timestamps_unavailable': sum(age is None or age < 0 for age in running),
            'oldest_running_grade_age_seconds': max(active) if active else None}


def snapshot(course):
    """Read-only consistent queue snapshot; scheduled lateness is not enqueue age."""
    database = Path(course.root) / 'cairn.db'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        conn.execute('BEGIN')
        now = datetime.now(timezone.utc)
        queue = {table: dict(conn.execute(f'SELECT status, count(*) FROM {table} GROUP BY status'))
                 for table in ('provisioning_jobs', 'grading_runs')}
        ages = queue_observations(conn, now)
    try:
        rss = read_memory_sample(course.process.pid)['rss_kib']
    except (OSError, ValueError, subprocess.TimeoutExpired):
        rss = None
    return {'cairn_rss_kib_point_sample': rss, 'sqlite_bytes': database.stat().st_size,
            'queue_status_counts': queue, 'queue_observations': ages}


def public_observability_metrics(phase):
    """Explicit numeric availability flags; unavailable observations are never zeroed."""
    metrics = {'database_query_counts_available': 0}
    memory = phase['memory_observations']
    for key in ('target_interval_ms', 'sample_attempts', 'sample_count', 'sample_errors',
                'max_attempt_gap_ms', 'sampled_window_seconds'):
        metrics['memory_' + key] = memory[key]
    metrics['memory_sample_budget_exhausted'] = int(memory['sample_budget_exhausted'])
    for key in ('rss_observed_max_kib', 'kernel_process_lifetime_hwm_kib'):
        metrics['memory_' + key + '_available'] = int(memory[key] is not None)
        if memory[key] is not None:
            metrics['memory_' + key] = memory[key]
    for point in ('before', 'after'):
        observed = phase['resources_' + point]['queue_observations']
        allowed = ('pending_jobs', 'due_jobs', 'future_scheduled_jobs',
                   'pending_job_timestamps_unavailable', 'oldest_due_job_lateness_seconds',
                   'running_grading_runs', 'running_grade_timestamps_unavailable',
                   'oldest_running_grade_age_seconds')
        if not isinstance(observed, dict) or set(observed) != set(allowed):
            raise ValueError('queue age observations require exact public fields')
        for key in allowed:
            value = observed[key]
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                raise ValueError('queue age observations must be nonnegative numbers or unavailable')
            name = point + '_' + key
            if key.endswith('_seconds'):
                metrics[name + '_available'] = int(value is not None)
            if value is not None:
                metrics[name] = value
    return metrics


def verify_bindings(course, cid, learners):
    database = Path(course.root) / 'cairn.db'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
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
            with MemorySampler(course.process.pid) as memory:
                result = phase(course.url, learners[:count])
            result['memory_observations'] = memory.result()
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
                'database_query_counts': {'available': False, 'reason': 'Cairn has no statement counter; a separate SQLite observer cannot count application queries.'},
                'queue_age_bounds': 'Pending-job scheduled lateness and running-grade age only; schema has no enqueue timestamp, so true queue residence age is unavailable.',
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
