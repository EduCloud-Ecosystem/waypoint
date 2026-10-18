import json,pathlib,sqlite3,sys,threading
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
with fresh() as c:
    native=sqlite3.connect;local=threading.local();caught=None;scope_error=None
    def event(root,role,boundary):
        if boundary=='before-open':local.role=role
    class Source:
        def __init__(self,db):self.db=db
        def __getattr__(self,key):return getattr(self.db,key)
        def backup(self,target):self.db.backup(target);raise OSError('backup acknowledgement lost')
    def connect(*a,**k):
        db=native(*a,**k)
        return Source(db) if getattr(local,'role',None)=='backup-source' else db
    target=c.root.parent/'backup.sqlite3'
    with patch.object(o,'_event',side_effect=event),patch.object(o.sqlite3,'connect',side_effect=connect):
        try:
            with c.store.write(role='offline-freeze') as freeze:
                freeze.execute('BEGIN IMMEDIATE')
                with o.Owner(c.root,'backup-source',guard=c.store.guard,readonly=True) as source:
                    with o.Owner(c.root,'backup-destination',guard=c.store.guard,path=target,initialize=True,validate=False) as dest:
                        try:source.backup_to(dest)
                        except OSError as error:caught=str(error)
        except BaseException as error:scope_error=repr(error)
    result={'caught':caught,'scope_exception':scope_error,'destination_exists':target.exists(),'held':o.held(c.root),'guard_depth':c.store.guard.lease.depth}
    (ROOT/'backup-ack-results.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
