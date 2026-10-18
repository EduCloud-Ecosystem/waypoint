"""Assemble immutable-head review with replayable script and evidence hashes."""
from pathlib import Path
import hashlib,json,platform,sqlite3,sys
B=Path(__file__).resolve().parent
hashes=json.loads((B/'independent-source.json').read_text())['source_sha256']
report={
 'reviewed_head':'cf8befca9cef418d7e1238945872bed45d7fc6f2',
 'base':'66a48dfe29d61d3f6f48f016f2c474e1ac6af4f6',
 'verdict':'BLOCKED',
 'findings':[{'priority':'P1','title':'Keep the cancelled claim token live through the final transaction check',
 'path':'research-workspace/research_workspace/store.py','lines':[73,75,641,645],
 'detail':'The cancelled-before-dispatch path completes the SQL receipt and raw commit, releases its operation, and returns from inside transaction(). The new final TrackedConnection.commit calls check_publication, which rejects the already-released token as missing_operation. Error cleanup then latches operation_failed. The cancellation and committed receipt are durable but subsequent ordinary admissions and worker claims remain paused until process restart. Reproduced for cancellation, local revocation, project closure, and expiry.',
 'replay':'claim_refusal_witness.py','evidence':'claim-refusal-witness.json',
 'expected':'Clean pre-dispatch refusal returns None, records cancellation and terminal receipt, releases its token after final checks, and leaves unrelated ordinary admissions available.',
 'required_fix_validation':'Exercise cancellation, revocation, closure and expiry before claim; assert None, cancelled row, committed receipt, no active token, ready status and subsequent admission. Retain publication checks for genuinely active/kept tokens and preserve existing supersession safeguards.'}],
 'independent_validation':{'candidate_publication_tests':{'passed':30,'report':'candidate-independent.json','log':'candidate-independent.log'},
 'additional_adversarial_tests':{'passed':9,'log':'adversarial-controls.log','script':'adversarial_controls.py'},
 'regression_witnesses':{'reproduced':4,'report':'claim-refusal-witness.json'},
 'negative_mutants':json.loads((B/'negative-controls.json').read_text()),
 'provenance':json.loads((B/'provenance.json').read_text())},
 'scope':'Single bounded read-only implementation review. Disposable state and simulated providers only. No worktree changes, Docker, accepted stores, broker pin or root-owned harness operations. No full-suite rerun claimed. No power-loss, VFS, live-provider or portable-quota qualification.',
 'other_review_results':'No additional actionable blocker found in role routing, exact binding/export gates, scratch preservation, exact-incarnation durability caching, fresh-process/takeover ordering, or the explicitly conditional 12582912-byte named-payload-only claim. The additional controls verify real safety effects, missing receipt refusal, post-statement residue refusal, essential uncertain-phase retention, real SQL takeover barrier and close failure, mixed binding refusal, and conservative settlement authorization.',
 'source_sha256':hashes,
 'runtime':{'python':sys.version,'sqlite':sqlite3.sqlite_version,'platform':platform.platform()},
 'artifacts':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(B.iterdir()) if p.is_file() and p.name!='review.json'},
 'provenance_note':'All independent harnesses for this review are saved standalone .py files, not merely inline execution. prepare.py binds the immutable export; rerunning it refuses to overwrite an existing export. negative_controls.py makes new disposable mutated copies and saves their exact changed source hashes.'
}
(B/'review.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({'verdict':report['verdict'],'head':report['reviewed_head'],'review':str(B/'review.json'),'sha256':hashlib.sha256((B/'review.json').read_bytes()).hexdigest()}))
