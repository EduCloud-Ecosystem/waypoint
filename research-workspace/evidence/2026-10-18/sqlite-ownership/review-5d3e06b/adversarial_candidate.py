import json, pathlib, sqlite3, sys, subprocess, traceback
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parent
SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o

def scope_reuse():
    with fresh() as c:
        events=[]
        with patch.object(o,'_event',side_effect=lambda root,role,b:events.append([role,b])):
            owner=o.Owner(c.root,'online-read',guard=c.store.guard,readonly=True)
            with owner:pass
            try:
                with owner:
                    owner.execute('SELECT subject FROM users').fetchall()
            except BaseException as e:error=repr(e)
            else:error=None
        return {'exception':error,'opened':events.count(['online-read','opened']),'closed':events.count(['online-read','closed']), 'guard_depth':c.store.guard.lease.depth,'held':o.held(c.root),'active_after_exit':owner._active}

def recursive_proof_owner():
    with fresh() as c:
        events=[]
        with patch.object(o,'_event',side_effect=lambda root,role,b:events.append([role,b])):
            owner=o.Owner(c.root,'guard-proof',guard=c.store.guard,readonly=True)
            try:
                with owner:
                    with owner:pass
            except BaseException as e:error=repr(e)
            else:error=None
        return {'exception':error,'opened':events.count(['guard-proof','opened']),'closed':events.count(['guard-proof','closed']), 'guard_depth':c.store.guard.lease.depth,'held':o.held(c.root)}

def diagnostic_join():
    with fresh() as c:
        token=c.store.guard.begin('mutation');o.hold(c.root)
        with o.diagnostic(token):
            with o.Owner(c.root,'guard-proof',guard=c.store.guard,readonly=True) as db:
                rows=db.execute('SELECT u.subject FROM operation_authority a JOIN users u ON 1=1').fetchall()
        return {'held':o.held(c.root),'business_subjects_returned':[tuple(x) for x in rows]}

def role_write(role):
    with fresh() as c:
        sequence=c.store.guard.load()['sequence']
        with c.store.write(role=role) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE users SET active=0 WHERE subject='alice'")
            db.commit()
        with c.raw() as db:active=db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0]
        return {'role':role,'business_write_committed':active==0,'new_intent_created':c.store.guard.load()['sequence']!=sequence,'held':o.held(c.root)}

CASES={'scope_reuse':scope_reuse,'recursive_proof_owner':recursive_proof_owner,'diagnostic_join':diagnostic_join,'barrier_business_write':lambda:role_write('barrier'),'started_receipt_business_write':lambda:role_write('started-receipt')}
if len(sys.argv)>1:
    try:print(json.dumps({'name':sys.argv[1],**CASES[sys.argv[1]]()}))
    except BaseException as e:print(json.dumps({'name':sys.argv[1],'exception':repr(e),'traceback':traceback.format_exc()}))
else:
    results=[]
    for case in CASES:
        child=subprocess.run([sys.executable,str(pathlib.Path(__file__).resolve()),case],text=True,capture_output=True,timeout=30)
        results.append({'name':case,'returncode':child.returncode,'stdout':child.stdout,'stderr':child.stderr})
    (ROOT/'adversarial-results.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
