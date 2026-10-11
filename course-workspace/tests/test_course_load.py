from datetime import datetime, timezone
import json
import os
import sqlite3
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rehearsal'))
from course_load import (check_work, local_origin, phase, summarize, MemorySampler,
                         read_memory_sample, queue_observations, public_observability_metrics)


class Handler(BaseHTTPRequestHandler):
    leak = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        learner = self.headers.get('Cookie', '').removeprefix('student=')
        item = {'submission_id': learner, 'status': 'active'}
        status, body = 200, item
        if self.path == '/me/work':
            body = {'work': [item]}
            if self.server.leak:
                body['work'].append({'submission_id': 'another-student', 'status': 'active'})
        elif self.path == '/classrooms':
            status, body = 401, {'error': 'denied'}
        elif self.path != '/me/work/' + learner:
            status, body = 404, {'error': 'missing'}
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


class CourseLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.leak = False
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def tearDown(self):
        self.server.leak = False

    def learners(self, count):
        return [{'submission_id': f'sub-{index}',
                 'cookies': [{'name': 'student', 'value': f'sub-{index}',
                              'domain': '127.0.0.1', 'path': '/'}]}
                for index in range(count)]

    def test_distinct_authenticated_sessions_and_denials(self):
        for count in (5, 15):
            with self.subTest(count=count):
                result = phase(self.url, self.learners(count), rounds=1)
                self.assertEqual(result['requests'], count * 4)
                self.assertEqual(result['errors'], 0)
                self.assertEqual(result['routes']['other']['requests'], count)
                self.assertGreater(result['max_ms'], 0)

    def test_success_status_with_cross_learner_data_fails(self):
        self.server.leak = True
        result = phase(self.url, self.learners(5), rounds=1)
        self.assertEqual(result['errors'], 5)
        self.assertEqual(result['routes']['list']['errors'], 5)
        self.assertNotIn('Cookie', json.dumps(result))

    def test_bounds_and_duplicate_identity_fail_before_http(self):
        for count in (1, 16):
            with self.assertRaises(ValueError):
                phase(self.url, self.learners(count))
        duplicated = self.learners(5)
        duplicated[1]['submission_id'] = duplicated[0]['submission_id']
        with self.assertRaises(ValueError):
            phase(self.url, duplicated)
        with self.assertRaises(ValueError):
            phase(self.url, self.learners(5), rounds=6)
        for url in ('https://example.org', 'http://0.0.0.0:80', 'http://secret@127.0.0.1:80'):
            with self.assertRaises(ValueError):
                local_origin(url)

    def test_grade_and_work_ownership_checks(self):
        for item in (None, [], {'submission_id': 'wrong', 'status': 'active'},
                     {'submission_id': 'owned', 'status': 'pending'},
                     {'submission_id': 'owned', 'status': 'active', 'latest_grade': {'score': 10}},
                     {'submission_id': 'owned', 'status': 'active', 'history': [{'score': 10}]}):
            with self.assertRaises(AssertionError):
                check_work(item, 'owned', detail=True)

    def test_nearest_rank_percentiles(self):
        self.assertEqual(summarize(range(1, 101)), {'p50_ms': 50, 'p95_ms': 95, 'max_ms': 100})
        with self.assertRaises(ValueError):
            summarize([])


class ObservabilityTests(unittest.TestCase):
    def test_linux_native_high_water_is_separate_from_sampled_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / '123').mkdir()
            (proc / '123/status').write_text('Name: private-name\nVmRSS: 3210 kB\nVmHWM: 7654 kB\n')
            with patch('course_load.sys.platform', 'linux'):
                result = read_memory_sample(123, proc)
                self.assertEqual(result['rss_kib'], 3210)
                self.assertEqual(result['lifetime_hwm_kib'], 7654)
                self.assertNotIn('private-name', json.dumps(result))
                (proc / '123/status').write_text('Name: exited\n')
                with self.assertRaises(ValueError):
                    read_memory_sample(123, proc)

    def test_current_process_sampling_has_bounded_scalar_evidence(self):
        with MemorySampler(os.getpid(), interval=.01) as sampler:
            time.sleep(.04)
        result = sampler.result()
        self.assertGreaterEqual(result['sample_count'], 2)
        self.assertGreater(result['rss_observed_max_kib'], 0)
        self.assertFalse(result['peak_is_exact'])
        self.assertEqual(result['sample_errors'], 0)
        self.assertLessEqual(result['sample_attempts'], 6000)
        self.assertFalse(sampler.thread.is_alive())
        self.assertNotIn('samples', result)

    def test_sample_failure_and_exhaustion_are_visible_without_zero_peak(self):
        ticks = iter([0, .07, .2])
        values = iter([{'rss_kib': 100, 'lifetime_hwm_kib': 300, 'source': 'linux-proc-status'},
                       OSError('unavailable'),
                       {'rss_kib': 150, 'lifetime_hwm_kib': 400, 'source': 'linux-proc-status'}])
        def read(_):
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value
        sampler = MemorySampler(123, reader=read, clock=lambda: next(ticks), max_samples=3)
        for _ in range(4):
            sampler.take()
        result = sampler.result()
        self.assertEqual(result['rss_observed_max_kib'], 150)
        self.assertEqual(result['kernel_process_lifetime_hwm_kib'], 400)
        self.assertEqual(result['sample_count'], 2)
        self.assertEqual(result['sample_errors'], 1)
        self.assertEqual(result['max_attempt_gap_ms'], 130)
        self.assertTrue(result['sample_budget_exhausted'])
        missing = MemorySampler(123, reader=lambda _: (_ for _ in ()).throw(OSError('unavailable')))
        missing.take()
        self.assertIsNone(missing.result()['rss_observed_max_kib'])
        self.assertIsNone(missing.result()['kernel_process_lifetime_hwm_kib'])

    def database(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE provisioning_jobs(status TEXT, scheduled_at TEXT)')
        conn.execute('CREATE TABLE grading_runs(status TEXT, started_at TEXT)')
        return conn

    def test_queue_lateness_excludes_retry_delays_completed_and_unknown_times(self):
        conn = self.database()
        conn.executemany('INSERT INTO provisioning_jobs VALUES (?,?)', [
            ('pending', '2026-10-11T11:59:40Z'), ('pending', '2026-10-11T12:01:00Z'),
            ('pending', 'unknown'), ('succeeded', '2025-10-11T00:00:00Z')])
        conn.executemany('INSERT INTO grading_runs VALUES (?,?)', [
            ('running', '2026-10-11T11:59:10Z'), ('running', None),
            ('running', '2026-10-11T12:01:00Z'), ('completed', '2025-10-11T00:00:00Z')])
        result = queue_observations(conn, datetime(2026, 10, 11, 12, tzinfo=timezone.utc))
        self.assertEqual(result['pending_jobs'], 3)
        self.assertEqual(result['due_jobs'], 1)
        self.assertEqual(result['future_scheduled_jobs'], 1)
        self.assertEqual(result['pending_job_timestamps_unavailable'], 1)
        self.assertEqual(result['oldest_due_job_lateness_seconds'], 20)
        self.assertEqual(result['oldest_running_grade_age_seconds'], 50)
        self.assertEqual(result['running_grade_timestamps_unavailable'], 2)

    def test_empty_queues_and_missing_memory_do_not_publish_measured_zero_age_or_peak(self):
        conn = self.database()
        observed = queue_observations(conn, datetime.now(timezone.utc))
        self.assertIsNone(observed['oldest_due_job_lateness_seconds'])
        memory = MemorySampler(123).result()
        result = public_observability_metrics({'memory_observations': memory,
            'resources_before': {'queue_observations': observed},
            'resources_after': {'queue_observations': observed}})
        self.assertEqual(result['database_query_counts_available'], 0)
        self.assertNotIn('database_query_count', result)
        self.assertEqual(result['memory_rss_observed_max_kib_available'], 0)
        self.assertNotIn('memory_rss_observed_max_kib', result)
        self.assertEqual(result['before_oldest_due_job_lateness_seconds_available'], 0)
        self.assertNotIn('before_oldest_due_job_lateness_seconds', result)
        observed['private_job_error'] = 1
        with self.assertRaises(ValueError):
            public_observability_metrics({'memory_observations': memory,
                'resources_before': {'queue_observations': observed},
                'resources_after': {'queue_observations': observed}})

    def test_queue_observation_row_budget_is_enforced(self):
        conn = self.database()
        conn.executemany('INSERT INTO provisioning_jobs VALUES (?,?)', [('pending', None)] * 1001)
        with self.assertRaises(ValueError):
            queue_observations(conn, datetime.now(timezone.utc))


if __name__ == '__main__':
    unittest.main()
