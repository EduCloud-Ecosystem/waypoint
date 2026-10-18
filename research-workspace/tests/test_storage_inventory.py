"""Read-only observations, including deliberately inconsistent synthetic authority."""
from contextlib import closing
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from research_workspace import storage_inventory as s
from research_workspace.recipes import expected_result
from research_workspace.store import Store, canonical

IMAGES = json.loads((Path(__file__).resolve().parents[1] / "images.lock.json").read_text())
CSV = "group,value\nA,1\nA,2\n"


class StorageInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve() / "state"
        self.store = Store(self.root, create=True)
        (self.root / "config.json").write_text(canonical({"mode":"local-public-data","private_canary":"never-print-this-secret"}))
        (self.root / "server.lock").touch()
        self.store.add_user("owner")
        self.project = self.store.create_project("owner", {"name":"Synthetic retention control", "question":"Which files need review?",
            "success_criterion":"Failed and unknown work remains protected.", "ends_at":(dt.datetime.now(dt.timezone.utc)+dt.timedelta(days=1)).isoformat(),
            "budget_cpu_seconds":300}, Mock(), 600)
        self.run = self.store.submit(self.project["id"], "owner", {"recipe":"summary-python","condition":"baseline","csv":CSV}, IMAGES)
        self.run = self.store.claim()
        compute = {"reserved_cpu_seconds":30,"charged_cpu_seconds":30,"provenance":"estimated_upper_bound"}
        self.run.update(status="succeeded", cleanup_verified=True, compute={**compute,"unresolved_liability":False}, metrics=expected_result(CSV,"baseline"))
        self.store.finish(self.run, self.run["metrics"])
        self.store.close_project(self.project["id"], "owner")
        self.ledger = {"version":2,"owner":"0123456789abcdef0123456789abcdef","authority":{"admission":"active"},"projects":{self.project["id"]:{"project_id":self.project["id"],"budget_cpu_seconds":300}},"runs":{
            self.run["id"]:{"request_id":self.run["id"],"project_id":self.project["id"],"status":"succeeded",
                "cleanup_verified":True,"unresolved_liability":False,"compute":compute,"image":IMAGES['python'],
                "owner":"0123456789abcdef0123456789abcdef","admitted":True,"execution_started":True,"exit_code":0,"schema_version":1,
                "stdout":canonical(self.run["metrics"]),"stderr":"","error":""}}}
        (self.root / "broker").mkdir(); self.write_ledger()

    def tearDown(self):
        self.tmp.cleanup()

    def write_ledger(self):
        (self.root / "broker/ledger.json").write_text(canonical(self.ledger))

    def before(self):
        return {str(p.relative_to(self.root)):(p.stat().st_mode,p.stat().st_mtime_ns,p.read_bytes() if p.is_file() else None)
                for p in [self.root,*self.root.rglob('*')]}

    def test_complete_inventory_counts_directories_and_all_orphan_temp_bookkeeping_bytes(self):
        (self.root/'orphan').mkdir(); (self.root/'orphan/.temporary').write_bytes(b'12345')
        (self.root/'workspace.sqlite3-shm').write_bytes(b'bookkeeping')
        (self.root/'workspace.sqlite3-wal').write_bytes(b'newer-authority')
        before=self.before(); result=s.observe(self.root,retention=True)
        self.assertTrue(result['complete']);self.assertFalse(result['quota_enforced'])
        self.assertEqual(result['filesystem_entries'],len(before))
        self.assertEqual(result['logical_bytes'],sum(len(v[2]) for v in before.values() if v[2] is not None))
        self.assertEqual(result['review_candidate_files'],0)
        self.assertEqual(before,self.before())
        self.assertEqual(result['inventory_sha256'],hashlib.sha256(canonical(result['entries']).encode()).hexdigest())

    def test_known_closed_settled_artifacts_are_only_review_candidates_and_nothing_changes(self):
        (self.root/'runs/orphan.tmp').write_bytes(b'unknown')
        before=self.before()
        with patch('research_workspace.store.Store.connect',side_effect=AssertionError('source DB opened')), patch('research_workspace.runtime.Broker.call',side_effect=AssertionError('broker invoked')):
            result=s.observe(self.root,retention=True)
        self.assertTrue(result['complete']);self.assertEqual(result['review_candidate_files'],3)
        self.assertFalse(result['deletion_authorized'])
        for name in ('config.json','workspace.sqlite3','broker/ledger.json','server.lock','runs/orphan.tmp'):
            self.assertEqual(result['retention_plan'][name]['classification'],'protected')
        self.assertNotIn('never-print-this-secret',json.dumps(result))
        self.assertEqual(before,self.before())

    def test_missing_lock_is_not_created_and_prevents_classification(self):
        (self.root/'server.lock').unlink(); before=self.before()
        result=s.observe(self.root,retention=True)
        self.assertTrue(result['complete']);self.assertFalse(result['existing_service_lock_held'])
        self.assertEqual(result['review_candidate_files'],0);self.assertEqual(before,self.before())

    def test_busy_lock_refuses_without_mutation(self):
        before=self.before()
        with (self.root/'server.lock').open('r') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            result=s.observe(self.root,retention=True)
        self.assertFalse(result['complete']);self.assertEqual(result['review_candidate_files'],0)
        self.assertEqual(before,self.before())

    def test_replaced_lock_invalidates_observation(self):
        real=s.scan; calls=0
        def replacement(*args,**kwargs):
            nonlocal calls
            result=real(*args,**kwargs);calls+=1
            if calls==1:
                (self.root/'server.lock').unlink();(self.root/'server.lock').touch()
            return result
        with patch.object(s,'scan',side_effect=replacement):result=s.observe(self.root,retention=True)
        self.assertFalse(result['complete']);self.assertEqual(result['review_candidate_files'],0)

    def test_concurrent_authority_writer_invalidates_candidate_plan(self):
        real=s._authority
        def changed(*args):
            result=real(*args);self.store.revoke_user('owner');return result
        with patch.object(s,'_authority',side_effect=changed):result=s.observe(self.root,retention=True)
        self.assertFalse(result['complete']);self.assertEqual(result['review_candidate_files'],0)

    def test_nonempty_journal_and_wal_never_get_stale_base_classification(self):
        for name in ('workspace.sqlite3-wal','workspace.sqlite3-journal'):
            with self.subTest(name=name):
                path=self.root/name;path.write_bytes(b'unreconciled')
                before=self.before();result=s.observe(self.root,retention=True)
                self.assertTrue(result['complete']);self.assertEqual(result['review_candidate_files'],0)
                self.assertEqual(before,self.before());path.unlink()

    def test_malformed_unknown_or_held_authority_protects_everything(self):
        for change in ('invalid-ledger','held','unresolved','invalid-db','new-schema'):
            with self.subTest(change=change):
                db=(self.root/'workspace.sqlite3').read_bytes();config=(self.root/'config.json').read_bytes()
                if change=='invalid-ledger':(self.root/'broker/ledger.json').write_text('[]')
                if change=='held':(self.root/'config.json').write_text('{"mode":"recovery-held"}')
                if change=='unresolved':
                    self.ledger['runs'][self.run['id']]['unresolved_liability']=True;self.write_ledger()
                if change=='invalid-db':(self.root/'workspace.sqlite3').write_bytes(b'broken')
                if change=='new-schema':
                    with closing(self.store.connect()) as connection:connection.execute('CREATE VIEW unexpected AS SELECT 1')
                before=self.before();result=s.observe(self.root,retention=True)
                self.assertTrue(result['complete']);self.assertEqual(result['review_candidate_files'],0)
                self.assertEqual(before,self.before())
                (self.root/'workspace.sqlite3').write_bytes(db);(self.root/'config.json').write_bytes(config)
                self.ledger['runs'][self.run['id']]['unresolved_liability']=False;self.write_ledger()

    def test_mismatched_charge_result_or_input_is_protected(self):
        for name in ('result.json','input/data.csv'):
            path=self.root/'runs'/self.run['id']/name;content=path.read_bytes();mode=path.stat().st_mode
            path.chmod(0o600);path.write_bytes(b'mismatch')
            result=s.observe(self.root,retention=True);self.assertEqual(result['review_candidate_files'],0)
            path.write_bytes(content);path.chmod(mode)
        self.ledger['runs'][self.run['id']]['compute']['charged_cpu_seconds']=0;self.write_ledger()
        self.assertEqual(s.observe(self.root,retention=True)['review_candidate_files'],0)

    def test_failed_or_active_project_artifacts_are_protected(self):
        with self.store.transaction() as db:
            project=json.loads(db.execute('SELECT payload FROM projects').fetchone()[0]);project['status']='active'
            db.execute('UPDATE projects SET payload=?',(canonical(project),))
        self.assertEqual(s.observe(self.root,retention=True)['review_candidate_files'],0)
        with self.store.transaction() as db:
            project['status']='closed';db.execute('UPDATE projects SET payload=?',(canonical(project),))
            run=json.loads(db.execute('SELECT payload FROM runs').fetchone()[0]);run['status']='failed'
            db.execute("UPDATE runs SET status='failed',payload=?",(canonical(run),))
        self.ledger['runs'][self.run['id']]['status']='failed';self.write_ledger()
        self.assertEqual(s.observe(self.root,retention=True)['review_candidate_files'],0)

    def test_absent_deserialize_has_no_disk_fallback(self):
        fake=Mock();fake.deserialize.side_effect=AttributeError('not available')
        before=self.before()
        with patch.object(s.sqlite3,'connect',return_value=fake) as connect:result=s.observe(self.root,retention=True)
        connect.assert_called_once_with(':memory:');self.assertEqual(result['review_candidate_files'],0)
        self.assertEqual(before,self.before())

    def test_symbolic_hard_links_and_special_entries_refuse(self):
        path=self.root/'unexpected'
        for kind in ('symlink','hardlink','fifo'):
            with self.subTest(kind=kind):
                if kind=='symlink':path.symlink_to(self.root/'config.json')
                elif kind=='hardlink':os.link(self.root/'config.json',path)
                else:os.mkfifo(path)
                result=s.observe(self.root,retention=True)
                self.assertFalse(result['complete']);self.assertEqual(result['review_candidate_files'],0);path.unlink()

    def test_growing_file_is_refused(self):
        path=self.root/'growing';path.write_bytes(b'first');inode=path.stat().st_ino
        real=os.read;changed=False
        def grow(fd,size):
            nonlocal changed
            chunk=real(fd,size)
            if not changed and os.fstat(fd).st_ino==inode:
                changed=True
                with path.open('ab') as target:target.write(b'growth')
            return chunk
        with patch.object(s.os,'read',side_effect=grow):result=s.observe(self.root)
        self.assertFalse(result['complete']);self.assertGreaterEqual(result['observed_entries_lower_bound'],1)

    def test_scan_limits_return_lower_bounds_without_candidates(self):
        real=s.scan
        for bounds in ({'max_bytes':1},{'max_entries':1}):
            with patch.object(s,'scan',side_effect=lambda root,**kw:real(root,**bounds)):
                result=s.observe(self.root,retention=True)
            self.assertFalse(result['complete']);self.assertFalse(result['quota_enforced'])
            self.assertEqual(result['review_candidate_files'],0);self.assertGreaterEqual(result['observed_entries_lower_bound'],1)

    def test_cli_is_read_only_and_returns_nonzero_for_incomplete_inventory(self):
        before=self.before()
        result=subprocess.run([sys.executable,'-m','research_workspace','storage-status','--state',str(self.root)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertTrue(json.loads(result.stdout)['complete'])
        self.assertEqual(before,self.before())
        os.mkfifo(self.root/'fifo')
        result=subprocess.run([sys.executable,'-m','research_workspace','retention-plan','--state',str(self.root)],capture_output=True,text=True)
        self.assertEqual(result.returncode,1);self.assertFalse(json.loads(result.stdout)['complete'])

    def test_missing_constraints_and_duplicate_identities_are_unsupported(self):
        original=(self.root/'workspace.sqlite3').read_bytes()
        for table in ('projects','runs'):
            with self.subTest(table=table), closing(self.store.connect()) as db:
                db.execute('CREATE TABLE duplicate AS SELECT * FROM '+table)
                db.execute('INSERT INTO duplicate SELECT * FROM '+table)
                db.execute('DROP TABLE '+table)
                db.execute('ALTER TABLE duplicate RENAME TO '+table)
                db.commit()
            result=s.observe(self.root,retention=True)
            self.assertEqual(result['authority_classification'],'UNAVAILABLE')
            self.assertEqual(result['review_candidate_files'],0)
            (self.root/'workspace.sqlite3').write_bytes(original)

    def test_incomplete_receipts_outputs_and_project_authority_remain_protected(self):
        original=canonical(self.ledger)
        for case in ('missing-version','missing-stdout','wrong-version','output-disagrees','missing-projects','missing-project','budget-disagrees'):
            with self.subTest(case=case):
                self.ledger=json.loads(original);receipt=self.ledger['runs'][self.run['id']]
                if case=='missing-version':del receipt['schema_version']
                if case=='missing-stdout':del receipt['stdout']
                if case=='wrong-version':receipt['schema_version']=2
                if case=='output-disagrees':receipt['stdout']='{}'
                if case=='missing-projects':del self.ledger['projects']
                if case=='missing-project':self.ledger['projects']={}
                if case=='budget-disagrees':self.ledger['projects'][self.project['id']]['budget_cpu_seconds']=600
                self.write_ledger();before=self.before();result=s.observe(self.root,retention=True)
                self.assertTrue(result['complete']);self.assertEqual(result['review_candidate_files'],0)
                self.assertEqual(before,self.before())

    def test_external_broker_output_is_hash_checked_and_matches_accepted_result(self):
        receipt=self.ledger['runs'][self.run['id']]
        data=canonical({'stdout':receipt['stdout'],'stderr':''}).encode()
        digest=hashlib.sha256(data).hexdigest()
        path=self.root/'broker/outputs'/ (digest+'.json');path.parent.mkdir();path.write_bytes(data)
        receipt.update(stdout='',output_sha256=digest,output_bytes=len(data));self.write_ledger()
        before=self.before();result=s.observe(self.root,retention=True)
        self.assertEqual(result['review_candidate_files'],3);self.assertEqual(before,self.before())
        path.write_bytes(b'corrupt')
        self.assertEqual(s.observe(self.root,retention=True)['review_candidate_files'],0)
