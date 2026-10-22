# SPDX-License-Identifier: Apache-2.0
"""Offline fixture-binding refusal controls, not real API/reviewer acceptance."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cw1_review', ROOT / 'rehearsal/info511_review_fixture.py')
REVIEW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REVIEW)
PACKAGE = ROOT / 'classroom/info511-lab03-q4-v1'


class ReviewFixtureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((PACKAGE / 'operator/reviewer-fixture.json').read_text())
        self.rubric = json.loads((PACKAGE / 'operator/rubric.json').read_text())
        artifacts, changes = [], []
        for name in sorted(REVIEW.FILES):
            raw = (PACKAGE / 'operator/attempts/alice' / name).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if name.endswith('.ipynb'):
                segments = [{'location': f'cell:{i+1}', 'text': ''.join(cell['source'])}
                            for i, cell in enumerate(json.loads(raw)['cells'])]
            else:
                segments = [{'location': f'line:{i+1}', 'text': line}
                            for i, line in enumerate(raw.decode().split('\n'))]
            artifacts.append({'path': name, 'size': len(raw), 'sha256': digest,
                              'original': base64.b64encode(raw).decode(), 'segments': segments})
            changes.append({'path': name, 'bytes': len(raw), 'sha256': digest})
        self.receipt = {'commit': 'a' * 40, 'changes': changes}
        self.record = {'status': 'collected', 'revision': 'a' * 40,
                       'document': {'revision': 'a' * 40, 'input_digest': 'b' * 64,
                                    'rubric': self.rubric, 'artifacts': artifacts}}

    def inputs(self):
        return REVIEW.review_inputs(self.record, self.receipt, self.fixture, 'alice', self.rubric)

    def test_explicit_fixture_labels_and_distinct_review_inputs(self):
        proposal, review = self.inputs()
        self.assertEqual(proposal['source'], 'fixture')
        self.assertEqual(proposal['model'], 'none-automated-synthetic-reviewer')
        self.assertEqual(sum(c['points'] for c in proposal['criteria']), 5)
        self.assertEqual(sum(c['points'] for c in review['criteria']), 6)
        self.assertIn('no human review', review['note'])

    def test_changed_original_or_fixture_or_receipt_hash_refused(self):
        for target in ('original', 'fixture', 'receipt'):
            with self.subTest(target=target):
                self.setUp()
                name = self.record['document']['artifacts'][0]['path']
                if target == 'original':
                    self.record['document']['artifacts'][0]['original'] = base64.b64encode(b'changed').decode()
                elif target == 'fixture':
                    self.fixture['learners']['alice']['artifacts'][name]['sha256'] = '0' * 64
                else:
                    self.receipt['changes'][0]['sha256'] = '0' * 64
                with self.assertRaises(ValueError):
                    self.inputs()

    def test_missing_duplicate_issued_or_stale_evidence_refused(self):
        for target in ('missing', 'duplicate', 'issue', 'stale'):
            with self.subTest(target=target):
                self.setUp()
                artifacts = self.record['document']['artifacts']
                if target == 'missing':
                    artifacts.pop()
                elif target == 'duplicate':
                    artifacts[0] = copy.deepcopy(artifacts[1])
                elif target == 'issue':
                    artifacts[0]['issue'] = 'Unsupported format'
                else:
                    self.record['document']['revision'] = 'c' * 40
                with self.assertRaises(ValueError):
                    self.inputs()

    def test_quote_and_location_must_match_captured_segment(self):
        for key, value in [('quote', 'invented quote'), ('location', 'line:999')]:
            with self.subTest(key=key):
                self.setUp()
                self.fixture['learners']['alice']['citations']['correctness'][key] = value
                with self.assertRaises(ValueError):
                    self.inputs()

    def test_nonfinite_boolean_and_out_of_range_points_refused(self):
        for points in (float('nan'), True, 5, -1):
            with self.subTest(points=points):
                self.fixture['learners']['alice']['review_points']['correctness'] = points
                with self.assertRaises(ValueError):
                    self.inputs()

    def test_wrong_actor_task_or_rubric_refused(self):
        for target in ('actor', 'task', 'rubric'):
            with self.subTest(target=target):
                self.setUp()
                if target == 'actor':
                    self.fixture['review_actor_kind'] = 'human'
                elif target == 'task':
                    self.fixture['task_version'] = 'unapproved-task'
                else:
                    self.record['document']['rubric'] = dict(self.rubric, title='another rubric')
                with self.assertRaises(ValueError):
                    self.inputs()


if __name__ == '__main__':
    unittest.main()
