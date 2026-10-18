import json,pathlib,sqlite3,sys,threading
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
results=[]
for stage in ['write','commit']:
    with fresh() as c:
        native=sqlite3.connect;local=threading.local();counts={'commit':0,'write':0};injected=False
        def event(root,role,boundary):
            if boundary=='before-open':local.role=role
        class Cursor:
            def __init__(self,db):self.db=db
            def __getattr__(self,key):return getattr(self.db,key)
            def execute(self,sql,*args):
                nonlocal_unused=None
                global injected
                result=self.db.execute(sql,*args)
                if sql=="UPDATE users SET active=0 WHERE subject='alice'":
                    counts['write']+=1
                    if stage=='write' and not injected:injected=True;raise OSError('write acknowledgement lost')
                return result
        class Connection:
            def __init__(self,db):self.db=db
            def __getattr__(self,key):return getattr(self.db,key)
            def cursor(self):return Cursor(self.db.cursor())
            def commit(self):
                global injected
                counts['commit']+=1;self.db.commit()
                if stage=='commit' and not injected:injected=True;raise OSError('commit acknowledgement lost')
        def connect(*a,**k):
            db=native(*a,**k)
            return Connection(db) if getattr(local,'role',None)=='mutation' else db
        caught=None;scope_error=None
        with patch.object(o,'_event',side_effect=event),patch.object(o.sqlite3,'connect',side_effect=connect):
            try:
                with c.store.transaction(essential=True) as db:
                    try:
                        db.execute("UPDATE users SET active=0 WHERE subject='alice'")
                        if stage=='commit':db.commit()
                    except OSError as error:caught=str(error)
            except BaseException as error:scope_error=repr(error)
        with c.raw() as raw:active=raw.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0]
        results.append({'stage':stage,'caught':caught,'scope_exception':scope_error,'business_write_committed':active==0,'guard_state':c.store.operation_status()['state'],'held':o.held(c.root),'native_calls':counts})
(ROOT/'failed-effect-results.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
