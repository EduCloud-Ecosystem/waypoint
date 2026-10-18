import json,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
with fresh() as c:
    with c.store.read() as db:
        row=db.execute('SELECT subject, zeroblob(1000000) AS subject FROM users').fetchone()
    result={'duplicate_names':list(row.keys()),'second_field_type':type(row[1]).__name__,'second_field_bytes':len(row[1]),'declared_subject_limit_bytes':80}
    (ROOT/'projection-capability-results.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
