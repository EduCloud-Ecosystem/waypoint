"""No services/browser/providers: operator contracts and derived fixture fences."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import info511_notebook as notebook


class NotebookContractTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.work=self.root/'work'; self.work.mkdir()
        self.turn=self.private('turn.json',{'source':'print(1)\n','question':'Which columns should I inspect?'})
        self.bob=self.private('bob.token','b'*64,raw=True)
        self.alice=self.private('alice.token','a'*64,raw=True)
        self.data={'version':1,'bob':{'origin':'http://127.0.0.1:5190','token_file':str(self.bob),
            'work_directory':str(self.work)},'alice':{'origin':'http://127.0.0.1:5191','token_file':str(self.alice)},
            'turn_file':str(self.turn)}
        self.contract=self.private('contract.json',self.data)
        self.fixture=json.loads((notebook.NOTEBOOK_OPERATOR/'notebook-reviewer-fixture.json').read_text())
    def private(self,name,value,raw=False):
        path=self.root/name
        path.write_text(value if raw else json.dumps(value)); path.chmod(0o600)
        return path
    def read(self,**kwargs):
        return notebook.read_contract(self.contract,'http://127.0.0.1:5190',**kwargs)
    def test_complete_private_contract_no_side_effects(self):
        before=set(self.root.iterdir())
        result=self.read()
        self.assertEqual(result['turn']['source'],'print(1)\n')
        self.assertEqual(result['contract_sha256'],hashlib.sha256(self.contract.read_bytes()).hexdigest())
        self.assertEqual(set(self.root.iterdir()),before)
    def test_strict_origins_keys_workspace_and_token_permissions(self):
        for mutation in ({'version':2},{'extra':True},{'bob':'not-an-object'},
                         {'alice':dict(self.data['alice'],origin=self.data['bob']['origin'])}):
            self.contract.write_text(json.dumps(dict(self.data,**mutation)))
            with self.subTest(mutation=mutation),self.assertRaises(notebook.NotebookFailure): self.read()
        self.contract.write_text(json.dumps(self.data))
        with self.assertRaises(notebook.NotebookFailure): self.read(device_only=True)
        self.bob.chmod(0o644)
        with self.assertRaises(notebook.NotebookFailure): self.read()
        self.bob.chmod(0o600)
        for value in ('http://localhost:5190','http://127.0.0.1:5190?token=x','https://127.0.0.1:5190','http://127.0.0.1:80'):
            with self.assertRaises(notebook.NotebookFailure): notebook.origin(value)
    def test_turn_only_source_question_and_source_bound(self):
        for turn in ({'source':'x'*8193,'question':'q'}, {'source':'x','message':'q'},
                     {'source':'','question':'q'}, {'source':'x','question':'q','metadata':{}}):
            self.turn.write_text(json.dumps(turn))
            with self.assertRaises(notebook.NotebookFailure): self.read()
    def test_explicit_derivative_required_and_fresh_path(self):
        with self.assertRaises(notebook.NotebookFailure): self.read(fixture=self.fixture)
        output=self.root/'derived.json'
        self.read(fixture=self.fixture,derivative=output)
        self.assertFalse(output.exists())
        output.write_text('retained-failure')
        with self.assertRaises(notebook.NotebookFailure): self.read(fixture=self.fixture,derivative=output)
        self.assertEqual(output.read_text(),'retained-failure')
    def test_old_no_ai_fixture_rejected_before_live_work_or_output(self):
        original=json.loads((notebook.NOTEBOOK_OPERATOR/'reviewer-fixture.json').read_text())
        output=self.root/'derived.json'
        for fixture in (original,{}, {'learners':{'bob':{'artifacts':None}}}):
            with self.subTest(fixture=fixture),self.assertRaisesRegex(notebook.NotebookFailure,
                    'reviewed_notebook_authored_fixture_required'):
                self.read(fixture=fixture,derivative=output)
            self.assertFalse(output.exists())
    def test_notebook_fixture_changes_only_reviewed_bob_authored_text_bindings(self):
        expected=json.loads((notebook.NOTEBOOK_OPERATOR/'reviewer-fixture.json').read_text())
        files=notebook.reviewed_authored_files()
        self.assertIn(b'\nDeclaration: ai_assisted\n',files['assistance.md'])
        self.assertNotIn(b'authored but not executed',files['explanation.md'])
        expected['learners']['bob']['artifacts'].update(notebook.manifest(files))
        expected['learners']['bob']['citations']['explanation-reproducibility']['quote']=files['explanation.md'].decode().splitlines()[0]
        self.assertEqual(self.fixture,expected)
    def test_saved_authored_text_is_checked_before_live_popup(self):
        files=notebook.reviewed_authored_files()
        with patch.object(notebook,'saved_files',return_value=files):
            notebook.require_saved_authored_files(self.work)
        old=(notebook.NOTEBOOK_OPERATOR/'attempts/bob/assistance.md').read_bytes()
        with patch.object(notebook,'saved_files',return_value=dict(files,**{'assistance.md':old})):
            with self.assertRaisesRegex(notebook.NotebookFailure,'reviewed_notebook_saved_authored_files_required'):
                notebook.require_saved_authored_files(self.work)
    def test_symlink_private_file_refused(self):
        alias=self.root/'alias'; alias.symlink_to(self.contract)
        with self.assertRaises(OSError): notebook.read_contract(alias,self.data['bob']['origin'])
    def test_derivative_changes_only_two_bob_artifact_digests_retains_judgments(self):
        files={name:('actual '+name).encode() for name in notebook.FILES}
        original={'schema':'CW1-reviewer-fixture-v1','task_version':'frozen','review_actor_kind':'automated-synthetic-fixture',
            'learners':{'alice':{'untouched':True},'bob':{'artifacts':notebook.manifest(files),
                'proposal_points':{'criterion':1},'review_points':{'criterion':2},
                'citations':{'criterion':{'path':'explanation.md','quote':'fixed'}}}}}
        before=copy.deepcopy(original)
        files['lab3-q4.ipynb']=b'actual executed notebook'
        files['tidy-inflation.csv']=b'actual kernel CSV'
        target=self.root/'derived.json'
        derived,provenance=notebook.derive_fixture(original,files,target,'c'*64,'d'*64)
        self.assertEqual(original,before)
        for key in ('proposal_points','review_points','citations'):
            self.assertEqual(derived['learners']['bob'][key],before['learners']['bob'][key])
        self.assertEqual(derived['learners']['alice'],before['learners']['alice'])
        self.assertEqual(provenance['original_fixture_sha256'],'d'*64)
        self.assertEqual(provenance['derived_fixture_sha256'],hashlib.sha256(target.read_bytes()).hexdigest())
        self.assertEqual(target.stat().st_mode&0o777,0o600)
        self.assertFalse(provenance['human_review_performed'])
        self.assertEqual(json.loads(target.read_text()),derived)
        with self.assertRaises(FileExistsError): notebook.derive_fixture(original,files,target,'c'*64,'d'*64)
        files['assistance.md']=b'unapproved drift'
        with self.assertRaises(notebook.NotebookFailure):
            notebook.derive_fixture(original,files,self.root/'forbidden.json','c'*64,'d'*64)
        self.assertFalse((self.root/'forbidden.json').exists())


if __name__=='__main__': unittest.main()
