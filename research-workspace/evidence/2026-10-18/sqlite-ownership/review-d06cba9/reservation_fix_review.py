import json,pathlib,subprocess,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace';sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
results=[]
code="import sqlite3,sys; db=sqlite3.connect(sys.argv[1],timeout=.1);\ntry:\n db.execute('BEGIN IMMEDIATE');print('reservation-acquired')\nexcept sqlite3.OperationalError: print('reservation-blocked')\nfinally: db.rollback();db.close()"
def contend(c):
 p=subprocess.run([sys.executable,'-c',code,str(c.root/'workspace.sqlite3')],capture_output=True,text=True,timeout=5)
 assert p.returncode==0,p.stderr
 return p.stdout.strip()
for boundary in ['valid-immediate','before-begin','after-commit','after-rollback','deferred-begin']:
 with fresh() as c:
  with c.store.write(role='offline-freeze') as freeze:
   setup_error=None
   try:
    if boundary in {'valid-immediate','after-commit','after-rollback'}:freeze.execute('BEGIN IMMEDIATE')
    elif boundary=='deferred-begin':freeze.execute('BEGIN')
    if boundary=='after-commit':freeze.commit()
    if boundary=='after-rollback':freeze.rollback()
   except BaseException as error:setup_error=repr(error)
   try:
    with o.Owner(c.root,'backup-source',guard=c.store.guard,readonly=True):pass
   except BaseException as error:backup_error=repr(error)
   else:backup_error=None
   results.append({'boundary':boundary,'setup_error':setup_error,'raw_other_process':contend(c),'backup_source_admitted':backup_error is None,'backup_error':backup_error})
for role in ['backup-source','backup-destination']:
 with fresh() as c:
  output=c.root/'standalone-backup.sqlite3'
  try:
   kwargs={'readonly':True} if role=='backup-source' else {'path':output,'initialize':True,'validate':False}
   with o.Owner(c.root,role,guard=c.store.guard,**kwargs):pass
  except BaseException as error:failure=repr(error)
  else:failure=None
  results.append({'boundary':'standalone-'+role,'error':failure,'destination_created':output.exists()})
for action in ['rollback','exit']:
 with fresh() as c:
  output=c.root/'retained-freeze-backup.sqlite3'
  with c.store.write(role='offline-freeze') as freeze:
   freeze.execute('BEGIN IMMEDIATE')
   with o.Owner(c.root,'backup-source',guard=c.store.guard,readonly=True) as source:
    with o.Owner(c.root,'backup-destination',guard=c.store.guard,path=output,initialize=True,validate=False) as destination:
     try:
      if action=='rollback':freeze.rollback()
      else:freeze.__exit__(None,None,None)
     except BaseException as error:failure=repr(error)
     else:failure=None
     other=contend(c);source.backup_to(destination)
     results.append({'boundary':'parent-'+action+'-with-children','error':failure,'raw_other_process':other,'backup_succeeded':True,'destination_size':output.stat().st_size})
(ROOT/'reservation-fix-results.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
