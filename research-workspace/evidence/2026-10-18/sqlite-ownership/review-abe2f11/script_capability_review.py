import json,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
with fresh() as c:
    token=c.store.guard.begin('mutation');sequence=c.store.guard.load()['sequence'];o.hold(c.root)
    path=str(c.root/'workspace.sqlite3').replace("'","''")
    sql="ATTACH DATABASE '"+path+"' AS authority; UPDATE authority.users SET active=0 WHERE subject='alice'; DETACH DATABASE authority;"
    try:
        with o.diagnostic(token):
            with o.Owner(c.root,'memory-reference',guard=c.store.guard,memory=True,initialize=True,validate=False) as db:
                db.executescript(sql)
    except BaseException as e:error=repr(e)
    else:error=None
    with c.raw() as raw:active=raw.execute("SELECT active FROM users WHERE subject='alice'").fetchone()[0]
    result={'exception':error,'held':o.held(c.root),'business_write_committed':active==0,'new_intent_created':sequence!=c.store.guard.load()['sequence']}
    (ROOT/'script-capability-results.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))
