#!/usr/bin/env python3
"""Capture and qualify disposable v4 owners, including deliberate regressions.

No provider, Docker, historical harness, quota, checkpoint/drain gate or cleanup.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


def digest(content):
    return hashlib.sha256(content).hexdigest()


def write_json(path,value):
    path.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n")


def run(source,names,log):
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE="1",PYTHONPATH=str(source)+os.pathsep+str(source/"tests"))
    result=subprocess.run([sys.executable,"-W","error::ResourceWarning","-m","unittest",*names,"-v"],cwd=source,
                          env=env,capture_output=True,text=True,timeout=120)
    output=(result.stdout+result.stderr).replace(str(source),"<captured-source>")
    log.write_text(output)
    return {"exit_code":result.returncode,"log":log.name,"log_sha256":digest(log.read_bytes())}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True)
    args=parser.parse_args()
    output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=False)
    source=Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="waypoint-owned-qualification-") as tmp:
        captured=Path(tmp)/"source"
        shutil.copytree(source,captured,ignore=shutil.ignore_patterns("__pycache__",".runtime","evidence","qualification","design"))
        files=sorted(p for p in captured.rglob("*") if p.is_file())
        write_json(output/"source-sha256.json",{str(p.relative_to(captured)):digest(p.read_bytes()) for p in files})
        buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode="w") as archive:
            for path in files:
                data=path.read_bytes();info=tarfile.TarInfo(str(path.relative_to(captured)));info.size=len(data);info.mode=0o644
                archive.addfile(info,io.BytesIO(data))
        (output/"candidate-source.tar.gz").write_bytes(gzip.compress(buffer.getvalue(),mtime=0))
        baseline=run(captured,["test_sqlite_ownership"],output/"controls.log")
        variants=[
          ("reusable-owner","research_workspace/sqlite_ownership.py",[("if self._thread != threading.get_ident() or self._entered:","if self._thread != threading.get_ident():")],"test_owner_reentry_reuse_and_wrong_thread_refuse_before_any_open"),
          ("diagnostic-business-join","research_workspace/sqlite_ownership.py",[("if self.role in _ROLE_READS:","if self.role in _ROLE_READS and not normalized.startswith('SELECT '):")],"test_diagnostic_role_rejects_join_union_subquery_and_business_pragma"),
          ("proof-role-business-write","research_workspace/sqlite_ownership.py",[("if self.role in _ROLE_READS:","if self.role in _ROLE_READS and not normalized.startswith('UPDATE '):")],"test_proof_writer_roles_cannot_mutate_or_observe_business_rows"),
          ("writable-reader","research_workspace/sqlite_ownership.py",[( '"ro" if self.readonly else "rw"', '"rw" if self.readonly else "rw"')],"test_online_read_guard_before_existing_ro_and_same_snapshot"),
          ("missing-connection-close","research_workspace/sqlite_ownership.py",[("                    self.__connection.close()","                    pass  # deliberate missing close")],"test_open_setup_cursor_rollback_close_failure_matrix"),
          ("swallowed-cursor-close","research_workspace/sqlite_ownership.py",[("        if original is None:\n            try:\n                if self._uncertain:\n                    raise self._cleanup_error or OwnershipFailure()\n                _permit(self.root,self.role,self.guard)\n            except BaseException as failure:\n                original=failure\n","")],"test_caught_cursor_close_failure_cannot_turn_scope_exit_into_success"),
          ("reusable-diagnostic-token","research_workspace/sqlite_ownership.py",[( 'getattr(token, "_ownership_diagnostic_used", False)', 'False')],"test_nested_business_barrier_and_diagnostic_business_reads_are_refused"),
          ("primary-dispatch-error-lost","research_workspace/store.py",[("if self.profile_version != 4:\n                        db.rollback()","if True:\n                        db.rollback()")],"test_primary_spawn_and_offline_errors_survive_rollback_failure"),
          ("spawn-after-reservation-close","research_workspace/store.py",[("                        return spawn()","                        return spawn"),("                return token.launch(current_launch)","                callback = token.launch(current_launch)\n                return callback()")],"test_unrelated_publication_qualifies_before_request_and_final_spawn_fenced"),
          ("skip-export-recapture","research_workspace/bounded_export.py",[("current = _capture(store, project_id, subject, token)","current = manifest")],"test_export_rechecks_authority")]
        negative=[]
        for name,relative,replacements,test in variants:
            target=captured/relative;original=target.read_text();mutated=original
            for old,new in replacements:
                if mutated.count(old)!=1:raise ValueError("Mutation anchor must occur exactly once: "+name)
                mutated=mutated.replace(old,new,1)
            target.write_text(mutated)
            try:
                result=run(captured,["test_sqlite_ownership.OwnershipTests."+test],output/(name+".log"))
                result.update(name=name,source_file=relative,mutated_source_sha256=digest(target.read_bytes()),
                              replacements=[{"before":a,"after":b} for a,b in replacements],detected=result['exit_code']!=0)
                negative.append(result)
            finally:target.write_text(original)
        report={"format":"waypoint-scoped-sqlite-qualification-v1","baseline":baseline,"negative_controls":negative,
                "source_archive_sha256":digest((output/"candidate-source.tar.gz").read_bytes()),
                "source_manifest_sha256":digest((output/"source-sha256.json").read_bytes()),
                "status":"PASS" if baseline['exit_code']==0 and all(x['detected'] for x in negative) else "FAIL",
                "boundary":"Disposable synthetic application failure injection, real process contention; not power-loss, VFS, provider, Docker, capacity or global quiescence evidence.",
                "quota_enforced":False,"available_capacity":"UNKNOWN","fifth_headroom":"FAIL_UNCHANGED"}
        write_json(output/"report.json",report)
        print(json.dumps({"status":report['status'],"negative_controls_detected":sum(x['detected'] for x in negative),
                          "source_archive_sha256":report['source_archive_sha256'],"report_sha256":digest((output/"report.json").read_bytes())}))
        return 0 if report['status']=="PASS" else 1


if __name__=="__main__":raise SystemExit(main())
