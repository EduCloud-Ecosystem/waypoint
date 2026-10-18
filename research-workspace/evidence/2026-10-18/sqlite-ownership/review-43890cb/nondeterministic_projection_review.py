import json,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace';sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
sql="SELECT CASE WHEN (random() & 1)=0 THEN subject ELSE CAST(zeroblob(1000000) AS TEXT) END AS subject FROM users LIMIT 1"
results=[]
with fresh() as c:
 for attempt in range(32):
  try:
   with c.store.read() as db:
    row=db.execute(sql).fetchone()
   item={'attempt':attempt,'returned_bytes':len(row['subject'].encode()),'held':o.held(c.root)}
  except BaseException as error:item={'attempt':attempt,'exception':repr(error),'held':o.held(c.root)}
  results.append(item)
  if item.get('returned_bytes',0)>80:break
(ROOT/'nondeterministic-projection-results.json').write_text(json.dumps({'sql':sql,'attempts':results},indent=2)+'\n')
print(json.dumps({'sql':sql,'attempts':results},indent=2))
