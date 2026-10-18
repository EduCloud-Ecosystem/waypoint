# Fresh v4 Docker qualification

This is one qualification-only local exercise of the runtime accepted in PR #46
at `0efc6fd734e7f812cabc3822d2ea93b8c1063752`. Its execution status is **NOT RUN**.
The standard Docker CI job exercises older profiles, so its passing result does
not establish that fresh v4 has completed this HTTP, Worker and export path.

The new harness, `qualify_scoped_docker.py`, requires coordinating and Outfitter
accounting review of the exact prepared manifest and source archive before its
`run` command. The manifest hash identifies the reviewed candidate; supplying a
hash is not itself review approval. Any subsequent PR merge also requires an
independent final review, passing CI and coordinating CLEAR.

## Fixed exercise

The exercise is `waypoint-v4-docker-20261018-01`. It initializes a fresh private
store with the production `--scoped-sqlite` CLI, starts the HTTP service on
loopback, and uses current local-key sessions. It invokes the production Worker
sequentially, without starting its background queue loop.

| Slot | Synthetic project and fixture | Runtime | Condition | Expected count / mean |
| --- | --- | --- | --- | --- |
| 1 | First / public-fixture.csv | Python | Baseline | 10 / 18 |
| 2 | First / public-fixture.csv | R | Alternative | 6 / 8.5 |
| 3 | Second / second-project.csv | R | Baseline | 10 / 9 |
| 4 | Second / second-project.csv | Python | Alternative | 6 / 7.5 |

Group counts and means are independently enumerated in the harness. Each project
receives 60 CPU-seconds. Each actual run retains the existing 1 CPU, 256 MiB,
64 PID, 30 second and 65,536 output-byte caps. Four attempts and 120 conservative
CPU-seconds are the entire exercise allowance, including interruptions and
restarts. These are four reservations/charges; they do not measure total host
overhead or prove termination after unknown cleanup.

There is no fifth attempt, retry, refund, resume, image pull, image build or
alternate-output option. A durable exclusive marker outside the fixed output
folder claims the whole exercise before initialization. Moving or deleting the
output does not renew the allowance. After interruption, use `status` and manual
review of the retained evidence. Never remove the marker to rerun this exercise.
This marker is qualification instrumentation, not a new capacity ledger or grant
system.

## Existing dependencies and provenance

The revised candidate uses the already present Python and R images in
`images.lock.json` and the retained `qualified/candidate` broker from the accepted
PR #44 exercise. Its current Outfitter #16 source pin is
`5446438985e31cb739f2048a2aa2176918831a7e` and its SHA-256 is
`1bf1ce898d6d10e574ab7b2f79a830fa94fa740867e208470bfc15790dc41bb1`, matching
[the retained broker-upgrade verification](evidence/2026-10-18/broker-upgrade/verification.json).
The prerequisite check correlates that accepted identity, its adjacent build
manifest, live binary metadata, current source pin and all 22 retained source
hashes. This exact historical build predates the builder-helper correction; it
is not the later `qualified-final` binary. No build or download is needed. The
accounting review must confirm the exact pairing before this exercise. Neither
the CI pin nor any operator executable is changed.

Candidates 01 and 02 remain frozen and unexecuted. Candidate 01 retains its older
P1 broker pairing; Candidate 02 retains the later lineage-correlation finding.
Candidate revisions share the same exercise identity, output and durable marker;
they do not create a fresh allowance.

Preparation records the local Docker socket and daemon identity, required cap
support, machine CPU/memory totals, local image identities, binary metadata,
source hashes and source archive. It only inspects existing dependencies.
Unavailable prerequisites produce NOT RUN. Execution binds Docker children to
the reviewed local socket and checks the dependencies again. These observations
do not establish available physical capacity, quota enforcement or future
headroom.

Before each Worker invocation, the harness records the exact source, image,
binary, ledger authority/path, request, project and attempt identities. Evidence
writes occur outside SQLite owners. At the production dispatch boundary, a
single-use check compares the actual request bytes and binary to the prepared
identity. No general-purpose execution path is added.

A successful final receipt must be joined to the retained request and ledger
record, including its owner, authority, payload hash and process identity. All
four admitted attempts must have verified cleanup, no unresolved liability and
exact 30/30 reservation/charge with `estimated_upper_bound` provenance. The
independent numeric oracle checks both the receipt output and saved application
result. The owned container must be explicitly absent at the reviewed endpoint.
A generic, partial, zero-charge or unknown receipt cannot establish PASS.
The ledger must contain exactly the two planned 60-CPU-second projects, with no
additional authority populations, before dispatch and at final verification.
Each diagnostic sidecar must be a bounded regular file with the ledger's exact
size and SHA-256, and its stdout/stderr must equal the final receipt streams.
The final HTTP input and recipe hashes must equal the recorded prelaunch hashes,
and its recipe and condition must equal the fixed matrix, before settlement can
pass. Agreement between HTTP and export alone cannot establish that lineage.

## Controls and retained evidence

Before dispatch, queued cancellation and removal of a claimed run's editor
access must both produce cancelled application records, an unchanged empty
broker execution ledger and zero broker launch calls. These two refusal records
remain in the first project's public export. OIDC construction and finite-profile
restore remain refused. The access control is removed before `Worker.execute`;
this exercise does not qualify a late revocation race. Both exports must pass
bounded bundle verification and equal the captured HTTP project and public run
records, including status, accounting, lineage and metrics. Input and recipe
hashes must agree, and successful results must match the captured result bytes
and independent numeric oracle. Removed members cannot export.

The report starts as FAIL. Any exception or interruption is recorded before
server cleanup. PASS is written only after the exact nonempty required-control
set, four bound settlements, server shutdown, restore refusal, unchanged source
and dependencies, artifact hashes and the final 120-CPU-second ledger check.
Cleanup errors cannot turn a completed workload into PASS. Correlated ledger
charges are recorded before diagnostics or independent container-absence checks.
A later check failure retains the known charge, marks accounting incomplete and
keeps cleanup acceptance separate from the ledger's cleanup observation. Known
charges never decrease; a witnessed regression refuses final acceptance. An
interruption retains earlier charges, each counted intent and any entered
dispatch boundary, with unknown exposure explicit. The harness does not cancel,
reconcile or mutate a failed broker liability; uncertain state requires manual
review and is never refunded or replaced with a zero-cost success.

The output directory and ledger/checkpoints/receipts remain private. Only the
bounded public exports and a separately reviewed public acceptance summary are
candidates for publication. Hashes establish byte correlation, not independent
attestation of a provider or executable. Synthetic wiring/oracle unit tests do
not count as actual Docker execution.

The first synthetic oracle failure and its exact harness/test sources are
retained under [pre-review/first](evidence/2026-10-18/scoped-docker/pre-review/first/).
It exposed integer `1` being accepted in place of boolean `true`; the current
oracle requires literal true for every required control.

Candidate 01 review also exposed four qualification-checker defects: missing
receipt/diagnostic correlation, incomplete checks of the project budgets and
population, exports that could differ from captured results/accounting, and a
known charge omitted when independent absence verification failed. The unchanged
review witnesses are retained under `pre-review/independent-5fd7d44`,
`pre-review/root-export` and `pre-review/accounting-absence`. The revised checks
and synthetic regressions address these witnesses. These are qualification
checker findings, not findings of a production export or provider defect.
The Candidate 02 re-review then demonstrated that matching HTTP/export lineage
could differ from the prelaunch binding. Its unchanged witnesses are retained
under `pre-review/independent-09418f2`; the revised settlement comparison refuses
that mismatch after the first attempt while preserving its known charge.

## Boundaries that remain

All four source environments stay disabled. Existing evidence, accepted PR #46
worktree, Belay, parent pilot worktrees, broker source/binary and production
settings remain unchanged. This slice adds no v5 profile, runtime redesign,
capacity policy, quota, defaults, image promotion, accepted-state upgrade,
private/student data, live allocation or hosted-service admission. Browser
coverage is NOT RUN; any later browser observation may only reuse these same
four attempts and cannot launch additional work.

Available physical capacity remains UNKNOWN and the historical fifth-headroom
result remains FAIL. The original replay receipt and failed workload remain
failed evidence. [PR #32's later read-only observation](evidence/2026-10-17/replay-cleanup-reconciliation/README.md)
resolved that specific resource/accounting observation with a full conservative
charge; it did not turn the old workload into PASS or close the source promotion,
independent-human/second-machine, hosted or real-data gates. Any new unknown
cleanup in this exercise remains visible and unresolved.
