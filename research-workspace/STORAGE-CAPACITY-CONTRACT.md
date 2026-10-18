# Storage capacity qualification contract

This is qualification evidence for issue #29, not an admission implementation.
The complete storage envelope is **UNKNOWN**, `quota_enforced` remains `false`,
and the fifth headroom witness remains **FAIL**. No available-capacity value can
be computed from this incomplete coverage.

The proposed next slice is finite authority growth on a new disposable profile,
subject to the parent review below. Quota admission wiring remains **NO-GO**.
No runtime, accepted store, journal setting, admission default or deletion policy
changes are included here.

## Source and evidence boundary

[Contract v1](qualification/capacity/contract-v1.json) pins Waypoint
`7d5cf925ffacd16129c6851f1b3d4f8bc4c1a8a0` and Outfitter
`5827c84e9952cc67b85e91ceff4282e0ead6923b`, with source byte hashes and 17 writer
rows. Each row records the owner/incarnation, retained and temporary bytes and
entries, serialization, future growth, terminal proof and partial failures.
Symbols refer to that immutable source, not to an assumed future implementation.

The map covers Store, runtime, guard and recovery writers, plus bootstrap,
lifecycle locks and the pinned broker ledger, outputs, snapshots, watchdog and
recovery. It includes SQLite connection/close/checkpoint effects around apparent
reads. It does not certify complete transitive writer reachability, physical
allocation, Docker storage, other host writers or a portable SQLite/VFS envelope.
Every row therefore retains `whole_root_envelope: UNKNOWN`.

The executable controls call production writers in newly created temporary
fixtures. They reuse operation IDs, sequences and database receipts. The small
arithmetic model is process-local qualification code, never loaded by the
application and never persisted as another authority. The broker runner exports
Git objects into a disposable directory and uses existing pinned Go controls
with a simulated provider. It never builds in the supplied checkout.

[Captured observations](evidence/2026-10-17/capacity-contract/waypoint.json) and
[broker controls](evidence/2026-10-17/capacity-contract/broker.json) are local,
synthetic evidence. A passing qualification test can successfully reproduce a
current failure; it is not a quota acceptance result.

## Consumption and replacement rules

Count logical bytes and entries separately. Entries include the root, every
subdirectory, every regular file, lock, sidecar and unknown file. Physical
blocks, inode metadata and free disk space require an additional contract.

For a completed append, move its proven occupancy from remaining liability to
observed occupancy only after matching authority, operation ID, sequence,
resource identity, expected paths/content hashes and terminal receipt. Do not
subtract global observed growth. Unrelated files, partial writes, another
operation or an older incarnation cannot discharge that liability. A terminal
compute receipt alone does not establish storage ownership or reclamation.

A reusable replacement requires the current retained file **plus** the next
full temporary representation and its entry. The old generation is already
observed occupancy; it cannot pay for the next temporary file. File fsync,
rename and directory fsync are separate boundaries. Failure before rename can
retain both files; failure after rename can leave a visible new generation
without proven durable completion. Failed guard temps remain protected. No
credit is granted from a missing observation, a cleanup attempt, an observer
exit or a future plan to delete files.

The desired inequality in both dimensions is:

`observed occupancy + remaining owned growth + next new growth + remaining safety growth <= policy limit`

Any UNKNOWN operand refuses a modeled admission decision. The inequality is
necessary but is not sufficient without complete writer coverage, serialized
ownership checks and a proved bound on each transition.

## Connected controls and limits

| Control | Evidence classification | What it establishes |
| --- | --- | --- |
| Actual `Store.submit` input/recipe append, matching operation and hashes | Modeled invariant | Correct completed append consumption; exact-fit bytes and entries accept; one byte or entry short refuses. Wrong incarnation, missing terminal proof and altered content receive no credit. |
| Unrelated orphan added after that append | Modeled invariant | Unknown occupancy increases total usage and does not reduce another writer's remaining growth. The file is preserved. |
| Partial input write through production `private_write` | Current witnessed failure | One retained byte, uncertain submit operation and started receipt; zero consumption credit. |
| Actual safety retries reaching `Guard.save` | Modeled invariant | Old and new files coexist at rename; each later generation needs fresh scratch. No whole-root bound follows. |
| Injected guard file fsync and post-rename directory fsync failures | Current witnessed failure | Protected failed temp or visible but uncertain generation; no reclamation credit. Host crash/power loss is NOT RUN. |
| 24 actual sign-ins with a held SQLite reader | Current witnessed failure | 24 new session rows, one reusable essential slot, unchanged ordinary count and growing WAL. No finite session/WAL peak is proved. |
| Two concurrent real `Store.add_user` transitions competing for the last guard slot | Enforced bound | Exactly one commits under the existing guard. The fixture narrows the metadata constant; no byte/entry quota is enforced. |
| Pinned broker ambiguous creation, cleanup failure, absent watchdog, failed diagnostic write and recovery controls | Enforced bound on uncertainty and compute settlement | Unknown liability/charges remain and provider effects are controlled. Real detached watchdog ownership and storage peaks are NOT RUN. |
| Actual guarded restore intercepted at its copy call | Current witnessed failure | Production creates the destination and held config without a capacity check. The fixture stops before copy; the source is unsealed. UNKNOWN full peak refuses in the offline model only. Enforced pre-copy/pre-seal quota refusal is NOT RUN. |

The existing
[fifth failed headroom witness](evidence/2026-10-17/storage-admission-blocked/observation.json)
is preserved byte-for-byte. It records an accepted exact-fit model followed by
one byte of protected temporary growth causing an essential refusal. The new
append model explains the missing ownership distinction; it does not repair or
replace that failed witness or prove recurring replacement headroom.

## Remaining writer obligations

SQLite is not a single bounded JSON file. Production uses WAL and FULL
synchronous writes, including during guard resume. A reader can prevent WAL
checkpoint progress. `max_page_count` concerns the database page count and does
not establish a combined database/WAL/SHM/replacement ceiling. Before asserting
one, pin the engine version, compile options, VFS/filesystem, page settings,
read-transaction lifetime and checkpoint behavior, then qualify the exact
schema and all transitions. [SQLite WAL documentation](https://www.sqlite.org/wal.html),
[SQLite page-count pragma](https://www.sqlite.org/pragma.html#pragma_max_page_count).

The guard limits each encoded file to 4 MiB, and metadata to 4096 ordinary slots
plus one essential slot. Those limits do not bound session rows, viewer-member
inserts through the essential path, repeated safety commits or accumulated
failed temps. Sequence/generation overflow needs pre-write refusal with safety
semantics; current read validation is not an overflow admission protocol.

Outfitter serializes ledger/output replacements under the ledger lock, with
96 KiB compact records, a 64 MiB ledger and 6 MiB + 512-byte encoded sidecars.
Output spills precede final compact-record validation and can become orphans.
Go JSON escaping differs from Python canonical JSON. Neither a raw output cap
nor the active-run limit proves the total file-writing envelope.

Broker snapshots and watchdog readiness writes occur outside that ledger lock.
A refused snapshot can retain 16 MiB + 1 byte after an input grows during copy,
and its 1024 walked entries exclude the snapshots parent and request directory.
Detached watchdogs can outlive the observer and retry cleanup. Unknown ownership
cannot be replaced by a fixed concurrency multiplier or a released reservation.

Current recovery inventories files, not every directory or unknown root file;
broker checkpoints include ledger and referenced outputs, not every orphan,
snapshot or ready file. Those manifests cannot certify whole-root migration.
A future destination reservation must cover copied material, database and guard
rewrites, broker restore, held-state metadata and cutover scratch before any
copy, then be revalidated before the first source seal. Both roots remain
charged throughout uncertain cutover. External checkpoints require their own
capacity policy.

## Next finite-authority proposal

**Conditional GO for a separate bounded authority-growth slice, after parent
review of this contract; NO-GO for admission wiring or quota claims.** The next
slice may implement finite logical state/serialization and qualification only.
It must not imply that SQLite or guard replacement headroom is solved.

1. Separate new sign-in admission from safety actions. New key sign-in creates
   authority state and needs a finite live-session policy. Proposed disposable
   profile limits are 256 total live sessions and eight per subject, with the
   existing eight-hour expiry. Counts must be checked inside the current guard
   and SQLite transaction. Expired-session deletion already exists; do not add
   pruning of evidence or unknown files. Logout, revocation, cancellation and
   conservative settlement need reserved safety behavior, including at limits.
2. Qualify every schema insertion and encoded field before effects. Proposed
   fresh-profile ceilings are 128 users, 20 projects, 1024 membership rows and
   1000 recorded runs total, retaining the tighter existing per-project/queue
   checks. A viewer membership that creates a row is new admission. Bound
   UTF-8 paths, escaped JSON, config, request, receipt, recovery metadata,
   sequence/generation and every diagnostic field. Candidate config and
   recovery records can use 65536 encoded bytes to match existing readers;
   choose request/receipt limits only from serializer measurements and maximum
   valid schema values. These numbers are review proposals, not defaults or
   approved capacity allocations.
3. Establish a finite SQLite and guard replacement protocol before any quota
   claim. Measure maximum pages/index growth at the candidate schema limits;
   qualify reader/checkpoint overlap and safety writes at the boundary. Do not
   change journal mode or accepted stores to make a fixture pass. If WAL growth
   cannot be bounded under the reviewed reader/commit protocol, retain UNKNOWN
   and stop admission implementation. Guard writes need pre-reserved current
   and replacement generations plus a finite retry/failure policy. Arbitrarily
   many new failed temp names cannot fit finite headroom. Reusing a slot must
   prove its exact prior incarnation terminal; unknown files cannot be removed
   or overwritten. If exhaustion prevents a safe durable transition, preserve
   uncertainty and keep new work held rather than promise indefinite retries.
4. Review that finite-authority slice independently, including held readers,
   maximum encodings, counters, failures at every durability boundary, and
   repeated safety operations at full admission capacity. Only then propose
   complete broker writer ownership, orphan accounting and pre-copy/pre-seal
   destination envelopes. Run competing final byte/entry admissions against
   actual enforced limits before claiming quota readiness.

The parent owns the next go/no-go decision. This PR authorizes no follow-on
admission implementation. Source candidates remain unavailable; failed replay,
unresolved cleanup evidence, conservative charges and revocations remain
unchanged. Accepted stores, the rejected draft, local Docker baseline, Belay
and parent worktrees are outside this qualification.

## Reproduce

From `research-workspace`, with Python 3.12+ and Go available:

```sh
python qualification/capacity/qualify.py --output /tmp/new-capacity-report.json
python qualification/capacity/qualify_broker.py --repository /path/to/outfitter --output /tmp/new-broker-report.json
python -W error::ResourceWarning -m unittest discover -s tests -v
```

Both report paths must be new. Fixtures are disposable and never accept an
existing state path. The source hashes must match; a changed writer requires a
new reviewed qualification contract. Repeated reports include fresh operation
identities and measured engine bytes, so they are not byte-identical artifacts.
