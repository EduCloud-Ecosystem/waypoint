import contextlib, json, pathlib, sqlite3, sys, traceback
from unittest.mock import patch
SOURCE=pathlib.Path(__file__).parent/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh, fault
from research_workspace import sqlite_ownership as o, recovery as r
reports=[]

def record(name,fn):
    try: reports.append({'name':name,**fn()})
    except BaseException as e: reports.append({'name':name,'harness_error':repr(e),'traceback':traceback.format_exc()})

def rollback_masks_body():
    with fresh() as c:
        with fault('offline-freeze','rollback'):
            try:
                with r.frozen_db(c.root):
                    raise ValueError('ORIGINAL offline body failure')
            except BaseException as e:
                return {'observed_exception':repr(e),'notes':getattr(e,'__notes__',[]),'original_preserved':str(e)=='ORIGINAL offline body failure','held':o.held(c.root)}

def dispatch_rollback_masks_body():
    with fresh() as c:
        c.submit();run=c.store.claim()
        with fault('dispatch-reservation','rollback'):
            try:
                c.store.launch_run(run,lambda: (_ for _ in ()).throw(ValueError('ORIGINAL spawn failure')))
            except BaseException as e:
                return {'observed_exception':repr(e),'notes':getattr(e,'__notes__',[]),'original_preserved':str(e)=='ORIGINAL spawn failure','held':o.held(c.root)}

def swallowed_cursor_close():
    with fresh() as c:
        caught=None;returned=False
        with fault('online-read','query',secondary=True):
            try:
                with c.store.read() as db:
                    try:db.execute('SELECT payload FROM projects')
                    except OSError as e:caught=repr(e)
                    returned=True
            except BaseException as e:
                return {'observed_exception':repr(e),'caught':caught,'returned':False,'held':o.held(c.root)}
        return {'observed_exception':None,'caught':caught,'returned':returned,'held':o.held(c.root)}

def swallowed_cursor_close_only():
    with fresh() as c:
        native=sqlite3.connect
        class Cursor:
            def __init__(self,x):self.x=x;self.selected=False
            def __getattr__(self,k):return getattr(self.x,k)
            def execute(self,sql,*args):
                self.selected=sql=='SELECT payload FROM projects'
                return self.x.execute(sql,*args)
            def close(self):
                self.x.close()
                if self.selected:raise OSError('ORIGINAL cursor-close failure')
        class Connection:
            def __init__(self,x):self.x=x
            def __getattr__(self,k):return getattr(self.x,k)
            def cursor(self):return Cursor(self.x.cursor())
        caught=None
        with patch.object(o.sqlite3,'connect',side_effect=lambda *a,**k:Connection(native(*a,**k))):
            try:
                with c.store.read() as db:
                    try:db.execute('SELECT payload FROM projects')
                    except OSError as e:caught=repr(e)
                return {'observed_exception':None,'caught':caught,'scope_returned_success':True,'held':o.held(c.root)}
            except BaseException as e:
                return {'observed_exception':repr(e),'caught':caught,'scope_returned_success':False,'held':o.held(c.root)}

for name,fn in [('offline_rollback_original',rollback_masks_body),('dispatch_rollback_original',dispatch_rollback_masks_body),('swallowed_cursor_close',swallowed_cursor_close_only)]:record(name,fn)
pathlib.Path('prior-fix-results.json').write_text(json.dumps(reports,indent=2)+'\n')
print(json.dumps(reports,indent=2))
