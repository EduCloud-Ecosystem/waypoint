# SPDX-License-Identifier: Apache-2.0
"""No browser, model or notebook execution: profile/data/receipt/fixture fences."""
import base64
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'classroom'), str(ROOT / 'rehearsal')]
from assignment_profiles import LEGACY, NHANES, get_profile
from info511_package import check_submission, compare_receipt
from info511_policy import template_files
from info511_review_fixture import review_inputs
import info511_notebook as notebook


class NhanesProfileTests(unittest.TestCase):
    def setUp(self):
        self.package = NHANES.package
        self.files = {name:(self.package / 'operator/attempts/alice' / name).read_bytes()
                      for name in NHANES.files}
        self.task = json.loads((self.package / 'task.json').read_bytes())

    def test_allowlist_and_default_do_not_accept_custom_profiles(self):
        self.assertIs(get_profile(), LEGACY)
        self.assertIs(get_profile('nhanes-v1'), NHANES)
        for value in ('../info511-nhanes-v1', None, {}, NHANES._replace(task=LEGACY.task)):
            with self.subTest(value=value), self.assertRaises(ValueError): get_profile(value)

    def test_source_hashes_selection_and_missingness_are_new_contract(self):
        tables = {}
        for entry in self.task['datasets']:
            raw = (self.package / entry['path']).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), entry['sha256'])
            tables[Path(entry['path']).name] = list(csv.DictReader(io.StringIO(raw.decode())))
        demo, bp = tables['DEMO_L_selected.csv'], tables['BPXO_L_selected.csv']
        self.assertEqual((len(demo), len(bp)), (64, 56))
        self.assertTrue(all(18 <= int(row['RIDAGEYR']) <= 79 for row in demo))
        self.assertEqual(len({row['SEQN'] for row in demo}), 64)
        self.assertEqual(len({row['SEQN'] for row in bp}), 56)
        self.assertEqual(len({row['SEQN'] for row in demo}-{row['SEQN'] for row in bp}), 8)
        rows = list(csv.DictReader(io.StringIO(self.files[NHANES.files[1]].decode())))
        self.assertEqual(len(rows), 192)
        self.assertEqual(len({(row['SEQN'],row['reading']) for row in rows}), 192)
        self.assertEqual(sum(row['systolic_mm_hg']=='' for row in rows), 58)
        source = {row['SEQN']:row for row in bp}
        ages = {row['SEQN']:row['RIDAGEYR'] for row in demo}
        for row in rows:
            present = source.get(row['SEQN'])
            self.assertEqual(row['RIDAGEYR'], ages[row['SEQN']])
            self.assertEqual(row['bpxo_match'], 'matched' if present else 'no_BPXO_record')
            self.assertEqual(row['systolic_mm_hg'], present['BPXOSY'+row['reading']] if present else '')
        self.assertEqual(hashlib.sha256(self.files[NHANES.files[1]]).hexdigest(),
            self.task['expected_output']['canonical_expected_output_sha256'])

    def test_completeness_is_profile_scoped_and_not_correctness(self):
        report = check_submission(self.files, profile=NHANES)
        self.assertEqual(report['state'], 'ready_for_preview_not_submitted')
        self.assertEqual(report['task_version'], NHANES.task)
        self.assertNotIn('score', report)
        self.assertEqual(check_submission(self.files)['state'], 'incomplete')
        starter = {name:(self.package / 'learner' / name).read_bytes() for name in NHANES.files}
        self.assertEqual(check_submission(starter,profile=NHANES)['state'], 'incomplete')
        malformed = dict(self.files)
        malformed[NHANES.files[1]] = b'SEQN,RIDAGEYR,reading,systolic_mm_hg,bpxo_match\nwrong,wrong,wrong,wrong,wrong\n'
        self.assertEqual(check_submission(malformed,profile=NHANES)['state'], 'ready_for_preview_not_submitted')
        malformed[NHANES.files[1]] = b'country,year,annual_inflation\na,1993,1\n'
        self.assertEqual(check_submission(malformed,profile=NHANES)['state'], 'incomplete')

    def test_receipt_checks_exact_profile_and_bytes(self):
        report = check_submission(self.files,profile=NHANES)
        receipt = {'submission_id':'bob', 'status':'committed','commit_revision':'a'*40,
            'changes':[dict(item,action='create') for item in report['files']]}
        self.assertEqual(compare_receipt(report,receipt,'bob',profile=NHANES)['state'],'receipt_bytes_match_only')
        self.assertEqual(compare_receipt(report,receipt,'bob')['state'],'not_verified')
        receipt['changes'][0]['sha256'] = '0'*64
        self.assertEqual(compare_receipt(report,receipt,'bob',profile=NHANES)['state'],'not_verified')

    def test_notebook_starter_has_no_solution_and_fixture_creation_executes_nothing(self):
        starter = json.loads((self.package / 'learner' / NHANES.files[0]).read_bytes())
        work = next(cell for cell in starter['cells'] if cell['id']=='learner-work')
        self.assertNotIn('.merge(', ''.join(work['source']))
        self.assertNotIn('.melt(', ''.join(work['source']))
        for path in [self.package / 'learner' / NHANES.files[0],
                     *sorted((self.package / 'operator/attempts').glob('*/'+NHANES.files[0]))]:
            for cell in json.loads(path.read_bytes())['cells']:
                if cell['cell_type']=='code':
                    self.assertIsNone(cell['execution_count']); self.assertEqual(cell['outputs'], [])
        source_contract = self.package / self.task['source_contract']['path']
        self.assertEqual(hashlib.sha256(source_contract.read_bytes()).hexdigest(),self.task['source_contract']['sha256'])

    def test_unbound_checker_blocks_policy_before_source_read(self):
        binding = json.loads((self.package / 'operator/checker-binding.json').read_bytes())
        # The source package starts unbound; after root pins it, explicitly
        # inject the unbound case so this refusal remains tested.
        import info511_policy
        read = info511_policy.read_json
        with patch.object(info511_policy,'read_json',side_effect=lambda path:dict(binding,sha256=None)
                          if path.name=='checker-binding.json' else read(path)):
            with self.assertRaisesRegex(ValueError,'not bound'):
                template_files(Path('/nonexistent-source'), 'sha256:'+'a'*64, profile=NHANES)

    def test_policy_stages_only_pinned_inputs_not_operator_answers(self):
        import info511_policy
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); checker = root/'checker.py'; checker.write_text('# pinned fake checker\n')
            binding = {'sha256':hashlib.sha256(checker.read_bytes()).hexdigest(),
                       'path_in_belay':'checker.py','source_files':{Path(e['path']).name:e['sha256'] for e in self.task['datasets']}}
            read = info511_policy.read_json
            with patch.object(info511_policy,'read_json',side_effect=lambda path:binding
                              if path.name=='checker-binding.json' else read(path)):
                staged = template_files(root,'sha256:'+'a'*64,profile=NHANES)
                self.assertEqual(set(staged),{'grading.json','grade_csv.py','nhanes_check.py','checker-binding.json',
                    'README.md','data/DEMO_L_selected.csv','data/BPXO_L_selected.csv'})
                grading=json.loads(staged['grading.json'])
                self.assertEqual(grading['tests'][0]['points'],4)
                self.assertEqual(grading['tests'][0]['name'],NHANES.correctness_test)
                self.assertEqual(grading['limits']['network'],'none')
                checker.write_text('# drift\n')
                with self.assertRaisesRegex(ValueError,'checker changed'):
                    template_files(root,'sha256:'+'a'*64,profile=NHANES)

    def test_review_inputs_bind_new_artifacts_citations_and_profile(self):
        fixture=json.loads((self.package/'operator/reviewer-fixture.json').read_bytes())
        rubric=json.loads((self.package/'operator/rubric.json').read_bytes())
        artifacts=[];changes=[]
        for name,raw in self.files.items():
            digest=hashlib.sha256(raw).hexdigest()
            segments=[{'location':f'cell:{i+1}','text':''.join(cell['source'])}
                for i,cell in enumerate(json.loads(raw)['cells'])] if name.endswith('.ipynb') else [
                {'location':f'line:{i+1}','text':line} for i,line in enumerate(raw.decode().split('\n'))]
            artifacts.append({'path':name,'size':len(raw),'sha256':digest,'original':base64.b64encode(raw).decode(),'segments':segments})
            changes.append({'path':name,'bytes':len(raw),'sha256':digest})
        receipt={'commit':'a'*40,'changes':changes}
        record={'status':'collected','revision':'a'*40,'document':{'revision':'a'*40,
            'input_digest':'b'*64,'rubric':rubric,'artifacts':artifacts}}
        proposal,review=review_inputs(record,receipt,fixture,'alice',rubric,profile=NHANES)
        self.assertEqual(sum(c['points'] for c in proposal['criteria']),5)
        self.assertEqual(sum(c['points'] for c in review['criteria']),6)
        with self.assertRaises(ValueError):review_inputs(record,receipt,fixture,'alice',rubric)
        fixture['learners']['alice']['artifacts'][NHANES.files[1]]['sha256']='0'*64
        with self.assertRaises(ValueError):review_inputs(record,receipt,fixture,'alice',rubric,profile=NHANES)

    def test_native_derivative_rebinds_only_new_profile_notebook_and_csv(self):
        original=json.loads((self.package/'operator/notebook-reviewer-fixture.json').read_bytes())
        notebook.require_notebook_fixture(original,profile=NHANES)
        files={name:(self.package/'operator/attempts/bob'/name).read_bytes() for name in NHANES.files}
        files.update(notebook.reviewed_authored_files(profile=NHANES))
        files[NHANES.files[0]]+=b'\n'; files[NHANES.files[1]]+=b'\n'
        before=copy.deepcopy(original)
        with tempfile.TemporaryDirectory() as directory:
            derived,provenance=notebook.derive_fixture(original,files,Path(directory)/'derived.json','c'*64,'d'*64,profile=NHANES)
        self.assertEqual(original,before)
        self.assertEqual(derived['learners']['alice'],before['learners']['alice'])
        for key in ('proposal_points','review_points','citations'):
            self.assertEqual(derived['learners']['bob'][key],before['learners']['bob'][key])
        self.assertEqual(provenance['changed_fields'],['learners.bob.artifacts.'+name for name in NHANES.files[:2]])
        original['task_version']=LEGACY.task
        with self.assertRaises(notebook.NotebookFailure):notebook.require_notebook_fixture(original,profile=NHANES)

    def test_native_turn_is_exact_profile_bound_before_any_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();work=root/'work';work.mkdir()
            def private(name,value):
                path=root/name;path.write_text(value);path.chmod(0o600);return str(path)
            approved=(self.package/'operator/approved-notebook-turn.json').read_text()
            turn=private('turn.json',approved)
            contract={'version':1,'bob':{'origin':'http://127.0.0.1:8189',
                'token_file':private('bob.token','b'*64),'work_directory':str(work)},
                'alice':{'origin':'http://127.0.0.1:8190','token_file':private('alice.token','a'*64)},
                'turn_file':turn}
            path=Path(private('contract.json',json.dumps(contract)))
            result=notebook.read_contract(path,contract['bob']['origin'],profile=NHANES)
            self.assertEqual(result['turn'],json.loads(approved))
            changed=json.loads(approved);changed['source']+='\n# unapproved edit\n'
            Path(turn).write_text(json.dumps(changed))
            with self.assertRaisesRegex(notebook.NotebookFailure,'turn_profile_mismatch'):
                notebook.read_contract(path,contract['bob']['origin'],profile=NHANES)


if __name__=='__main__': unittest.main()
