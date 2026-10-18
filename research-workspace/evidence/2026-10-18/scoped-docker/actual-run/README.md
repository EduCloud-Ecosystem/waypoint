# Fresh v4 local Docker qualification

**PASS on 2026-10-18:** exactly four sequential actual attempts completed through
fresh production `--scoped-sqlite` initialization, loopback HTTP with current
local-key authorization, the production Worker, and bounded public export.
[verification.json](verification.json) records all 18 required controls, exact
attempt/project/source identities and hashes linking the private evidence.

| Slot | Runtime / condition | Count / mean | Reservation / charge | Cleanup |
| --- | --- | --- | --- | --- |
| First project | Python / baseline | 10 / 18 | 30 / 30 CPU-seconds | Verified |
| First project | R / alternative | 6 / 8.5 | 30 / 30 CPU-seconds | Verified |
| Second project | R / baseline | 10 / 9 | 30 / 30 CPU-seconds | Verified |
| Second project | Python / alternative | 6 / 7.5 | 30 / 30 CPU-seconds | Verified |

Both projects received exactly 60 CPU-seconds. Total charged compute is **120
conservative CPU-seconds**, with `estimated_upper_bound` provenance. These are
four full reservations/charges, not measured total host overhead or monetary
cost. Every run has a bound successful receipt, matching retained diagnostics,
an independently checked numeric result, and an explicit observation that its
owned container is absent. No admitted attempt retains unresolved liability.

The fixed exercise allowance is exhausted. There was no retry, refund, resume,
image pull or build. Its durable marker and private state remain in place. The
new qualification must never be rerun to improve evidence or obtain browser
coverage; any later browser observation can only reuse these same four runs.

## Public artifacts and provenance

- [First project](first-project.zip), SHA-256
  `2bac427dac640efe3da236bbc0018877e897432f6e7d7326eba5c533e2816bd0`,
  contains two completed executions and two cancellation/access-refusal records.
- [Second project](second-project.zip), SHA-256
  `5d7ea0ef823bd953afc514b65c26e5d6a489a6a3aa21099deb2e44d56715fea4`,
  contains two completed executions.

Both archives pass the bounded bundle verifier. Their public project/run records
equal the captured HTTP records, their input/recipe/result bytes match the
recorded hashes, and final HTTP lineage matches the prelaunch bindings. The
queued cancellation and removed-access controls made zero broker dispatches.
The latter removes access before `Worker.execute`; it does not qualify a late
revocation race. Anonymous access and removed-member export were refused.

Execution used harness `fe4788c47a378e6234648dc4131ec4b870a8e713` and
[Candidate 03](../candidate-03/manifest.json), manifest SHA-256
`ad701c8d5b164666133a1d06f241a80ab052ccbeb02a48c499a663589e537295`.
Its [34-file source archive](../candidate-03/source.tar.gz) has SHA-256
`6fcfce9bf3db5b57b4f503574f379123a43739c44fc69f9af9b40a8cc17055b5`.
The runtime remains the PR #46 merge
`0efc6fd734e7f812cabc3822d2ea93b8c1063752`.

The existing accepted broker has Outfitter source
`5446438985e31cb739f2048a2aa2176918831a7e` and binary SHA-256
`1bf1ce898d6d10e574ab7b2f79a830fa94fa740867e208470bfc15790dc41bb1`.
It ran the already present reviewed Python and R images on local linux/arm64.
The exact source/binary/image identities and all five pre-execution checker
findings, corrections, failed controls and independent reviews remain retained.
Hashes correlate bytes; they are not independent executable attestation.

The private final report has SHA-256
`8234b728d1b94ae7046777c93185ca3868aa40310e4fbf5a4ca68f12d3d22c40`.
Only an explicit public summary projection, evidence hashes and these two
bounded synthetic exports are published. Credentials, raw requests/receipts,
diagnostics, broker ledger, SQLite state and checkpoints remain private.

## Validation and remaining gates

[Pre-execution checks](../candidate-03-validation/validation.json) passed 457
tests, including 24 focused tests in normal and optimized Python. Independent
review replays closed the diagnostic, project-budget, export, charge-retention
and prelaunch-lineage gaps before the
[combined execution clearance](../candidate-03-validation/combined-pre-execution-review/combined-clearance.json).
The existing CI jobs remain separate evidence: their actual-Docker job exercises
older profiles and does not independently reproduce this fresh-v4 exercise.
The optional source-build job remains opt-in; no workflow changed.

All four source environments remain disabled. OIDC and finite-profile restore
remain refused, available physical capacity remains UNKNOWN, `quota_enforced`
remains false, and the historical fifth-headroom result remains FAIL. Browser,
hosted/live allocation, inference, real private/student data and independent
person/machine acceptance are not established. Selected host, cohort, storage,
retention and support decisions remain open. No new profile, general quota
machinery, default, accepted-state upgrade or production change is introduced.

The original failed replay receipt and workload remain failed evidence.
[PR #32's later read-only observation](../../../2026-10-17/replay-cleanup-reconciliation/README.md)
resolved that specific resource/accounting observation with its full
conservative charge and no refund. This local PASS does not rewrite that failure
or close the remaining promotion, independent acceptance, hosted or real-data
gates. Any future unknown cleanup must remain visible and unresolved.

Belay, the parent pilot worktrees and the accepted PR #46 worktree remain
untouched. PR merge still requires final independent review, passing exact-head
CI and explicit coordinating CLEAR. Source/document whitespace checks pass;
raw diff checks flag only unchanged historical failed logs and retained
`narrow.diff`. Those evidence bytes are intentionally preserved.
