"""Independent assertions against a disposable source export, never the live checkout.
Usage: python3 adversarial_controls.py [--source PATH] [TestClass.test_name ...]
Results are standard unittest output; run_review.py retains logs and JSON.
"""
from pathlib import Path
import argparse, contextlib, json, os, sqlite3, stat, subprocess, sys, unittest
from unittest.mock import patch
args=list(sys.argv[1:])
base=Path(__file__).resolve().parent
source=base/'checkout/research-workspace'
if '--source' in args:
    i=args.index('--source'); source=Path(args[i+1]).resolve(); del args[i:i+2]
sys.path[:0]=[str(source),str(source/'tests')]
os.chdir(source)
os.environ['PYTHONPATH']=str(source)+os.pathsep+str(source/'tests')
os.environ['PYTHONDONTWRITEBYTECODE']='1'
from test_guard_publication import fresh
from research_workspace import guard_publication as p, operation_guard as g
from research_workspace.store import Store, AccessError

class IndependentControls(unittest.TestCase):
    def test_cached_B_partial_A_ordinary_residue_preserves_safety_effect(self):
        with fresh() as c:
            before=(c.root/g.FILE).read_bytes(); cached=c.store.guard.lease.durable_incarnation
            result=c.child('''
import json,sys,os
from unittest.mock import patch
from research_workspace.store import Store
from research_workspace import guard_publication as p
s=Store(sys.argv[1]);write=os.write
seen=[]
def fail(fd,data):
    if not seen: seen.append(True);return write(fd,data[:17])
    raise OSError('partial candidate')
try:
    with patch.object(p.os,'write',side_effect=fail):s.add_user('A')
except OSError:print(json.dumps({'failed':True}))
''')
            self.assertTrue(result['failed']); self.assertEqual((c.root/g.FILE).read_bytes(),before)
            self.assertEqual(c.store.guard.lease.durable_incarnation,cached)
            residue=(c.root/p.ORDINARY).read_bytes();self.assertEqual(len(residue),17)
            with self.assertRaises(g.GuardPaused):c.store.add_user('B')
            c.store.logout(c.token)
            with self.assertRaises(AccessError):c.store.session(token=c.token)
            data=c.store.guard.load(); oid=next(k for k,v in data['operations'].items() if v['essential'])
            self.assertEqual(c.store.guard.proof(data)[oid][2],'committed')
            self.assertEqual((c.root/p.ORDINARY).read_bytes(),residue)

    def test_cached_B_partial_A_safety_residue_refuses_both_roles(self):
        with fresh() as c:
            before=(c.root/g.FILE).read_bytes();cached=c.store.guard.lease.durable_incarnation
            c.child('''
import json,sys,os
from unittest.mock import patch
from research_workspace.store import Store
from research_workspace import guard_publication as p
s=Store(sys.argv[1]);write=os.write
seen=[]
def fail(fd,data):
    if not seen:seen.append(True);return write(fd,data[:13])
    raise OSError('partial safety')
try:
    with patch.object(p.os,'write',side_effect=fail):s.revoke_user('alice')
except OSError:print(json.dumps(True))
''')
            self.assertEqual((c.root/g.FILE).read_bytes(),before);self.assertEqual(c.store.guard.lease.durable_incarnation,cached)
            residue=(c.root/p.SAFETY).read_bytes();self.assertEqual(len(residue),13)
            before_db=c.database()
            for action in (lambda:c.store.logout(c.token),lambda:c.store.add_user('B')):
                with self.assertRaises(g.GuardPaused):action()
            self.assertEqual(c.database(),before_db)
            self.assertEqual(c.store.session(token=c.token),'alice')
            self.assertEqual((c.root/p.SAFETY).read_bytes(),residue)

    def test_residue_arriving_after_business_statement_blocks_commit(self):
        with fresh() as c:
            with self.assertRaises(g.GuardPaused):
                with c.store.transaction() as db:
                    db.execute("UPDATE users SET active=0 WHERE subject='alice'")
                    (c.root/p.SAFETY).write_bytes(b'retain at commit')
            with contextlib.closing(c.store.connect()) as db:
                self.assertEqual(db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0],1)
            self.assertEqual((c.root/p.SAFETY).read_bytes(),b'retain at commit')

    def test_missing_receipt_at_same_incarnation_blocks_safety(self):
        with fresh() as c:
            before=(c.root/g.FILE).read_bytes()
            with contextlib.closing(sqlite3.connect(c.root/'workspace.sqlite3')) as db:
                db.execute('DELETE FROM operation_receipts WHERE sequence=(SELECT min(sequence) FROM operation_receipts)');db.commit()
            with self.assertRaises(g.GuardPaused):c.store.logout(c.token)
            self.assertEqual((c.root/g.FILE).read_bytes(),before)
            self.assertEqual(c.store.session(token=c.token),'alice')

    def test_uncertain_essential_terminal_receipt_does_not_grant_fresh_safety(self):
        with fresh() as c:
            token=c.store.guard.begin('mutation',essential=True);token.effect();token.fail()
            with contextlib.closing(sqlite3.connect(c.root/'workspace.sqlite3')) as db:
                db.execute("UPDATE operation_receipts SET outcome='committed' WHERE id=?",(token.identity,));db.commit()
            before=(c.root/g.FILE).read_bytes(); before_db=c.database()
            with self.assertRaises(g.GuardPaused):c.store.logout(c.token)
            self.assertEqual((c.root/g.FILE).read_bytes(),before);self.assertEqual(c.database(),before_db)
            self.assertEqual(c.store.session(token=c.token),'alice')

    def test_takeover_performs_real_barrier_before_business(self):
        with fresh() as c:
            c.child("import json,sys;from research_workspace.store import Store;s=Store(sys.argv[1]);s.logout('absent');print(json.dumps(True))")
            with contextlib.closing(sqlite3.connect(c.root/'workspace.sqlite3')) as db:
                old=db.execute('SELECT barrier FROM operation_authority').fetchone()[0]
            events=[];connect=c.store.guard.connect;business=c.store._connect
            class Probe:
                def __init__(self,db,label):self.db,self.label=db,label;self.barrier=False
                def __getattr__(self,k):return getattr(self.db,k)
                def execute(self,sql,*a):
                    if sql.startswith('UPDATE operation_authority SET barrier='):self.barrier=True;events.append('barrier-write')
                    if self.label=='business' and sql=='BEGIN IMMEDIATE':events.append('business-begin')
                    return self.db.execute(sql,*a)
                def commit(self):
                    result=self.db.commit()
                    if self.barrier:events.append('barrier-commit')
                    return result
                def close(self):
                    result=self.db.close()
                    if self.barrier:events.append('barrier-close')
                    return result
            with patch.object(c.store.guard,'connect',side_effect=lambda:Probe(connect(),'guard')),patch.object(c.store,'_connect',side_effect=lambda:Probe(business(),'business')):
                c.store.add_user('takeover')
            self.assertEqual(events,['barrier-write','barrier-commit','barrier-close','business-begin'])
            with contextlib.closing(sqlite3.connect(c.root/'workspace.sqlite3')) as db:
                self.assertNotEqual(db.execute('SELECT barrier FROM operation_authority').fetchone()[0],old)

    def test_barrier_close_error_holds_process_without_business_effect(self):
        with fresh() as c:
            c.child("import json,sys;from research_workspace.store import Store;s=Store(sys.argv[1]);s.logout('absent');print(json.dumps(True))")
            connect=c.store.guard.connect;closed=[]
            class Probe:
                def __init__(self,db):self.db=db
                def __getattr__(self,k):return getattr(self.db,k)
                def close(self):self.db.close();closed.append(True);raise OSError('barrier close uncertain')
            with patch.object(c.store.guard,'connect',side_effect=lambda:Probe(connect())):
                with self.assertRaisesRegex(OSError,'barrier close uncertain'):c.store.add_user('must-not-exist')
            self.assertEqual(closed,[True]);self.assertTrue(c.store.guard.lease.publication_failed)
            with self.assertRaises(g.GuardPaused):c.store.logout(c.token)
            with contextlib.closing(c.store.connect()) as db:
                self.assertIsNone(db.execute("SELECT subject FROM users WHERE subject='must-not-exist'").fetchone())

    def test_settlement_uses_fresh_safety_authority_despite_ordinary_residue(self):
        with fresh() as c:
            run=c.settled();run_token=c.store.guard.running(run['id'])
            (c.root/p.ORDINARY).write_bytes(b'ordinary residue retained')
            c.store.finish(run,run['metrics'],claimed=True)
            data=c.store.guard.load();proof=c.store.guard.proof(data)
            self.assertEqual(proof[run_token.identity][2],'committed')
            safety=next(k for k,v in data['operations'].items() if v['essential'])
            self.assertEqual(proof[safety][2],'committed')
            self.assertEqual(c.store.run(c.pid,run['id'],'alice')['status'],'succeeded')
            self.assertEqual((c.root/p.ORDINARY).read_bytes(),b'ordinary residue retained')

    def test_mixed_v3_binding_refuses_reopen_live_safety_and_export(self):
        for mutation in ('db','config','guard'):
            with self.subTest(mutation=mutation),fresh() as c:
                if mutation=='db':
                    with contextlib.closing(sqlite3.connect(c.root/'workspace.sqlite3')) as db:db.execute('PRAGMA user_version=1162232114');db.commit()
                elif mutation=='config':
                    path=c.root/'config.json';data=json.loads(path.read_bytes());data['logical_authority']['guard_publication']['residue']='delete';path.write_text(json.dumps(data))
                else:
                    path=c.root/g.FILE;data=json.loads(path.read_bytes());data['format']='educloud-operation-guard-v3';path.write_text(json.dumps(data))
                with self.assertRaises((ValueError,g.GuardPaused)):Store(c.root)
                with self.assertRaises((ValueError,g.GuardPaused)):c.store.logout(c.token)
                with self.assertRaises((ValueError,g.GuardPaused)):c.export()

if __name__=='__main__':unittest.main(argv=[sys.argv[0],*args],verbosity=2)
