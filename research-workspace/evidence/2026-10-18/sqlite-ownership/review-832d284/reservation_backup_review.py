import json,pathlib,sqlite3,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
results=[]
code="import sqlite3,sys; db=sqlite3.connect(sys.argv[1],timeout=.1);\ntry:\n db.execute('BEGIN IMMEDIATE');print('reservation-acquired')\nexcept sqlite3.OperationalError: print('reservation-blocked')\nfinally: db.rollback();db.close()"
with fresh() as c:
    try:
        with o.Owner(c.root,'backup-source',guard=c.store.guard,readonly=True):pass
    except BaseException as error:failure=repr(error)
    else:failure=None
    results.append({'case':'standalone-source','admitted':failure is None,'exception':failure})
with fresh() as c:
    output=c.root/'standalone-backup.sqlite3'
    try:
        with o.Owner(c.root,'backup-destination',guard=c.store.guard,path=output,initialize=True,validate=False):pass
    except BaseException as error:failure=repr(error)
    else:failure=None
    results.append({'case':'standalone-destination','admitted':failure is None,'file_created':output.exists(),'exception':failure})
with fresh() as c:
    output=c.root/'ended-freeze-backup.sqlite3'
    with c.store.write(role='offline-freeze') as freeze:
        freeze.execute('BEGIN IMMEDIATE')
        with o.Owner(c.root,'backup-source',guard=c.store.guard,readonly=True) as source:
            with o.Owner(c.root,'backup-destination',guard=c.store.guard,path=output,initialize=True,validate=False) as destination:
                freeze.rollback()
                child=subprocess.run([sys.executable,'-c',code,str(c.root/'workspace.sqlite3')],capture_output=True,text=True,timeout=5)
                try:source.backup_to(destination)
                except BaseException as error:failure=repr(error)
                else:failure=None
                results.append({'case':'backup-after-parent-rollback','backup_succeeded':failure is None,'raw_other_process':child.stdout.strip(),'destination_size':output.stat().st_size,'exception':failure})
(ROOT/'reservation-backup-results.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
