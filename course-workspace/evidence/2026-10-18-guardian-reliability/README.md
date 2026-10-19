# Local authorization and Guardian reliability

Executed October 18, 2026. The final local rehearsal passed **17/17 phases** at
`b88f4b5338299c4b3fd763a037e4109f4dcde4cf`, with owned-resource cleanup and final
source verification. This is synthetic local runtime evidence; no new cloud
pilot was provisioned.

## Diagnosis and changes

Concurrent replay requests reproduced ordinary SQLite writer starvation on a
controller capped at 0.5 CPU. A baseline eight-thread, 400-request measurement
had nine refusals; an instrumented repeat classified SQLite busy errors. The
public refusal alone did not identify the underlying cause. The old hosted
failure lacks those categories, so this reproduction does not establish its
exact cause.

Replay admission now uses a bounded FIFO before opening SQLite, with one wake
event per waiter and a shared one-second queue/SQLite contention budget. Each
request still performs fresh private-file, integrity, nonce and clock checks.
Failure releases admission after rollback/connection close. Nonce consumption
commits before fresh principal authorization and runtime dispatch. There are no
automatic retries or relaxed Guardian deadlines. Other processes retain SQLite's
bounded fail-closed behavior; filesystem sync is not a hard wall-clock deadline.

Enrollment checks previously took a SQLite writer and updated the clock floor
on every call. They now fully validate fresh read snapshots without a writer
when the persisted clock second is unchanged. When time advances, they close
the read transaction, acquire a writer and validate again. Rollback below either
the persisted floor or the first observed time is refused; revocation and
history remain fresh. A separate 400-call constrained measurement reduced maximum
enrollment check duration from about 964 ms to 96 ms, with zero errors in both measurements.
This is evidence of lower contention, not proof of hosted heartbeat causality.

Bounded diagnostics now distinguish replay stages/categories and WebSocket
timeout versus bad HTTP status, without exception messages, credentials or raw
logs. The rehearsal can explicitly select smaller service profiles while
recording actual daemon capacity separately.

## Observed validation

- **696 component tests passed**, including real-SQLite queue cancellation,
  rollback, external-writer refusal, duplicate nonce handling, fresh revocation,
  enrollment history corruption and clock rollback regressions.
- Rehearsal fixture suite: 45 tests, 44 passed and one skipped on macOS because
  it requires root Linux ownership. In the Linux course image the actual
  root/mixed-UID ownership check passed; 44 passed and one optional Docker-SDK
  test was skipped. The full runtime used the controller with Docker SDK.
- [Replay contention](REPLAY-CONTENTION.json): eight callers, 400 distinct durable
  nonces, zero refusals, maximum about 193 ms at the 0.5-CPU/256-MiB allowance.
  Reopening validated the full durable history. CI now repeats this measurement.
- [Final notebook run](local-notebooks-003-four-cpu/REMOTE-NOTEBOOKS.json): real
  mutual TLS, Python/R WebSockets, saved coursework, authority loss, interrupted
  transfer quarantine and reconciliation, stale-source refusal, worker loss and
  restart, Guardian loss, credential rotation, scoped revocation and cleanup.
- Independent read-only review found no blockers in replay admission,
  diagnostics, profile selection or enrollment freshness/rollback safeguards.
- Workflow lint, whitespace checks and the implementation commit-range secret
  scan passed. Scanning the original three notebook reports flagged 12 generic
  API-key candidates: the source SHA-256 fields for four authentication/credential
  modules in each of the three runs. Every value was verified against its
  recorded Git revision; these are code hashes, not credentials. No scanner
  rules were suppressed. This is not a repository-wide security certification.

## Preserved failed controls

| Attempt | Source | Result | Observation |
| --- | --- | --- | --- |
| [001](local-notebooks-001/REMOTE-NOTEBOOKS.json) | `ef45739` | FAIL, 14/17 | Automatic eight-CPU profile; R WebSocket failed after credential rotation. Old classification cannot distinguish timeout from HTTP rejection. |
| [002](local-notebooks-002-four-cpu/REMOTE-NOTEBOOKS.json) | `6bb6184` | FAIL, 3/17 | Selected four-CPU service caps; first notebook did not become ready. Early Guardian authorization/transport refusals occurred; captured replay refusal counters were zero. |
| [003](local-notebooks-003-four-cpu/REMOTE-NOTEBOOKS.json) | `b88f4b5` | PASS, 17/17 | Same four-CPU service caps after the enrollment change; coursework and unaffected peer survived the full failure/recovery sequence. |

Both failed runs verified cleanup. Their bounded, non-atomic post-failure
diagnostics are retained beside their reports. Raw private logs are not included.
[sha256.json](sha256.json) hashes the original report bytes; reports have not been
relabelled to the final commit.

## Enrollment follow-up and retained CI failures

The first PR CI attempt passed its worker-channel Python/R/Guardian rehearsal
and the separate twelve-phase authenticated runtime, but its following
enrollment rehearsal failed after a deliberate four-second lock hold
([failed job](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/39202337992)).
The original generic wait did not distinguish release, process exit, heartbeat
renewal or suppressed probe errors. Its diagnostic default also does not prove
that a probe raised. The exact failed conjunct and removal cause remain unknown;
the subsequent passing runs do not retroactively accept this failure.

The brief-hold waiter now logs only five fixed booleans and a fixed exception
category, fails on malformed/expired/late results, and does not swallow probe
errors. Before the hold, both learners must exist and retained bytes must still
be readable. The hold remains four seconds and its recovery wait ten seconds.
An explicit smaller-profile selector now allows the legacy authenticated and
enrollment fixtures' existing CI caps to be reproduced locally. Historical
schemas remain immutable; current reports separate the selector from actual
daemon capacity. Production runtime and enforcement files did not change in
this extension.

| Control | Source | Result | Scope |
| --- | --- | --- | --- |
| [Original CI runtime](enrollment-ci-001-failed/AUTHENTICATED-RUNTIME.json) | `76a4c6e` | PASS, 12/12 | Linux runner, automatic four-CPU fixture; job subsequently failed enrollment. |
| [Original CI enrollment](enrollment-ci-001-failed/ENROLLMENT-RUNTIME.json) | `76a4c6e` | FAIL, 2/10 | Brief-hold recovery wait; cleanup verified. |
| [Local automatic enrollment](enrollment-001-auto/ENROLLMENT-RUNTIME.json) | `307f8b9` | PASS, 10/10 | Actual eight-CPU ARM64 daemon, automatic six-CPU fixture; controller 1 CPU. |
| [Local selected-four enrollment](enrollment-002-selected-four/ENROLLMENT-RUNTIME.json) | `d713259` | PASS, 10/10 | Same actual eight-CPU daemon; selected controller 0.4 CPU, each Guardian 0.1, each Hub 1 and each learner 0.1. |

Both local enrollment runs were clean and verified cleanup. The selected-four
run observed held/unchanged, released/exited/unchanged, then renewed/not-expired
states before continuing through credential rotation, expiry, worker revocation
and saved-work recovery. This is a different fixture from the worker-channel
profile above; its full reservation is 4 CPUs. It does not establish physical
four-CPU capacity. All ten retained report files are covered by the manifest.

The extension passed 306 rehearsal helper tests (299 passed, seven environment
skips), followed by 13 targeted readiness tests after the final consistency
guard. Independent review cleared the propagation, privacy and deadline checks.
A second PR policy run separately exposed a half-second response-header timeout
in a durable ownership-plan TLS unit test
([failed job](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/39204289365)).
Those four durable-plan tests now use the normal three-second client budget;
explicit short socket/deadline tests and production settings remain unchanged.
The full 696-component suite passed again after this test-only adjustment.
The expanded evidence scan flagged 23 source-hash candidates in total; every
value was checked against its recorded Git revision with no rule suppression.
Final CI remains the merge gate.

## Remaining scope

The daemon was local Linux/ARM64 on macOS, with eight CPUs and about 8 GiB RAM.
The passing run selected the existing four-CPU profile: central 0.5 CPU, each
worker 0.5, each Guardian 0.25, each Hub 0.5 and each learner 0.1; total caps 3.8.
It did not isolate a physical four-CPU host or prove classroom capacity, and
other unrelated local containers remained running. Logical Hub ownership
reservations still exceed actual Hub caps on smaller profiles; see
[REMOTE-NOTEBOOKS.md](../../REMOTE-NOTEBOOKS.md).

This is one daemon with two logical workers, not independent-host acceptance,
encrypted off-host recovery or production readiness. The earlier eight-profile
failure remains unaccepted. Next: review/CI, then a separately authorized
synthetic target-host rehearsal with these diagnostics before any student use.
