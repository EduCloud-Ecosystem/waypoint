import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'rehearsal'))
from course_load import check_work, local_origin, phase, summarize


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


if __name__ == '__main__':
    unittest.main()
