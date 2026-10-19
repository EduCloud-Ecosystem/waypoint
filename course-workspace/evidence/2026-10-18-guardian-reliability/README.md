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
  scan passed. Scanning these preserved reports additionally flagged 12 generic
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

## Scope and next gate

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
