"""Credential replacement must refuse scope/candidate drift before Docker effects."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
import enrollment_fixture as fixture
from hub.authority_client import AuthorityError


class EnrollmentInstallTests(unittest.TestCase):
    def test_guardian_probe_distinguishes_valid_grant_busy_and_actual_access_denial(self):
        class Client:
            def __init__(self, response): self.response = response
            def call(self, op, payload):
                self_outer.assertEqual(op, 'guardian.observe'); self_outer.assertEqual(payload, {'identity': 'synthetic'})
                if isinstance(self.response, Exception): raise self.response
                return self.response
        self_outer = self
        grant = {'authorized': True, 'epoch': 1, 'worker_id': 'worker-two', 'course_id': 'course-main'}
        busy = {'authorized': False, 'deferred': True, 'worker_id': 'worker-two', 'course_id': 'course-main'}
        for response, expected in ((grant, {'authorized': True}), (busy, {'authorized': False, 'deferred': True}),
                                   (AuthorityError('authority_access_refused'), {'authorized': False, 'denied': True})):
            self.assertEqual(fixture._guardian_probe(Client(response), {'identity': 'synthetic'}, 'worker-two', 'course-main'), expected)
        malformed = [dict(grant, authorized=1), dict(grant, epoch=True), dict(grant, worker_id='worker-one'),
            dict(grant, course_id='course-peer'), dict(grant, extra='private'), dict(busy, deferred=1),
            dict(busy, authorized=0), dict(busy, epoch=1), dict(busy, worker_id='worker-one'),
            {'authorized': False}, None]
        for response in malformed:
            with self.subTest(response=response), self.assertRaisesRegex(ValueError, '^fixture_guardian_probe_invalid$'):
                fixture._guardian_probe(Client(response), {'identity': 'synthetic'}, 'worker-two', 'course-main')

    def test_guardian_probe_never_converts_transport_unexpected_or_operation_failures_to_denial(self):
        for error in (AuthorityError('authority_transport_unavailable'), AuthorityError('authority_operation_refused'),
                      AuthorityError('authority_response_invalid'), RuntimeError('PRIVATE_PROBE_CANARY')):
            class Failed:
                def call(self, op, payload): raise error
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)) as caught:
                fixture._guardian_probe(Failed(), {}, 'worker-two', 'course-main')
            self.assertIs(caught.exception, error)

    def test_contention_probe_requires_exact_authenticated_no_grant_shape(self):
        result = {'authorized': False, 'deferred': True, 'worker_id': 'worker-one', 'course_id': 'course-main'}
        self.assertTrue(fixture._deferred(result, 'worker-one', 'course-main'))
        for field, value in [('authorized', 0), ('deferred', 1), ('worker_id', 'worker-two'),
                             ('course_id', 'course-peer'), ('epoch', 1)]:
            changed = dict(result); changed[field] = value
            self.assertFalse(fixture._deferred(changed, 'worker-one', 'course-main'))
        for value in (None, {}, dict(result, authorized=True)):
            self.assertFalse(fixture._deferred(value, 'worker-one', 'course-main'))

    def test_contention_holder_rejects_unconfigured_scope_or_unbounded_duration(self):
        for course, seconds in [('other', 4), ('course-main', 999), ('course-peer', -1)]:
            with patch.object(fixture, 'DockerBackend', side_effect=AssertionError('no provider before scope validation')):
                with self.assertRaisesRegex(ValueError, '^fixture_contention_invalid$'):
                    fixture._hold_endpoint(Path('/unused'), course, seconds)

    def test_other_live_role_worker_course_or_candidate_refused_before_docker(self):
        certificate = {'fingerprint': 'a' * 64, 'spki_sha256': 'b' * 64, 'not_before': 1, 'not_after': 9999999999}
        valid_entry = {'role': 'hub', 'workers': ['worker-one'], 'courses': ['course-main']}
        variants = [None, {'role': 'guardian', 'workers': ['worker-one'], 'courses': ['course-main']},
            {'role': 'hub', 'workers': ['worker-two'], 'courses': ['course-main']},
            {'role': 'hub', 'workers': ['worker-one'], 'courses': ['course-peer']},
            {'role': 'hub', 'workers': ['worker-one'], 'courses': ['course-main', 'course-peer']}]
        for entry in variants + [valid_entry]:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve(); previous = root / 'source' / 'credentials'
                previous.mkdir(parents=True); old = previous / 'client.crt'; old.write_text('original certificate sentinel')
                candidate = deepcopy(certificate)
                if entry == valid_entry: candidate['fingerprint'] = 'c' * 64
                fixture.write(root / 'enrollment-source-candidate.json', candidate)
                config = {'courses': {'worker-one': {'course-main': {}}}}
                service = {'ca_file': '/unused/ca.pem'}
                store = types.SimpleNamespace(entries=lambda: {} if entry is None else {certificate['fingerprint']: entry})
                docker = types.SimpleNamespace(DockerClient=lambda **kwargs: self.fail('Docker effect before scope verification'))
                with patch.dict(sys.modules, {'docker': docker}), patch.object(fixture, 'context', return_value=(config, service, None, store)), patch.object(fixture, 'verify_certificate', return_value=certificate):
                    with self.assertRaises(ValueError): fixture.dispatch(root, 'enrollment-install', {'identity': 'source'})
                self.assertEqual(old.read_text(), 'original certificate sentinel')
                self.assertFalse((root / 'source-previous').exists())

    def test_failure_diagnostic_never_exposes_exception_or_private_file_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            error = ValueError('private fixture exception canary')
            with patch.object(fixture, '_dispatch', side_effect=error):
                with self.assertRaises(ValueError): fixture.dispatch(root, 'enrollment-create', {})
            self.assertEqual(fixture.dispatch(root, 'enrollment-failure-status', {}), {'code': 'fixture_enrollment_refused'})
            fixture.write(root / 'enrollment-failure.json', {'code': 'private fixture file canary'})
            self.assertEqual(fixture.dispatch(root, 'enrollment-failure-status', {}), {'code': 'fixture_enrollment_refused'})


if __name__ == '__main__': unittest.main()
