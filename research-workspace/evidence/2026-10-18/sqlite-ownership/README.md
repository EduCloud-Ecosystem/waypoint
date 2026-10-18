# Scoped SQLite ownership evidence

Current runtime candidate: `43890cbf10a02cd522da627f6ea5a841820a4762`.
Its [full suite](full-suite-43890cb.log) passes 433 tests and
[frozen qualification](candidate-8/report.json) passes 49 v4 controls and detects
29 deliberately broken variants. The captured source archive SHA-256 is
`cbb0b9242cd45d5aec1d0c356b6f39f90f7efc2ac533d068999dd90c12e75d75`.
All 91 captured source files match the immutable runtime commit. JavaScript
syntax checks also pass. The [final independent review](review-43890cb/review.json)
is CLEAR for this runtime commit: R1–R15 are resolved, with 49 controls, the
19 earlier witnesses, nine reservation checks, 32 rejected random-expression
attempts and all three rejected string-disguise cases retained. Its review
SHA-256 is `baea5b37f265c97480efc0e2e70ee249e28f9b6ac72312ec6e5f788db72369c7`.
The [final runtime provenance](final-runtime-provenance.json) binds this record
to the source and evidence. Exact final-PR-head CI and coordinating CLEAR remain
separate merge gates. This source review does not authorize an owner merge.

The [documentation correction](documentation-correction.json) after evidence
head `b2972b2` distinguishes the preserved original failed cleanup receipt from
the later accepted, resolved resource/accounting observation. Runtime and test
files remain identical to the cleared source; the qualification archive retains
its exact reviewed documentation. Original proposal and review artifacts remain
unchanged as historical records.

Earlier runtime candidate: `832d2844bb82ff7a9e24e19a3f2c551fb9849f48`.
Its [full suite](full-suite-832d284.log) passes 427 tests and
[frozen qualification](candidate-5/report.json) passes 43 v4 controls and detects
20 deliberately broken variants. The captured source archive SHA-256 is
`2a38b25ed980019c0bb6e78f08e085298db104e5ed8529b34ca99a79cfb754df`.
[Consolidated provenance](consolidated-runtime-provenance.json) binds the results
to the unchanged committed source. Final independent review, exact-head CI and
coordinating CLEAR remain separate gates.

Earlier candidate: `5d3e06b02c0e131acdcf99f16c9cc520257ce122`.
Its [full suite](full-suite-5d3e06b.log) passes 419 tests and
[frozen qualification](candidate-3/report.json) passes 35 v4 controls and detects
ten deliberately broken variants. The captured source archive SHA-256 is
`28fba3312a6839e83dad49460fadee87ddf50b2994267c606d74934f7a689670`.
[Review-fix provenance](review-fixes-provenance.json) binds those results to
the committed source; its later independent HOLD review is retained below.

Initial runtime candidate: `1837c55b5e851a63688a0b14ea5bcc9bcb0c20b7`.
The approved pre-code design remains frozen at
`4606cc6076999c6bae507bdb6beab4bbd0b0819a`, on accepted #45.

- [Full suite](full-suite.log): 416 tests passed with ResourceWarning treated
  as an error; JavaScript syntax check also passed.
- [Frozen v4 qualification](candidate-2/report.json): 32 controls passed and
  all seven deliberate regressions were detected. The
  [captured source](candidate-2/candidate-source.tar.gz) has SHA-256
  `bcd34f482fad871455bd4b45df0a0f50eff82ba7761ea4eef721e6ead0c15e15`.
- [Runtime provenance](runtime-provenance.json) binds those results to 91
  captured source files, records the eight intentional existing-runtime edits,
  and verifies 395 unchanged pre-code files including 343 historical evidence
  files. The pre-code provenance is preserved without rewriting its claims.

The v4 controls cover same-snapshot read bounds, per-handle cleanup attempts,
original-error preservation, shared-root holds, one diagnostic allowance per
token, both #43 regressions, two-process contention, all clean full-cap safety
paths, held safety refusal, export reader release, late authority recapture and
the two-phase offline backup boundary. They include native SQLite contention
after a deliberately unclosed writer: an unlocked application guard is not
proof of quiescence. The test explicitly releases its disposable native handle
after observing contention; production performs no retry or repair.

## Retained unsuccessful evidence

[Candidate 1](candidate-1/report.json) is retained with its exact source. Its
baseline had one fixture failure: the HTTP request lacked the required
`X-Research-Request` header and was correctly rejected before authentication.
It is recorded as FAIL, even though its seven negative controls detected their
mutations. Candidate 2 supplies the header and adds post-spawn/guard-close
failure controls.

The [preliminary independent review](preliminary-review.json) and
[adversarial results](adversarial-results.json) remain available with their
[complete captured source](preliminary-review-snapshot.tar.gz). That review
identified three issues: publishing a backup manifest before SQL cleanup,
losing the original exception to a failed rollback, and allowing successful
scope exit after a caught cursor-close failure. The runtime candidate includes
fixes and regression controls; this preliminary record itself is not approval
of the candidate. Its failed instrumentation control is also retained.

The development logs in `iteration-logs/` include instrumentation errors,
intermediate runtime failures and passing iterations. They were not each
captured with an immutable source and are not retrospective qualification.

The [immutable review of 1837c55](review-1837c55/review.json) verified the first
three fixes and both #43 regressions, but returned HOLD. Six adversarial cases
showed three more issues: reused/reentrant/wrong-thread owners could leak or
corrupt cleanup state, diagnostic joins could read business rows, and receipt
or barrier roles could write business tables. Its complete source archive,
scripts and results are retained. The new candidate enforces one-use,
thread-bound ownership and complete statement allowlists for restricted roles.
These are application capability-contract findings, not claimed remote exploits.
The [earlier-head CI success](ci-d59edb4.json) does not clear the new candidate.

The [consolidated review of 5d3e06b](review-5d3e06b/review.json) verified R1–R6
and retained ten further adverse witnesses grouped as R7–R12. Those cover
unrestricted schema scripts, guard/root/path or validation bypasses, reading
after rollback, diagnostic retry after a caught failure, caught native effect
failures, and duplicate projections bypassing field bounds. The exact source,
scripts and results remain available. The later candidates include controls
for these twelve finding groups and retain clean business-denial behavior.

[Candidate 4](candidate-4/report.json) is a passing source-pinned intermediate
script fix at `1c3a97a`; it does not address all consolidated findings. The
`full-schema-fix` development log overlapped later source edits and is explicitly
not exact-commit qualification. The first comprehensive-fix iteration had one
obsolete diagnostic-test expectation; it is retained, with the corrected test
included in the final freeze. No earlier PASS or HOLD is rewritten.

The [review of 832d284](review-832d284/review.json) confirmed all 19 earlier
witnesses were resolved but returned HOLD for R13. Backup owners trusted a
parent role without proving that its validated immediate reservation remained
active. Seven adverse cases and a valid reservation control are retained with
the exact source. Candidate 6 at `d06cba9` added admission/use checks and refused
parent rollback or exit while child owners remained active. Its
[qualification](candidate-6/report.json) passed 46 controls and 25 deliberate
regressions; its [full suite](full-suite-d06cba9.log) passed 430 tests.

The [review of d06cba9](review-d06cba9/review.json) verified R13 and all earlier
witnesses, then returned HOLD for R14: a nondeterministic SQL expression yielded
a 1,000,000-byte subject through an 80-byte field limit because metadata and
payload were separately evaluated. Candidate 7 at `abe2f11` restricts complete
query shapes and copies scalar bindings once. Its
[qualification](candidate-7/report.json) passed 48 controls and 28 deliberate
regressions; its [full suite](full-suite-abe2f11.log) passed 432 tests. The first
R14 iteration failed nine full-cap subcases because their fixture selected an
unregistered projection; it now uses the existing production query. That
failure log remains in `iteration-logs/`.

The [review of abe2f11](review-abe2f11/review.json) rejected the original R14
witness in all 32 attempts and verified the production query inventory, but
returned HOLD for R15. Three custom-string witnesses disguised SQL during
normalization and bypassed read, write or script restrictions. The following
candidate requires plain built-in strings before normalization or effects.
Every prior PASS, HOLD, source capture and reproduced failure remains intact.

## Acceptance boundary

This is disposable local synthetic application evidence, including real
two-process SQLite contention. It is not power-loss/VFS qualification, v4
Docker/provider acceptance, checkpoint/drain admission, physical capacity or
global quiescence. The unchanged standard Docker workflow exercises its
existing profiles. Exact candidate CI, final independent review and
coordinating CLEAR remain separate merge gates.

Source candidates stay disabled. The original failed workload and unknown
cleanup receipt remain unchanged. The accepted
[later observation](../../2026-10-17/replay-cleanup-reconciliation/README.md)
verified container absence, cleanup complete and liability resolved, with
30 CPU-seconds conservatively charged and no refund. That later observation
resolves resource/accounting state; it does not promote candidates or satisfy
independent-human reproduction, hosted or real-data admission gates.
The #39/#41 archives and fifth-headroom FAIL remain unchanged, available
capacity remains UNKNOWN and `quota_enforced` remains false. Belay, parent pilot
worktrees, accepted #43 worktree and provider state are untouched.
