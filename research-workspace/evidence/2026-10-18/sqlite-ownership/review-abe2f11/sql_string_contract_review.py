import pathlib,json,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace';sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
from research_workspace.storage_inventory import SCHEMA_SQL
from research_workspace.operation_guard import SCHEMA
class SQL(str):
 def __new__(cls,actual,approved):
  obj=super().__new__(cls,actual);obj.approved=approved;return obj
 def split(self,*args,**kwargs):return self.approved.split(*args,**kwargs)
results=[]
with fresh() as c:
 with o.Owner(c.root,'guard-proof',guard=c.store.guard,readonly=True) as db:
  try:rows=db.execute(SQL('SELECT subject FROM users','SELECT authority_id,sequence FROM operation_authority')).fetchall()
  except BaseException as error:result={'exception':repr(error)}
  else:result={'business_rows_returned':[list(row) for row in rows]}
 results.append({'case':'proof-execute-subclass',**result})
with fresh() as c:
 with c.store.write(role='barrier') as db:
  try:
   db.execute(SQL("UPDATE users SET active=0 WHERE subject='alice'",'UPDATE operation_authority SET barrier=? WHERE authority_id=?'));db.commit()
  except BaseException as error:result={'exception':repr(error)}
  else:result={'scope_success':True}
 with c.raw() as db:result['alice_active']=db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0]
 results.append({'case':'barrier-execute-subclass',**result})
with fresh() as c:
 actual="ATTACH DATABASE '"+str(c.root/'workspace.sqlite3').replace("'","''")+"' AS target; UPDATE target.users SET active=0 WHERE subject='alice'; DETACH DATABASE target;"
 with o.Owner(c.root,'memory-reference',guard=c.store.guard,memory=True,initialize=True,validate=False) as db:
  try:db.executescript(SQL(actual,';'.join(SCHEMA_SQL+SCHEMA)))
  except BaseException as error:result={'exception':repr(error)}
  else:result={'scope_success':True}
 with c.raw() as db:result['alice_active']=db.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0]
 results.append({'case':'schema-script-subclass',**result})
(ROOT/'sql-string-contract-results.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
