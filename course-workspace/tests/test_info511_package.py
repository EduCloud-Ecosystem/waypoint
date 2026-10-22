# SPDX-License-Identifier: Apache-2.0
"""Hermetic completeness/receipt controls. No services, notebooks or models."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cw1_info511', ROOT / 'classroom/info511_package.py')
CW1 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CW1)
PACKAGE = ROOT / 'classroom/info511-lab03-q4-v1'


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.files = {name: (PACKAGE / 'operator/attempts/alice' / name).read_bytes()
                      for name in CW1.FILES}

    def receipt(self):
        return {'id': 'synthetic-receipt', 'submission_id': 'alice-submission',
                'status': 'committed', 'commit_revision': 'a' * 40,
                'changes': [{'path': name, 'bytes': len(raw),
                             'sha256': hashlib.sha256(raw).hexdigest(), 'action': 'create'}
                            for name, raw in self.files.items()]}

    def test_no_ai_fixture_can_be_ready_but_is_never_submitted_or_graded(self):
        result = CW1.check_submission(self.files)
        self.assertEqual(result['state'], 'ready_for_preview_not_submitted')
        self.assertEqual(result['reminders'], [])
        self.assertNotIn('score', result)
        self.assertNotIn('commit_revision', result)
        self.assertNotIn('content', json.dumps(result))

    def test_learner_starter_is_incomplete(self):
        files = {name: (PACKAGE / 'learner' / name).read_bytes() for name in CW1.FILES}
        result = CW1.check_submission(files)
        self.assertEqual(result['state'], 'incomplete')
        self.assertEqual(len(result['reminders']), 5)

    def test_missing_or_extra_selection_is_not_a_submission(self):
        for files in [{k: v for k, v in self.files.items() if k != 'assistance.md'},
                      dict(self.files, **{'private-monitoring.json': b'{}'})]:
            with self.subTest(files=list(files)):
                self.assertEqual(CW1.check_submission(files)['state'], 'incomplete')

    def test_finite_bytes_and_text_refusals(self):
        for name, value in [('explanation.md', b'x' * (CW1.MAX_FILE_BYTES + 1)),
                            ('explanation.md', b'\xff'), ('assistance.md', b'\xff'),
                            ('tidy-inflation.csv', b'\xff')]:
            with self.subTest(name=name, bytes=len(value)):
                self.assertEqual(CW1.check_submission(dict(self.files, **{name: value}))['state'], 'incomplete')

    def test_prediction_presence_and_version_are_not_inferred(self):
        notebook = json.loads(self.files['lab3-q4.ipynb'])
        for mutation in ['missing', 'duplicate', 'blank', 'wrong_version']:
            candidate = copy.deepcopy(notebook)
            if mutation == 'missing':
                candidate['cells'] = [c for c in candidate['cells'] if c['id'] != 'schema-prediction']
            elif mutation == 'duplicate':
                candidate['cells'].append(copy.deepcopy(next(c for c in candidate['cells'] if c['id'] == 'schema-prediction')))
            elif mutation == 'blank':
                next(c for c in candidate['cells'] if c['id'] == 'schema-prediction')['source'] = [' ']
            else:
                candidate['metadata']['cw1_task_version'] = 'wrong-version'
            with self.subTest(mutation=mutation):
                files = dict(self.files, **{'lab3-q4.ipynb': json.dumps(candidate).encode()})
                self.assertEqual(CW1.check_submission(files)['state'], 'incomplete')

    def test_notebook_is_parsed_without_executing_code(self):
        notebook = json.loads(self.files['lab3-q4.ipynb'])
        notebook['cells'].append({'cell_type': 'code', 'id': 'never-execute', 'metadata': {},
                                  'source': ['raise RuntimeError("MUST NOT EXECUTE")'],
                                  'execution_count': None, 'outputs': []})
        self.files['lab3-q4.ipynb'] = json.dumps(notebook).encode()
        self.assertEqual(CW1.check_submission(self.files)['state'], 'ready_for_preview_not_submitted')

    def test_header_only_or_wrong_width_csv_needs_a_reminder(self):
        for raw in [b'country,year,annual_inflation\n', b'country,year,value\na,1993,1\n',
                    b'country,year,annual_inflation\na,1993,1,extra\n']:
            self.assertEqual(CW1.check_submission(dict(self.files, **{'tidy-inflation.csv': raw}))['state'], 'incomplete')

    def test_completeness_does_not_claim_scientific_correctness(self):
        self.files['tidy-inflation.csv'] = b'country,year,annual_inflation\nwrong,wrong,wrong\n'
        self.assertEqual(CW1.check_submission(self.files)['state'], 'ready_for_preview_not_submitted')
        # B1's separately qualified checker/instructor review decides values.

    def test_explanation_limit_and_declared_assistance(self):
        self.files['explanation.md'] = ('word ' * 150).encode()
        self.assertEqual(CW1.check_submission(self.files)['state'], 'ready_for_preview_not_submitted')
        self.files['explanation.md'] += b'extra'
        self.assertEqual(CW1.check_submission(self.files)['state'], 'incomplete')
        self.files['explanation.md'] = b'Learner written explanation.'
        self.files['assistance.md'] = ('Task version: ' + CW1.TASK + '\nDeclaration: ai_assisted\nSummary: Documentation hints only.\n').encode()
        self.assertEqual(CW1.check_submission(self.files)['state'], 'ready_for_preview_not_submitted')
        self.files['assistance.md'] = self.files['assistance.md'].replace(b'ai_assisted', b'missing')
        self.assertEqual(CW1.check_submission(self.files)['state'], 'incomplete')

    def test_receipt_matches_bytes_but_cannot_authenticate(self):
        receipt = self.receipt()
        receipt['changes'].reverse()
        receipt['changes'][0]['action'] = 'replace'
        result = CW1.compare_receipt(CW1.check_submission(self.files), receipt, 'alice-submission')
        self.assertEqual(result['state'], 'receipt_bytes_match_only')
        self.assertIs(result['authenticated_here'], False)

    def test_preview_pending_unknown_or_peer_receipt_never_verified(self):
        report = CW1.check_submission(self.files)
        for state in ['preview', 'queued', 'publishing', 'verification_needed', 'failed', 'conflict', 'expired']:
            receipt = self.receipt(); receipt['status'] = state
            self.assertEqual(CW1.compare_receipt(report, receipt, 'alice-submission')['state'], 'not_verified')
        self.assertEqual(CW1.compare_receipt(report, self.receipt(), 'bob-submission')['state'], 'not_verified')

    def test_modified_missing_duplicate_receipt_files_refused(self):
        report = CW1.check_submission(self.files)
        for mutation in ['digest', 'missing', 'duplicate', 'size', 'revision']:
            receipt = self.receipt()
            if mutation == 'digest': receipt['changes'][0]['sha256'] = '0' * 64
            elif mutation == 'missing': receipt['changes'].pop()
            elif mutation == 'duplicate': receipt['changes'][0] = receipt['changes'][1]
            elif mutation == 'size': receipt['changes'][0]['bytes'] = True
            else: receipt['commit_revision'] = 'not-a-git-revision'
            with self.subTest(mutation=mutation):
                self.assertEqual(CW1.compare_receipt(report, receipt, 'alice-submission')['state'], 'not_verified')

    def test_synthetic_fixture_and_environment_bindings(self):
        task = json.loads((PACKAGE / 'task.json').read_text())
        self.assertEqual(task['dataset']['sha256'], hashlib.sha256((PACKAGE / task['dataset']['path']).read_bytes()).hexdigest())
        for file_key, hash_key in [('reuse', 'dockerfile_sha256'), ('requirements', 'requirements_sha256')]:
            self.assertEqual(task['environment'][hash_key], hashlib.sha256((ROOT.parent / task['environment'][file_key]).read_bytes()).hexdigest())
        self.assertIsNone(task['environment']['executed_image_id'])
        self.assertIs(task['binding']['runtime_calls_enabled'], False)


if __name__ == '__main__':
    unittest.main()
