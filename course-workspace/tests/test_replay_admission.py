"""Real SQLite replay contention and fail-closed FIFO admission; no provider I/O."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import authority_access as access
from hub.authority_client import AuthorityError, VERSION


class ReplayAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = access.ReplayStore.create(self.root / 'replay', 'a' * 32)

    def frame(self):
        return dict(version=VERSION, authority_id='a' * 32, nonce=uuid.uuid4().hex,
                    issued_at=int(time.time()), operation='guardian.observe',
                    worker_id='one', course_id='main', payload={})

    def consume(self, frame=None):
        self.store.consume(frame or self.frame(), 'b' * 64)

    def count(self):
        with self.store._connection() as conn:
            return conn.execute('SELECT count(*) FROM requests').fetchone()[0]

    def test_concurrent_distinct_nonces_complete_and_reopen_with_full_history(self):
        barrier = threading.Barrier(8)
        def worker(_):
            barrier.wait(timeout=10)
            for _ in range(12): self.consume()
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(8)))
        self.assertEqual(self.count(), 96)
        access.ReplayStore.open(self.store.directory, 'a' * 32)

    def test_waiters_open_sqlite_only_after_the_active_writer_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store._validate
        def validate(conn, full=False):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(conn, full)
        with patch.object(self.store, '_validate', side_effect=validate), \
                patch.object(self.store, '_raw', wraps=self.store._raw) as raw, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.consume)
            self.assertTrue(entered.wait(5))
            second = pool.submit(self.consume)
            try:
                with self.store._writers:
                    self.assertTrue(self.store._writers.wait_for(lambda: len(self.store._waiting) == 2, timeout=.5))
                self.assertEqual(raw.call_count, 1)
            finally: release.set()
            first.result(5); second.result(5)
        self.assertEqual(self.count(), 2)

    def test_queue_expiry_does_not_consume_nonce_or_strand_following_requests(self):
        frame = self.frame()
        with self.store._writer(), patch.object(self.store, '_raw', wraps=self.store._raw) as raw:
            with patch.object(access.time, 'monotonic', side_effect=[0, 2]), \
                    self.assertLogs('authority_access', level='WARNING') as logs:
                with self.assertRaisesRegex(AuthorityError, 'authority_replay_state_unavailable'):
                    self.consume(frame)
            self.assertEqual(raw.call_count, 0)
        self.assertIn('stage=queue category=deadline', logs.output[0])
        self.assertEqual(self.count(), 0)
        self.consume(frame)
        with self.assertRaisesRegex(AuthorityError, 'authority_replay_refused'): self.consume(frame)

    def test_canceled_waiter_is_removed(self):
        with self.store._writer():
            with patch.object(threading.Event, 'wait', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt): self.consume()
        self.consume()
        self.assertEqual(self.count(), 1)

    def test_sqlite_busy_budget_uses_remaining_time_and_never_resets_to_one_second(self):
        with patch.object(access.time, 'monotonic', return_value=10):
            conn = self.store._raw(deadline=10.125)
            try: self.assertEqual(conn.execute('PRAGMA busy_timeout').fetchone()[0], 125)
            finally: conn.close()
            with self.assertRaises(TimeoutError): self.store._raw(deadline=10)

    def test_external_writer_still_refuses_without_consuming_or_recreating_state(self):
        frame = self.frame()
        blocker = self.store._raw()
        blocker.execute('BEGIN IMMEDIATE')
        try:
            with self.assertLogs('authority_access', level='WARNING') as logs:
                with self.assertRaisesRegex(AuthorityError, 'authority_replay_state_unavailable'):
                    self.consume(frame)
            self.assertIn('stage=begin category=sqlite_busy', logs.output[0])
        finally: blocker.close()
        self.assertEqual(self.count(), 0)
        self.consume(frame)
        self.assertEqual(self.count(), 1)

    def test_binding_validation_and_commit_failures_release_admission(self):
        for method in ('_raw', '_validate'):
            with self.subTest(method=method):
                frame = self.frame()
                with patch.object(self.store, method, side_effect=ValueError('private-canary')):
                    with self.assertRaisesRegex(AuthorityError, 'authority_replay_state_unavailable'): self.consume(frame)
                self.consume(frame)
        frame = self.frame()
        # A deadline reached after INSERT must roll the transaction back.
        original = self.store._busy_budget
        calls = []
        def budget(deadline):
            calls.append(deadline)
            if len(calls) == 3: raise TimeoutError('private-canary')
            return original(deadline)
        before = self.count()
        with patch.object(self.store, '_busy_budget', side_effect=budget):
            with self.assertRaisesRegex(AuthorityError, 'authority_replay_state_unavailable'): self.consume(frame)
        self.assertEqual(self.count(), before)
        self.consume(frame)
        access.ReplayStore.open(self.store.directory, 'a' * 32)

    def test_diagnostic_output_has_only_fixed_categories(self):
        error = sqlite3.OperationalError('private-token-path-canary')
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with self.assertLogs('authority_access', level='WARNING') as logs:
            access.replay_diagnostic('begin', error)
            access.replay_diagnostic('private-stage-canary', ValueError('private-message'))
        self.assertIn('stage=begin category=sqlite_busy', logs.output[0])
        self.assertIn('stage=binding category=integrity', logs.output[1])
        self.assertNotIn('private', json.dumps(logs.output))

    def test_failed_diagnostic_cannot_change_refusal_or_strand_writer(self):
        frame = self.frame()
        with patch.object(self.store, '_raw', side_effect=ValueError('private-failure')), \
                patch.object(access, 'replay_diagnostic', side_effect=ValueError('private-diagnostic')):
            with self.assertRaisesRegex(AuthorityError, 'authority_replay_state_unavailable'): self.consume(frame)
        self.assertEqual(self.count(), 0)
        self.consume(frame)
        self.assertEqual(self.count(), 1)


if __name__ == '__main__': unittest.main()
