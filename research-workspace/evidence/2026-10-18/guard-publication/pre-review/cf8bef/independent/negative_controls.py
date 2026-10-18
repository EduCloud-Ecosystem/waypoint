"""Create deliberately broken disposable copies; verify corresponding controls fail.
Never edits the reviewed worktree or the unmodified exported checkout.
"""
from pathlib import Path
import hashlib, json, os, shutil, subprocess, sys, tempfile, time
BASE=Path(__file__).resolve().parent
ORIGINAL=BASE/'checkout/research-workspace'
mutations=[
 {'name':'cached_permission','files':{'research_workspace/guard_publication.py':[(
  '        # These observations MUST precede the durability-cache shortcut.\n',
  '        if self.current()[0] == lease.durable_incarnation:\n            return self.guard.load()\n        # Deliberately incorrect permission cache for negative control.\n')]},
  'independent':['IndependentControls.test_residue_arriving_after_business_statement_blocks_commit','IndependentControls.test_missing_receipt_at_same_incarnation_blocks_safety']},
 {'name':'missing_takeover_barrier','files':{'research_workspace/guard_publication.py':[(
  '            self.guard.barrier()\n','            pass  # Deliberately omit real SQL durability barrier.\n')]},
  'independent':['IndependentControls.test_takeover_performs_real_barrier_before_business']},
 {'name':'uncertain_as_terminal','files':{
  'research_workspace/guard_publication.py':[(
   '            unresolved = item["phase"] == "uncertain" or receipts[identity][2] not in g.TERMINAL\n',
   '            unresolved = receipts[identity][2] not in g.TERMINAL\n')],
  'research_workspace/operation_guard.py':[(
   '                    terminal = self.proof(data)[oid][2] in TERMINAL and item["phase"] != "uncertain"\n',
   '                    terminal = self.proof(data)[oid][2] in TERMINAL\n')]},
  'independent':['IndependentControls.test_uncertain_essential_terminal_receipt_does_not_grant_fresh_safety']},
 {'name':'late_oversize_refusal','files':{'research_workspace/guard_publication.py':[(
  '        if len(payload) > g.MAX_BYTES:\n            raise g.GuardPaused("intent_limit")\n',
  '        # Deliberately omit bound before candidate file creation.\n')]},
  'candidate':['test_guard_publication.GuardPublicationTests.test_one_byte_overflow_precedes_scratch_and_business_effects']},
 {'name':'ordinary_note_consumes_safety','files':{'research_workspace/guard_publication.py':[(
  '        name = SAFETY if essential else ORDINARY\n',
  '        name = SAFETY if essential or stage == "uncertainty-note" else ORDINARY\n')]},
  'candidate':['test_guard_publication.GuardPublicationTests.test_required_failure_note_preserves_original_error_and_role']},
]
reports=[]
for item in mutations:
    dest=Path(tempfile.mkdtemp(prefix='negative-'+item['name']+'-',dir=BASE))/'research-workspace'
    shutil.copytree(ORIGINAL,dest)
    changes={}
    for relative,replacements in item['files'].items():
        path=dest/relative; original=path.read_text(); changed=original
        for old,new in replacements:
            assert changed.count(old)==1,(item['name'],relative,changed.count(old))
            changed=changed.replace(old,new)
        path.write_text(changed)
        changes[relative]={'original_sha256':hashlib.sha256(original.encode()).hexdigest(),'mutated_sha256':hashlib.sha256(changed.encode()).hexdigest()}
    if 'independent' in item:
        cmd=[sys.executable,'-W','error::ResourceWarning',str(BASE/'adversarial_controls.py'),'--source',str(dest),*item['independent']]
    else:cmd=[sys.executable,'-W','error::ResourceWarning','-m','unittest','-v',*item['candidate']]
    log=BASE/('negative-'+item['name']+'.log')
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':str(dest)+os.pathsep+str(dest/'tests')}
    with log.open('wb') as output:
        proc=subprocess.run(cmd,cwd=dest,env=env,stdout=output,stderr=subprocess.STDOUT,timeout=60)
    output=log.read_text()
    meaningful=proc.returncode!=0 and ('AssertionError:' in output) and 'ModuleNotFoundError' not in output and 'SyntaxError' not in output
    record={'name':item['name'],'exit_code':proc.returncode,'detected_by_assertion':meaningful,'command':cmd,'source':str(dest),'source_changes':changes,'log':str(log)}
    reports.append(record);print(json.dumps(record),flush=True)
(BASE/'negative-controls.json').write_text(json.dumps({'reviewed_head':'cf8befca9cef418d7e1238945872bed45d7fc6f2','controls':reports,'all_detected':all(r['detected_by_assertion'] for r in reports)},indent=2)+'\n')
raise SystemExit(not all(r['detected_by_assertion'] for r in reports))
