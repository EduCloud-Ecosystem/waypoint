"""Failures must clean partial fixtures and cannot produce overall success."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from cairn_journey import Course, public_metrics
from course_load import MemorySampler
from notebook import Notebook, SOLUTION
import verify_course


class NotebookCleanupTests(unittest.TestCase):
    def test_partial_failure_still_attempts_network_and_unmount(self):
        for volume_failure in (False, True):
            with self.subTest(volume_failure=volume_failure), tempfile.TemporaryDirectory() as directory:
                book = Notebook(Path(directory))
                book.homes = Path(directory) / 'homes'
                book.mounted = True
                resources = {'container': {'a' * 64}, 'volume': {'own-volume'}, 'network': {'own-network'}}
                calls, mounted = [], [True]

                def command(args, **kwargs):
                    calls.append(args)
                    if args[0] == 'umount':
                        mounted[0] = False
                        return SimpleNamespace(returncode=0, stdout='')
                    if args[1] == 'ps':
                        kind = 'container'
                    elif args[1] in ('volume', 'network') and args[2] == 'ls':
                        kind = args[1]
                    else:
                        kind = 'container' if args[1] == 'rm' else args[1]
                        if volume_failure and kind == 'volume':
                            return SimpleNamespace(returncode=1, stdout='')
                        resources[kind].discard(args[-1])
                        return SimpleNamespace(returncode=0, stdout='')
                    return SimpleNamespace(returncode=0, stdout='\n'.join(resources[kind]))

                with patch('notebook.subprocess.run', side_effect=command), patch('notebook.os.path.ismount', side_effect=lambda _: mounted[0]):
                    if volume_failure:
                        with self.assertRaises(RuntimeError):
                            book.close()
                    else:
                        self.assertTrue(book.close())
                self.assertEqual(book.cleanup_verified, not volume_failure)
                self.assertFalse(resources['container'])
                self.assertFalse(resources['network'])
                self.assertFalse(mounted[0])
                self.assertTrue(all('--force' not in c for c in calls if c[1:3] == ['volume', 'rm']))

    def test_daemon_failure_cannot_be_treated_as_resource_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            book = Notebook(Path(directory))
            with patch('notebook.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='')) as command:
                with self.assertRaises(RuntimeError):
                    book.close()
            self.assertFalse(book.cleanup_verified)
            self.assertGreaterEqual(command.call_count, 9)

    def test_timeout_does_not_abort_attempts_to_clean_other_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            book = Notebook(Path(directory))
            with patch('notebook.subprocess.run', side_effect=subprocess.TimeoutExpired('docker', 30)) as command:
                with self.assertRaises(RuntimeError):
                    book.close()
            self.assertFalse(book.cleanup_verified)
            self.assertGreaterEqual(command.call_count, 9)

    def test_mount_leftover_prevents_success_even_when_docker_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            book = Notebook(Path(directory))
            book.homes = Path(directory) / 'homes'
            book.mounted = True
            with patch('notebook.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='')), patch('notebook.os.path.ismount', return_value=True):
                with self.assertRaises(RuntimeError):
                    book.close()
            self.assertFalse(book.cleanup_verified)


class CourseCleanupTests(unittest.TestCase):
    def course(self, root):
        return Course(root, root / 'cairn', 'http://fixture.invalid', 'course:fixture', root)

    def test_partial_start_daemon_failure_blocks_success(self):
        with tempfile.TemporaryDirectory() as directory:
            course = self.course(Path(directory))
            with patch('cairn_journey.subprocess.run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='')), patch('cairn_journey.run', side_effect=RuntimeError('daemon unavailable')) as command:
                with self.assertRaises(RuntimeError):
                    course.close()
            self.assertEqual(command.call_count, 6)

    def test_failed_diagnostic_write_does_not_skip_owned_resource_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            course = self.course(Path(directory))
            with patch('cairn_journey.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')), patch.object(Path, 'write_text', side_effect=OSError('disk full')), patch('cairn_journey.run', return_value='') as command:
                with self.assertRaises(RuntimeError):
                    course.close()
            self.assertEqual(command.call_count, 6)

    def test_runtime_recovery_failure_still_removes_fixture_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            course = self.course(Path(directory))
            course.env = {'TMPDIR': str(Path(directory) / 'grading')}
            resources = {'container': {'fixture-container'}, 'volume': {'fixture-volume'}, 'network': {'fixture-network'}}
            def command(*args, **kwargs):
                if args[0] != 'docker':
                    raise RuntimeError('runtime recovery refused active worker')
                if args[1] == 'ps':
                    return '\n'.join(resources['container'])
                if args[2] == 'ls':
                    return '\n'.join(resources[args[1]])
                kind = 'container' if args[1] == 'rm' else args[1]
                resources[kind].discard(args[-1])
                return ''
            with patch('cairn_journey.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')), patch('cairn_journey.run', side_effect=command):
                with self.assertRaises(RuntimeError):
                    course.close()
            self.assertTrue(all(not values for values in resources.values()))

    def test_successful_apply_requires_independent_empty_runtime_inventory(self):
        for observed in ({'resources': []}, {'resources': [{'type': 'container', 'id': 'leftover'}]},
                         {}, {'resources': None}, {'resources': ''}, []):
            with self.subTest(observed=observed), tempfile.TemporaryDirectory() as directory:
                course = self.course(Path(directory))
                course.env = {'TMPDIR': str(Path(directory) / 'grading')}
                calls = []
                def command(*args, **kwargs):
                    calls.append(args)
                    if args[0] == 'docker':
                        return ''  # All fixture labels are absent, regardless of Cairn resources.
                    if '--plan' in args:
                        Path(args[-1]).write_text('{"resources": []}')
                        return ''
                    if '--apply' in args:
                        return 'Reviewed runtime recovery completed.'
                    return json.dumps(observed)
                with patch('cairn_journey.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')), patch('cairn_journey.run', side_effect=command):
                    if observed == {'resources': []}:
                        self.assertTrue(course.close())
                    else:
                        with self.assertRaises(RuntimeError):
                            course.close()
                runtime_calls = [c for c in calls if c[0] != 'docker']
                self.assertEqual(len(runtime_calls), 3)
                self.assertIn('--apply', runtime_calls[1])
                self.assertNotIn('--plan', runtime_calls[2])
                self.assertNotIn('--apply', runtime_calls[2])


class PublicMetricsTests(unittest.TestCase):
    def report(self):
        resource = {'cairn_rss_kib_point_sample': 1234, 'sqlite_bytes': 4096,
                    'queue_status_counts': {'provisioning_jobs': {'succeeded': 15},
                                            'grading_runs': {'completed': 9}},
                    'queue_observations': {'pending_jobs': 0, 'due_jobs': 0, 'future_scheduled_jobs': 0,
                        'pending_job_timestamps_unavailable': 0, 'oldest_due_job_lateness_seconds': None,
                        'running_grading_runs': 0, 'running_grade_timestamps_unavailable': 0,
                        'oldest_running_grade_age_seconds': None}}
        phase = {'memory_observations': MemorySampler(123).result(), 'concurrent_learners': 5, 'requests': 100, 'errors': 0, 'elapsed_seconds': .2,
                 'p50_ms': 2, 'p95_ms': 5, 'max_ms': 9,
                 'routes': {r: {'requests': 25, 'errors': 0, 'p50_ms': 2, 'p95_ms': 5, 'max_ms': 9}
                            for r in ('list', 'own', 'other', 'operator')},
                 'resources_before': resource, 'resources_after': resource}
        return {'push_count': 6, 'queue_drain_seconds': 10, 'load': {'phases': [phase]}}

    def test_queue_observations_survive_public_filter_without_claiming_a_peak(self):
        metrics = public_metrics(self.report())
        for point in ('before', 'after'):
            self.assertEqual(metrics[f'load_5_{point}_provisioning_jobs_succeeded'], 15)
            self.assertEqual(metrics[f'load_5_{point}_grading_runs_completed'], 9)
            self.assertEqual(metrics[f'load_5_{point}_grading_runs_running'], 0)
        self.assertTrue(all(type(value) in (int, float) for value in metrics.values()))
        self.assertFalse(any('peak' in key for key in metrics))

    def test_unknown_queue_fields_are_not_silently_published_or_ignored(self):
        for invalid in ({'private-error': 1}, {'pending': True}, {'failed': -1}):
            report = self.report()
            report['load']['phases'][0]['resources_before']['queue_status_counts']['grading_runs'] = invalid
            with self.assertRaises(ValueError):
                public_metrics(report)


class NotebookPersistenceTests(unittest.TestCase):
    def test_saved_bytes_and_distinct_replacement_are_required(self):
        for wrong_bytes, same_container in ((False, False), (True, False), (False, True)):
            with self.subTest(wrong_bytes=wrong_bytes, same_container=same_container), tempfile.TemporaryDirectory() as directory:
                book = Notebook(Path(directory))
                book.homes = Path(directory) / 'homes'
                session = Mock()
                session.get.side_effect = lambda url, **kwargs: SimpleNamespace(
                    status_code=200,
                    json=lambda: {'server': None} if '/hub/api/' in url else {'content': 'wrong' if wrong_bytes else SOLUTION},
                    raise_for_status=lambda: None)
                book.ready = Mock(return_value=(session, 'https://fixture.invalid/new'))
                book.kernel = Mock()
                with patch('notebook.run', side_effect=['a' * 64, '', ('a' if same_container else 'b') * 64]), patch('notebook.change', return_value=SimpleNamespace(status_code=204)), patch('notebook.inspect'):
                    if wrong_bytes or same_container:
                        with self.assertRaises(AssertionError):
                            book.restart_saved_file(None, session, 'https://fixture.invalid/old')
                    else:
                        book.restart_saved_file(None, session, 'https://fixture.invalid/old')
                self.assertEqual('saved-file-after-server-restart' in book.report, not wrong_bytes and not same_container)


class FullPilotTests(unittest.TestCase):
    def test_notebook_phases_require_observed_checks(self):
        all_checks = set().union(*verify_course.NOTEBOOK_CHECKS.values())
        self.assertTrue(all(verify_course.notebook_phases(all_checks).values()))
        for phase, needed in verify_course.NOTEBOOK_CHECKS.items():
            for check in needed:
                self.assertFalse(verify_course.notebook_phases(all_checks - {check})[phase])

    def test_partial_notebook_start_failure_cleans_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            notebook = Mock()
            notebook.start.side_effect = RuntimeError('partial fixture startup failed')
            argv = ['verify_course.py', '--cairn-binary', '/unused', '--cairn-source', '/unused',
                    '--lti-binary', '/unused', '--output', str(Path(directory) / 'run')]
            with patch('verify_course.sys.argv', argv), patch('verify_course.sys.platform', 'linux'), patch('verify_course.os.geteuid', return_value=0), patch('verify_course.Notebook', return_value=notebook), patch('verify_course.write_pass') as publish:
                with self.assertRaises(RuntimeError):
                    verify_course.main()
            notebook.close.assert_called_once()
            publish.assert_not_called()

    def test_forgejo_is_resolved_before_full_provenance_and_failure_cleans(self):
        with tempfile.TemporaryDirectory() as directory:
            notebook = Mock(image='course:fixture', instance='fixture')
            events = []
            argv = ['verify_course.py', '--cairn-binary', '/unused', '--cairn-source', '/unused',
                    '--lti-binary', '/unused', '--output', str(Path(directory) / 'run')]
            def provenance(*args, **kwargs):
                events.append('provenance')
                raise RuntimeError('fixture provenance failed')
            with patch('verify_course.sys.argv', argv), patch('verify_course.sys.platform', 'linux'), patch('verify_course.os.geteuid', return_value=0), patch('verify_course.Notebook', return_value=notebook), patch('verify_course.run', side_effect=lambda *a, **k: events.append('pull')), patch('verify_course.collect_provenance', side_effect=provenance), patch('verify_course.write_pass') as publish:
                with self.assertRaises(RuntimeError):
                    verify_course.main()
            self.assertEqual(events, ['pull', 'provenance'])
            notebook.close.assert_called_once()
            publish.assert_not_called()


if __name__ == '__main__':
    unittest.main()
