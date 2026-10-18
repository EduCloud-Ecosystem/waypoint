"""Replay with python3 script.py; all state is disposable, providers simulated."""
from pathlib import Path
import contextlib, hashlib, json, os, sys, traceback
BASE=Path(__file__).resolve().parent
SOURCE=BASE/'checkout'/'research-workspace'
sys.path[:0]=[str(SOURCE),str(SOURCE/'tests')]
from test_guard_publication import fresh
report={'head':'cf8befca9cef418d7e1238945872bed45d7fc6f2','source_sha256':hashlib.sha256((SOURCE/'research_workspace/store.py').read_bytes()).hexdigest(),'cases':[]}
for action in ('cancel','revoke','close','expired'):
    with fresh() as c:
        run=c.submit()
        if action=='cancel': c.store.cancel(c.pid,run['id'],'alice')
        elif action=='revoke': c.store.revoke_user('alice')
        elif action=='close': c.store.close_project(c.pid,'alice')
        else:
            with c.store.transaction(essential=True) as db:
                payload=json.loads(db.execute('SELECT payload FROM projects WHERE id=?',(c.pid,)).fetchone()[0])
                payload['ends_at']='2000-01-01T00:00:00+00:00'
                db.execute('UPDATE projects SET payload=? WHERE id=?',(json.dumps(payload),c.pid))
        before=c.store.operation_status()
        result=None
        try: result=c.store.claim()
        except Exception as exc:
            result={'exception':type(exc).__name__,'code':getattr(exc,'code',None),'notes':getattr(exc,'__notes__',[])}
        after=c.store.operation_status()
        with contextlib.closing(c.store.connect()) as db:
            row=db.execute('SELECT status,cancel_requested FROM runs WHERE id=?',(run['id'],)).fetchone()
            receipt=db.execute('SELECT outcome FROM operation_receipts ORDER BY sequence DESC LIMIT 1').fetchone()[0]
        case={'action':action,'before':before,'claim':result,'after':after,'run':list(row),'latest_receipt':receipt}
        try:
            c.store.add_user('probe')
            case['later_admission']='allowed'
        except Exception as exc: case['later_admission']={'exception':type(exc).__name__,'code':getattr(exc,'code',None)}
        report['cases'].append(case)
print(json.dumps(report,indent=2))
(BASE/'claim-refusal-witness.json').write_text(json.dumps(report,indent=2)+'\n')
