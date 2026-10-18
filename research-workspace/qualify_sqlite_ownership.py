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
          ("unreviewed-projection","research_workspace/sqlite_ownership.py",[("if \" \".join(sql.split()) not in _SELECTS | _export_selects():","if False:")],"test_unreviewed_projection_and_predicate_refuse_before_sql_evaluation"),
          ("mutable-observation-bindings","research_workspace/sqlite_ownership.py",[("        parameters = tuple(parameters)","        parameters = parameters")],"test_observation_bindings_copied_once_and_custom_adapters_refused"),
          ("custom-observation-adapter","research_workspace/sqlite_ownership.py",[("        if any(type(value) not in (str, bytes, int, float, type(None))\n               or type(value) is float and not math.isfinite(value) for value in parameters):","        if False:")],"test_observation_bindings_copied_once_and_custom_adapters_refused"),
          ("backup-without-freeze","research_workspace/sqlite_ownership.py",[("                freezes[0]._require_freeze()","                pass  # deliberately trust the role alone")],"test_backup_admission_requires_live_immediate_parent"),
          ("deferred-freeze","research_workspace/sqlite_ownership.py",[("if self.role in {\"offline-freeze\",\"dispatch-reservation\"} and normalized == \"BEGIN\":","if False:")],"test_backup_admission_requires_live_immediate_parent"),
          ("release-parent-before-children","research_workspace/sqlite_ownership.py",[("        if self._dependents():\n            raise RuntimeError(\"End child SQLite scopes before releasing their parent reservation.\")\n        if self._rollback_attempted:","        if self._rollback_attempted:")],"test_backup_parent_cannot_release_children_and_blocks_other_process"),
          ("skip-pre-copy-reservation","research_workspace/sqlite_ownership.py",[("        self._freeze_owner._require_freeze()\n        try:","        try:")],"test_backup_rechecks_native_reservation_before_and_after_copy"),
          ("skip-post-copy-reservation","research_workspace/sqlite_ownership.py",[("            self._freeze_owner._require_freeze()\n        except BaseException as failure:","        except BaseException as failure:")],"test_backup_rechecks_native_reservation_before_and_after_copy"),
          ("reader-after-rollback","research_workspace/sqlite_ownership.py",[(" or self._read_ended","")],"test_read_rollback_ends_the_validated_snapshot_capability"),
          ("skipped-validation","research_workspace/sqlite_ownership.py",[("not validate and role not in","False and role not in")],"test_existing_authority_cannot_disable_validation"),
          ("wrong-root-guard","research_workspace/sqlite_ownership.py",[("if guard is not None and Path(guard.root).resolve() != self.root:","if False:")],"test_owner_guard_and_database_path_are_bound_to_resolved_root"),
          ("wrong-database-path","research_workspace/sqlite_ownership.py",[("if path is not None and role != \"backup-destination\":","if False:")],"test_owner_guard_and_database_path_are_bound_to_resolved_root"),
          ("retry-failed-diagnostic","research_workspace/sqlite_ownership.py",[("if token is not None and token.guard.root == Path(root).resolve():","if False:")],"test_caught_diagnostic_owner_failure_ends_the_whole_attempt"),
          ("caught-write-ack","research_workspace/sqlite_ownership.py",[("            try:\n                cursor.execute(sql, parameters)\n            except BaseException as failure:\n                hold(self.root,self,failure)\n                raise","            cursor.execute(sql, parameters)")],"test_caught_write_or_commit_acknowledgement_failure_cannot_succeed"),
          ("caught-commit-ack","research_workspace/sqlite_ownership.py",[("        except BaseException as failure:\n            hold(self.root,self,failure)\n            raise\n\n    def rollback", "        except BaseException:\n            raise\n\n    def rollback")],"test_caught_write_or_commit_acknowledgement_failure_cannot_succeed"),
          ("caught-backup-ack","research_workspace/sqlite_ownership.py",[("            hold(self.root,self,failure)\n            hold(destination.root,destination,failure)","            pass  # deliberately lose backup uncertainty")],"test_caught_backup_acknowledgement_failure_keeps_both_owners_failed"),
          ("ambiguous-projection","research_workspace/sqlite_ownership.py",[("            if len({name.casefold() for name in names}) != len(names):\n                raise ReadBounds(\"Ambiguous SQLite projection names are not permitted.\")\n","")],"test_duplicate_projection_names_refuse_before_payload_fetch"),
          ("diagnostic-attach-script","research_workspace/sqlite_ownership.py",[("if not _schema_allowed(self.role, sql, script=True):","if not self.initialize:")],"test_schema_scripts_cannot_attach_file_authority"),
          ("reusable-owner","research_workspace/sqlite_ownership.py",[("if self._thread != threading.get_ident() or self._entered:","if self._thread != threading.get_ident():")],"test_owner_reentry_reuse_and_wrong_thread_refuse_before_any_open"),
          ("diagnostic-business-join","research_workspace/sqlite_ownership.py",[("if self.role in _ROLE_READS:","if self.role in _ROLE_READS and not normalized.startswith('SELECT '):")],"test_diagnostic_role_rejects_join_union_subquery_and_business_pragma"),
          ("proof-role-business-write","research_workspace/sqlite_ownership.py",[("if self.role in _ROLE_READS:","if self.role in _ROLE_READS and not normalized.startswith('UPDATE '):")],"test_proof_writer_roles_cannot_mutate_or_observe_business_rows"),
          ("writable-reader","research_workspace/sqlite_ownership.py",[( '"ro" if self.readonly else "rw"', '"rw" if self.readonly else "rw"')],"test_online_read_guard_before_existing_ro_and_same_snapshot"),
          ("missing-connection-close","research_workspace/sqlite_ownership.py",[("                    self.__connection.close()","                    pass  # deliberate missing close")],"test_open_setup_cursor_rollback_close_failure_matrix"),
          ("swallowed-cursor-close","research_workspace/sqlite_ownership.py",[("        if original is None:\n            try:\n                if self._uncertain:\n                    raise self._cleanup_error or OwnershipFailure()\n                _permit(self.root,self.role,self.guard)\n            except BaseException as failure:\n                original=failure\n","")],"test_caught_cursor_close_failure_cannot_turn_scope_exit_into_success"),
          ("reusable-diagnostic-token","research_workspace/sqlite_ownership.py",[( 'getattr(token, "_ownership_diagnostic_used", False)', 'False')],"test_diagnostic_allowance_is_once_per_token_not_nested_owner"),
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
