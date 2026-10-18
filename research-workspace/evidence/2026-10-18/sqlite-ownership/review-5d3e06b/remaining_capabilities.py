import json,pathlib,sqlite3,sys,subprocess,threading,shutil
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o

def read_rollback():
    with fresh() as c:
        with c.store.read() as db:
            db.rollback()
            with c.raw() as raw:raw.execute('PRAGMA user_version=0');raw.commit()
            rows=db.execute('SELECT subject FROM users').fetchall()
            version=db.execute('PRAGMA user_version').fetchone()[0]
        return {'invalid_version':version,'business_rows_returned':[tuple(x) for x in rows],'held':o.held(c.root)}

def disabled_validation():
    with fresh() as c:
        with c.raw() as raw:raw.execute('PRAGMA user_version=0');raw.commit()
        with o.Owner(c.root,'online-read',guard=c.store.guard,readonly=True,validate=False) as db:
            rows=db.execute('SELECT subject FROM users').fetchall()
        return {'business_rows_returned':[tuple(x) for x in rows],'held':o.held(c.root)}

def mismatched_guard():
    with fresh() as c:
        clone=c.root.parent/'other-root';shutil.copytree(c.root,clone)
        with o.Owner(clone,'online-read',guard=c.store.guard,readonly=True) as db:
            rows=db.execute('SELECT subject FROM users').fetchall()
            roots={'database_root':str(db.root),'guard_root':str(db.guard.root)}
        return {**roots,'business_rows_returned':[tuple(x) for x in rows]}

def diagnostic_retry():
    with fresh() as c:
        native=sqlite3.connect;local=threading.local();proof_opens=0
        token=c.store.guard.begin('mutation');o.hold(c.root)
        def event(root,role,boundary):
            nonlocal proof_opens
            if boundary=='before-open':
                local.role=role
                if role=='guard-proof':proof_opens+=1
        def connect(*a,**k):
            if getattr(local,'role',None)=='guard-proof' and proof_opens==1:raise OSError('first diagnostic open failed')
            return native(*a,**k)
        first_error=None
        with patch.object(o,'_event',side_effect=event),patch.object(o.sqlite3,'connect',side_effect=connect):
            with o.diagnostic(token):
                try:
                    with o.Owner(c.root,'guard-proof',guard=c.store.guard,readonly=True):pass
                except OSError as e:first_error=str(e)
                with o.Owner(c.root,'guard-proof',guard=c.store.guard,readonly=True) as db:
                    rows=db.execute('SELECT authority_id,sequence FROM operation_authority').fetchall()
        return {'first_error':first_error,'proof_open_attempts':proof_opens,'second_attempt_returned_rows':len(rows),'token_allowance_consumed':token._ownership_diagnostic_used,'held':o.held(c.root)}

CASES={'readonly_rollback':read_rollback,'disabled_validation':disabled_validation,'mismatched_guard':mismatched_guard,'diagnostic_retry':diagnostic_retry}
if len(sys.argv)>1:
    try:result={'name':sys.argv[1],**CASES[sys.argv[1]]()}
    except BaseException as e:result={'name':sys.argv[1],'exception':repr(e)}
    print(json.dumps(result))
else:
    results=[]
    for name in CASES:
        child=subprocess.run([sys.executable,str(pathlib.Path(__file__).resolve()),name],capture_output=True,text=True,timeout=30)
        results.append({'name':name,'returncode':child.returncode,'stdout':child.stdout,'stderr':child.stderr})
    (ROOT/'remaining-capabilities-results.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
