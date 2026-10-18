"""Run the committed candidate's publication suite only in the disposable export."""
from pathlib import Path
import json, os, subprocess, sys, time
BASE=Path(__file__).resolve().parent
cwd=BASE/'checkout/research-workspace'
command=[sys.executable,'-W','error::ResourceWarning','-m','unittest','discover','-s','tests','-p','test_guard_publication.py','-v']
started=time.monotonic()
with (BASE/'candidate-independent.log').open('wb') as out:
    result=subprocess.run(command,cwd=cwd,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},stdout=out,stderr=subprocess.STDOUT,timeout=180)
report={'command':command,'cwd':str(cwd),'exit_code':result.returncode,'seconds':time.monotonic()-started,'head':'cf8befca9cef418d7e1238945872bed45d7fc6f2'}
(BASE/'candidate-independent.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
raise SystemExit(result.returncode)
