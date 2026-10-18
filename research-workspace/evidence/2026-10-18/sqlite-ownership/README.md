# Scoped SQLite ownership evidence

Current runtime candidate: `5d3e06b02c0e131acdcf99f16c9cc520257ce122`.
Its [full suite](full-suite-5d3e06b.log) passes 419 tests and
[frozen qualification](candidate-3/report.json) passes 35 v4 controls and detects
ten deliberately broken variants. The captured source archive SHA-256 is
`28fba3312a6839e83dad49460fadee87ddf50b2994267c606d74934f7a689670`.
[Review-fix provenance](review-fixes-provenance.json) binds those results to
the committed source; final independent review and exact-head CI remain pending.

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

## Acceptance boundary

This is disposable local synthetic application evidence, including real
two-process SQLite contention. It is not power-loss/VFS qualification, v4
Docker/provider acceptance, checkpoint/drain admission, physical capacity or
global quiescence. The unchanged standard Docker workflow exercises its
existing profiles. Exact candidate CI, final independent review and
coordinating CLEAR remain separate merge gates.

Source candidates stay disabled; the original cleanup gate stays unresolved.
The #39/#41 archives and fifth-headroom FAIL remain unchanged, available
capacity remains UNKNOWN and `quota_enforced` remains false. Belay, parent pilot
worktrees, accepted #43 worktree and provider state are untouched.
