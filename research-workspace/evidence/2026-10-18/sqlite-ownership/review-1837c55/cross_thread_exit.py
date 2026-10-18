import json,pathlib,sys,threading
ROOT=pathlib.Path(__file__).resolve().parent;SOURCE=ROOT/'source/research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_sqlite_ownership import fresh
from research_workspace import sqlite_ownership as o
with fresh() as c:
    owner=o.Owner(c.root,'online-read',guard=c.store.guard,readonly=True)
    events={}
    def misuse():
        try:owner.__exit__(None,None,None)
        except BaseException as e:events['wrong_thread_exception']=repr(e)
    try:
        with owner:
            thread=threading.Thread(target=misuse);thread.start();thread.join(3)
            events['thread_stopped']=not thread.is_alive()
            events['active_in_original_scope']=owner._active
            try:owner.execute('SELECT subject FROM users')
            except BaseException as e:events['original_scope_query_exception']=repr(e)
    except BaseException as e:events['original_exit_exception']=repr(e)
    events['held']=o.held(c.root);events['guard_depth']=c.store.guard.lease.depth
    (ROOT/'cross-thread-exit.json').write_text(json.dumps(events,indent=2)+'\n');print(json.dumps(events,indent=2))
