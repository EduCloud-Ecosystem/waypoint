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


class EnrollmentInstallTests(unittest.TestCase):
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
