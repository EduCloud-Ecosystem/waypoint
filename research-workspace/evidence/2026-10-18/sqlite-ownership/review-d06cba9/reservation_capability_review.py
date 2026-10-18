import json,pathlib,sqlite3,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
results=[]
code="import sqlite3,sys; db=sqlite3.connect(sys.argv[1],timeout=.1);\ntry:\n db.execute('BEGIN IMMEDIATE');print('reservation-acquired')\nexcept sqlite3.OperationalError: print('reservation-blocked')\nfinally: db.rollback();db.close()"
for boundary in ['valid-immediate','before-begin','after-commit','after-rollback','deferred-begin']:
    with fresh() as c:
        with c.store.write(role='offline-freeze') as freeze:
            if boundary in {'valid-immediate','after-commit','after-rollback'}:freeze.execute('BEGIN IMMEDIATE')
            elif boundary=='deferred-begin':freeze.execute('BEGIN')
            if boundary=='after-commit':freeze.commit()
            if boundary=='after-rollback':freeze.rollback()
            child=subprocess.run([sys.executable,'-c',code,str(c.root/'workspace.sqlite3')],capture_output=True,text=True,timeout=5)
            try:
                with o.Owner(c.root,'backup-source',guard=c.store.guard,readonly=True):pass
            except BaseException as error:backup_error=repr(error)
            else:backup_error=None
            results.append({'boundary':boundary,'raw_other_process':child.stdout.strip(),'backup_source_admitted':backup_error is None,'backup_error':backup_error})
(ROOT/'reservation-capability-results.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
