# SQLite and guard qualification evidence

Nine disposable production-connected controls pass against unchanged Waypoint
`e79a50c6074e3ecf579a5bc5b0732c473d62502d`. This qualifies the witnesses and
per-save limits, not a finite whole-store write protocol. Available capacity
remains **UNKNOWN**, quota admission **NO-GO**, and `quota_enforced:false`.

[qualification.json](qualification.json) records the 22 source hashes, harness
and contract hashes, runtime fingerprint, settings and individual observations.
[qualification.log](qualification.log) is its raw output. The full local suite
passed 318 tests in [suite.log](suite.log). Its new bridge executes these nine
controls; the existing capacity bridge executes eight frozen controls. These
counts overlap and must not be added. JavaScript syntax and whitespace pass.
Exact-head CI and final independent review are recorded separately in the PR.

## Observations and their limits

| Control | Observed result | Interpretation |
| --- | --- | --- |
| Held SELECT reader and 160 real key rotations | WAL reached 4,614,432 bytes; PASSIVE reported 1,120 frames with zero checkpointed; default threshold was 1,000 pages | Automatic checkpoint threshold is not a live bound under that reader. Explicit TRUNCATE succeeded only after reader release. Settings were unchanged. |
| Six Store opens | Each startup barrier is a writer; WAL grew | Guard counters do not bound this history. |
| Commit failure before and after real commit | Revoke rolled back in the first case and persisted in the second; both kept an uncertainty hold | Injected Python acknowledgement faults, not SQLite VFS errors or power-loss tests. |
| Full 4,096 plus one guard map | Current and temporary were each 2,327,358 bytes; measured coexistence 4,654,716 bytes | Seeded maximum-width valid map and receipts, not 4,097 admissions. Enforced per-save cap remains 4 MiB; current plus one candidate at most 8 MiB excluding prior scratch and all other writers. |
| Three repeated file-fsync failures | Three distinct protected temporary files; old current guard and business data unchanged before later success | No automatic orphan removal or future retry-count bound. |
| Parent-fsync failure after rename | New guard visible with uncertain durability; subsequent safety call remains held | Visible generation does not establish completion or reclaimable bytes. |
| Nine independent safety actions under a reader | Authentication, logout, demotion/removal, revoke/rotate, cancellation, settlement and project close returned with verified effects | Separate fresh fixtures; row caps were not filled, and physical write availability is not proved. |
| Eight borrowed essential rotations | Guard bytes unchanged while WAL grew | Neither sidecar generation nor intent count caps write history. |
| Fresh-process recovery of 20 near-ceiling synthetic run records | Nineteen cancelled, one interrupted with unknown liability; new process remained paused; WAL observed at 5,228,312 bytes | Payload padding was seeded, no provider ran, and before/after sizes are not an engine transient peak. |

Values are observations on the recorded local runtime, not portable maxima.
The [protocol proposal](../../../SQLITE-GUARD-PROTOCOL.md) leaves reader ownership,
retry-slot reuse/exhaustion and engine ceilings for separate decisions. No such
protocol is installed by this qualification.

## Preserved failed assumption

[pre-review/second.log](pre-review/second.log),
[second.json](pre-review/second.json) and
[second-qualify.py](pre-review/second-qualify.py) retain the first nine-control
attempt unchanged. Eight controls passed; the recovery control expected a paused
restart after calling recover in the same process. Its existing live run lease
made that expectation invalid. This is a fixture assumption failure, not a
reported production defect. The corrected control invokes the production Store
and recovery method in a fresh process, then checks records and paused status.
The first seven-control exploratory report/log are also retained; its exact
intermediate harness was not frozen, so no exact replay claim is made for it.

## Preserved state and remaining gates

All 199 pre-existing evidence files match the base byte-for-byte, including #39's
frozen contract and #40's original failures. Application source, the active
broker pin, default WAL/FULL/settings, source candidates, accepted and rejected
stores, Belay, parent worktrees and Docker remain unchanged. The fifth consumed
headroom failure remains FAIL. No restore enablement, quota admission, migration,
retention deletion or live provider test occurred.

The manifest binds retained files; hashes provide integrity, not independent
authenticity. Retain its digest or the accepted Git commit separately. The
harness only accepts a new report path, never an existing state root, and removes
its own disposable fixtures after preserving observations.
