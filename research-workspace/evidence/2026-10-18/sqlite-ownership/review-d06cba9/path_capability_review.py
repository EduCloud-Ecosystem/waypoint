import json,pathlib,shutil,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
with fresh() as c:
    clone=c.root.parent/'other-database';shutil.copytree(c.root,clone)
    with o.Owner(c.root,'online-read',guard=c.store.guard,readonly=True,path=clone/'workspace.sqlite3') as db:
        rows=db.execute('SELECT subject FROM users').fetchall()
        result={'guard_root_equals_owner_root':db.guard.root==db.root,'database_outside_owner_root':not db.path.is_relative_to(db.root),'business_rows_returned':[tuple(x) for x in rows]}
    (ROOT/'path-capability-results.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
