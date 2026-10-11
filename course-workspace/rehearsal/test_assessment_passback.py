"""Pure fixture checks; the authenticated browser rehearsal remains authoritative."""
import copy
import subprocess
import unittest
from unittest.mock import Mock

from assessment_passback import AssessmentPassback, proposal


def captured(digest='a' * 64, source_hash='b' * 64):
    return {'document': {'input_digest': digest, 'artifacts': [{
        'path': 'response.md', 'sha256': source_hash,
        'segments': [{'location': 'line:1', 'text': 'Synthetic source evidence.'}]}]}}


class ProposalTests(unittest.TestCase):
    def test_repeated_import_is_reproducible_without_changing_capture(self):
        record = captured()
        original = copy.deepcopy(record)
        first = proposal(record)
        self.assertEqual(first, proposal(record))
        self.assertEqual(record, original)
        self.assertEqual(first['source'], 'fixture')
        self.assertNotIn('usage', first)
        first['criteria'][0]['citations'][0]['quote'] = 'Mutated external data'
        self.assertEqual(record, original)

    def test_new_capture_requires_its_own_digest_and_citation(self):
        old = proposal(captured())
        new = proposal(captured('c' * 64, 'd' * 64))
        self.assertNotEqual(old['input_digest'], new['input_digest'])
        self.assertNotEqual(old['criteria'][0]['citations'][0]['sha256'],
                            new['criteria'][0]['citations'][0]['sha256'])
        self.assertEqual(new['criteria'][0]['citations'][0]['location'], 'line:1')
        self.assertEqual(new['criteria'][0]['citations'][0]['quote'], 'Synthetic source evidence.')

    def test_unassessable_is_null_not_zero(self):
        self.assertIsNone(proposal(captured(), None)['criteria'][0]['points'])
        self.assertEqual(proposal(captured(), 0)['criteria'][0]['points'], 0)


class CleanupTests(unittest.TestCase):
    def fixture(self):
        fixture = AssessmentPassback.__new__(AssessmentPassback)
        fixture.contexts = [Mock(), Mock()]
        fixture.process = Mock()
        fixture.process.poll.return_value = None
        fixture.log = Mock()
        return fixture

    def test_context_failure_does_not_skip_other_contexts_or_process(self):
        fixture = self.fixture()
        first, second = fixture.contexts
        first.close.side_effect = RuntimeError('private browser detail')
        with self.assertRaisesRegex(RuntimeError, '^assessment fixture cleanup failed: RuntimeError$'):
            fixture.close()
        second.close.assert_called_once()
        fixture.process.terminate.assert_called_once()
        fixture.process.wait.assert_called_once_with(timeout=10)
        fixture.log.close.assert_called_once()
        self.assertEqual(fixture.contexts, [])

    def test_stubborn_simulator_is_killed_and_reaped(self):
        fixture = self.fixture()
        fixture.process.wait.side_effect = [subprocess.TimeoutExpired('simulator', 10), 0]
        fixture.close()
        fixture.process.kill.assert_called_once()
        self.assertEqual(fixture.process.wait.call_count, 2)
        fixture.process.wait.assert_called_with(timeout=5)
        fixture.log.close.assert_called_once()

    def test_reap_failure_still_closes_log(self):
        fixture = self.fixture()
        fixture.process.wait.side_effect = RuntimeError('private process detail')
        with self.assertRaisesRegex(RuntimeError, 'cleanup failed'):
            fixture.close()
        fixture.log.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
