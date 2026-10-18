# SQLite connection ownership proposal

**Decision requested: approve a fresh opt-in connection ownership and read-scope
increment. Runtime implementation remains NOT RUN until coordinating design
review. Checkpoint gates and WAL or whole-store quotas remain NO-GO.**

The new profile would give every application SQLite connection and cursor a
named owner and a definite success boundary. Ordinary reads would open an
existing database read-only, validate its binding, and return bounded copied
values only after cleanup succeeds. Mutation and durability-barrier owners would
retain their existing authority ordering. Offline backup would have a separate
owner that preserves its frozen consistency boundary.

The inspected base is accepted PR #44,
`2317eac63a0bad0d691457b0cde2de14bf6bcc7f`. Its application modules are identical
to accepted PR #43, `5f2b21f1535702f9c18592b15e61c4c347d93558`.
This proposal changes no runtime, tests, workflow, broker pin or accepted state.
The coordinating chat owns standard Docker CI broker adoption separately.
All four source environments remain disabled. The original failed replay and
unresolved cleanup gate remain documented in
[the retained boundary](FINAL-DISPATCH.md); this proposal does not resolve them.

## Proposed fresh binding

| Binding | Proposed value |
| --- | --- |
| Profile and CLI opt-in | Version 4, `--scoped-sqlite`, mutually exclusive with the existing finite, bounded-export and bounded-guard-publication options |
| Configuration mode | `local-finite-public-data-v4` |
| Logical authority format | `educloud-finite-authority-v4` |
| Guard binding format | `educloud-operation-guard-v5` |
| SQLite application ID | Existing `APPLICATION_ID`, unchanged |
| SQLite user version | `0x45464134` |
| Ownership protocol | `educloud-scoped-sqlite-v1` |
| Inherited contracts | Exact v3 logical limits, `educloud-bounded-export-v1`, and `educloud-bounded-guard-publication-v1` |
| Canonical whole-policy SHA-256 | `ed235fd6d6bb3c10632c0c5dc2bb20c8d3cb9076ebb157c71c1cc9c8072a656e` |

[The proposed policy](design/sqlite-ownership/proposed-profile.json) is an exact
candidate, not a registered runtime version. Its digest uses the existing
canonical JSON encoding. Its readable file SHA-256 is
`053a1846ea2b4d57054565c122181f4a33f38b5ccb478315dc7720241b394f8c`.
The authority ID remains a fresh random value bound across config, guard and
SQLite. No table migration, automatic adoption, old-policy rewrite or partial
version fallback is permitted. Versions 0–3 keep their existing paths. Finite
OIDC, restore and activation remain refused; v4 continues to require the complete
authorized export operation instead of the old `snapshot` interface.

## Current readers and writers

The [mechanical inventory](design/sqlite-ownership/connection-sites.json) records
44 production call sites: 42 connection/factory calls and two backup calls.
These are source locations, not a maximum simultaneous connection count. Line
references below are against the inspected base; the
[provenance manifest](design/sqlite-ownership/provenance.json) binds
the exact files. An owner must cover implicit cursors created by `execute` and
`executescript`, not only explicit `cursor()` calls.

| Current owner or caller | Current lifetime and proposed v4 treatment |
| --- | --- |
| `Store.__init__`, `store.py:124,170` | Fresh schema construction and an existing-store format probe. Separate create and discovery owners; close the probe before constructing usable Store authority. Normal v4 bootstrap acquires the existing guard before opening SQLite. |
| `Store._connect/connect`, `store.py:202,228` | `_connect` opens writable/create-capable, sets WAL/FULL, and returns a raw connection; `connect` sets query-only afterward. Replace v4 entry points with scoped owners. Existing reads use `mode=ro`; existing writers use `mode=rw`. Neither can create a missing database. |
| `Store.transaction`, `store.py:241` | Admission preflight, started receipt, business transaction, commit/rollback and close. Separate preflight reader and mutation owner. Retain token lifetime, effect checks, receipt ordering and error uncertainty; finish cleanup before returning a result. |
| Session lookup, project list/detail, run detail, `should_cancel`, `store.py:338,390,472,584,657` | Some multi-query reads currently use connection scope without an explicit common snapshot. Use one short read transaction for each logical observation, bounded typed projections and detached results. Authentication remains current at each existing decision boundary. |
| Project replay/admission, membership classification, submit replay, queued claim probe, `store.py:423,479,522,614` | Guard-protected preflights and replay reads. Materialize and close before admitting the next owner. Read classification does not become cached mutation permission. Preserve in-transaction rechecks. |
| `Guard.validate_schema/_receipts/queued_ids`, `operation_guard.py:286,303,385` | In-memory schema reference plus independent existing/read-only observations. Own both kinds explicitly. Validate binding on every v4 observation, including queued IDs; return detached receipts/IDs. Avoid recursive proof validation. |
| `Guard.resume/barrier/begin`, `operation_guard.py:342,368,465` | Startup/takeover barrier and started-receipt writes use raw factory callbacks. Replace v4 callbacks with role-specific owned transactions. A takeover barrier remains forbidden inside a live business SQL transaction. |
| Worker request preparation and final launch, `runtime.py:208`, `store.py:668` | Request preparation qualifies an intervening publication before its external effect. Final spawn stays inside guard → SQLite write reservation → local authorization fence. Rollback/close happen before provider waiting. |
| Final settlement, `store.py:730` | Result file publication and terminal SQL receipt remain in the existing mutation fence. The run token and settlement token retain separate roles. Do not shorten this write fence under a reader refactor. |
| `bounded_export._capture/export_bundle`, `bounded_export.py:50,160` | Capture already ends before artifact/ZIP work and final authority is recaptured. Route each capture through the common read owner; preserve final comparison and session expiry checks under the guard. |
| Offline freeze and backup, `recovery.py:113,196`; `guarded_recovery.py:15` | Lifecycle lock, frozen writer connection, separate backup source and destination, copied files and fingerprint comparisons. Use a distinct offline owner; preserve this longer consistency boundary rather than applying the online short-read rule to it. |
| Restore/cutover, `recovery.py:264,294`; `guarded_recovery.py:55,117,125` | Existing legacy writers and recovery receipt paths remain unchanged. V4 refuses before destination/provider effects. The finite-recovery rejection probe is an owned offline read of the verified archive. |
| `finite_authority.refuse_unqualified_recovery`, `finite_authority.py:171` | Existing immutable archive probe is a separate role. Never transfer `immutable=1` to an online database. |
| `Identity._active_subject`, `identity.py:140` | Legacy OIDC read remains unchanged and must stay outside network waiting. V4 does not enable OIDC or route online v4 authority through this legacy connector. |
| `storage_inventory._supported_schema/_authority`, `storage_inventory.py:40,182`; `recovery.database_digest` | In-memory reference/deserialized observations and bounded offline fingerprinting. Give memory connections/cursors explicit owners where shared by v4 validation. Preserve the existing offline inventory contract and frozen qualification code. |

Fresh creation is the only online role allowed to create a new database. It
retains the current WAL initialization, FULL synchronization and schema/binding
order in a fresh exclusive directory. Existing writer opens verify WAL and use
the existing connection-local FULL/foreign-key settings; they do not switch
journals as a repair. Read opens set query-only and inspect the required binding
without setting journal mode, running a barrier or qualifying a mutation.

SQLite URI modes distinguish read-only, existing writable and create-capable
opens. `immutable=1` skips locking and change detection, so it is unsuitable for
mutable online authority. [SQLite URI documentation](https://www.sqlite.org/uri.html).
Read-only WAL access can still create or use WAL/SHM sidecars when the directory
permits it. Normal open, commit or close can also trigger SQLite-managed work;
this design does not claim zero filesystem writes from a reader.
[SQLite WAL documentation](https://www.sqlite.org/wal.html).

## Owner interface and lifecycle

Introduce one internal ownership module with explicit roles: discovery probe,
fresh initialization, online read snapshot, guard proof read, started-receipt
write, mutation, durability barrier, dispatch reservation, memory reference and
offline backup. Owners are per operation, not pooled or shared across threads.
An internal owner records its role, thread, parent owner, connection, cursors,
transaction state and cleanup outcome. This bookkeeping is not a durable ledger
or a count of all SQLite users on the machine.

Owners progress through opening, validation, active work, transaction ending and
close. Only complete success may publish a returned value. On any exception,
cleanup is attempted once for each acquired resource in dependency order;
ownership records are retained until the outcome is known or quarantined. An
invalidated façade cannot issue further statements, even if retained by a caller.
Python's connection context manager commits or rolls back but does not close a
connection, so `with connection` alone cannot implement this contract.
[Python sqlite3 context manager](https://docs.python.org/3.14/library/sqlite3.html#how-to-use-the-connection-context-manager).

The v4 data-access façade exposes bounded `one`, `many` and scalar observations,
and approved mutation statements. Each observation uses an owned cursor,
materializes values and closes that cursor before returning. No connection,
cursor, cursor iterator, lazy generator, backup iterator or `.raw` handle is
returned to business callers. Internal receipt completion accepts the mutation
owner capability, not a publicly accessible raw connection. Existing profiles
retain `TrackedConnection`; v4 uses a separate adapter so their behavior is not
silently changed. Statement names and query bounds are internal constants.

Do not infer statement safety from the first SQL word. V4 read façades admit
only reviewed SELECTs and a narrow metadata-PRAGMA list. Transaction control and
writes belong to their respective owners. No new ATTACH, extension, arbitrary
SQL, pool, cache, retry or maintenance interface is introduced.

### Bounded read results

Validate types and encoded lengths inside the same snapshot before fetching
payload columns, following the existing export `_rows` pattern. Fetch at most
the declared count plus one and refuse overflow before returning partial data.
Validate and copy to ordinary Python values; JSON parsing does not keep a live
cursor. Single-row queries check uniqueness rather than silently accepting a
second row. Scalar counts have integer/range checks.

| Query family | Result ceiling and field checks |
| --- | --- |
| Config and binding probe | Existing config 65,536-byte ceiling; fixed metadata projection, one authority row, exact schema and version |
| Schema metadata | At most 100 records; type ≤8 bytes, name/table ≤80 bytes, SQL ≤8,192 bytes or NULL for implicit indexes, followed by exact normalized schema comparison |
| Session, user, membership or one project/run lookup | At most one matching row; subject/IDs ≤80 UTF-8 bytes, key/token/request hashes ≤64, role ≤8; finite session expiry and valid integer flags |
| Project list and budget observation | At most 20 project records, each encoded payload ≤32,768 bytes |
| Project detail | At most 1,024 membership rows and 100 run records; run payload ≤262,144 bytes, status ≤32, request key ≤80 when present |
| Queued/running work | At most 20 records under the existing admitted queue invariant; claim probe at most one; apply run field limits before materialization |
| Complete finite offline database fingerprint | Existing table ceilings: 128 users, 20 projects, 1,024 memberships, 1,000 runs and 256 sessions; bounded fields and strict schema before hashing |
| Guard proof | One authority row and at most 4,097 receipts; ID 32 hex, digest 64 hex, valid signed-64-bit sequence and enumerated outcome/essential flag |
| Export | Keep the accepted export row, field, file and archive limits and publication comparison unchanged |

These bounds constrain returned application data. They do not bound SQLite's
internal allocations, query time, temporary storage, pages, WAL or SHM. A failed
or oversized read returns no partial project, session or export result.

## Lock order and publication boundaries

Every admitted v4 online owner takes the application guard before opening its
database connection. After connection-local setup, a read owner begins its
transaction explicitly, then validates application/user version, authority ID
and schema in the same snapshot as its business observations. Config and guard
binding are checked within the guard scope. Writer owners begin their write
reservation before database validation. Each read cursor closes after bounded
materialization; all remaining cursors close before ending the transaction and
closing the connection. Only successful cleanup permits returning values and
releasing the guard scope. Failure still attempts application-lock cleanup.
Reads may inspect a valid held authority without obtaining mutation permission;
they do not call `Guard.check`, resume, or a durability barrier. A process-local
ownership failure is a separate refusal condition described below.

Mutation owners retain the existing `BEGIN IMMEDIATE`, operation publication and
SQL receipt sequence. Fresh receipt observations nested under a mutation are
permitted only as subordinate guard-proof readers and must close before the
parent continues. They see committed authority; no raw handle is shared with
the writer. A new mutation/barrier cannot nest inside business SQL. Validation
on an already-open owner must not recursively open another validated owner.

The worker's request-file preparation keeps #43's qualification under the guard
before the external effect. Final dispatch separately reacquires and qualifies
authority before opening its reservation, rechecks access/cancellation/expiry,
then spawns inside that fence. The child is observed and awaited only after
rollback and connection close. A post-spawn cleanup error retains uncertain
ownership and invokes the existing bounded observer cleanup; it is never
reported as a zero-cost refusal or retried launch.

Export captures close before file reads, ZIP construction and HTTP delivery.
Final recapture uses a new owned snapshot; the guard remains held through the
equality and expiry checks. Slow streaming, artifact reads and provider waiting
cannot retain an online SQL reader. Existing submission/result file writes
inside mutation transactions remain write effects, not accidental read scopes.
No guarantee of elapsed lock latency is added: SQLite, filesystem I/O and an
external process can still block. No reader-drain gate is introduced.

### Bootstrap and stripped bindings

Read bounded configuration and filesystem guard evidence first. A recognizable
v4 configuration requires the exact existing guard files; missing files cause
refusal without opening a writable database or recreating a lock. Valid v4
bootstrap acquires that existing lock before the discovery owner opens `mode=ro`.
The discovery owner returns only bounded format/binding metadata, closes, and
then permits complete guarded validation. Fresh initialization has its separate
exclusive-create owner and bootstrap authorization.

The legacy compatibility discriminator remains a narrow exception when all
configuration/guard identifiers have been stripped: its existing read-only
format probe may inspect only the database markers to reject a finite database.
It cannot admit v4, read business rows, qualify durability or fall through to a
write/create-capable connection. This is not an admitted v4 online read. Removing
this discriminator would permit downgrade or change legacy startup semantics;
its explicit rejection-only boundary requires coordinating approval. Negative
controls must exercise stripped config, guard, lock, policy and database markers
separately and together.

## Failure matrix

Preserve the first exception and annotate cleanup failures. An exception is not
proof that a statement, commit, close or open had no effect. Attempt every
independently owned close once, without retrying an ambiguous handle. Do not use
garbage collection, a missing file, a zero local owner count or releasing the
application lock as evidence of database quiescence or reusable storage.

| Boundary | Required result and retained state |
| --- | --- |
| Guard acquisition or binding failure | No business query/write; original failure preserved. Never recreate an existing authority or fall back to legacy. |
| Connection open raises without returning a handle | Failed operation; release outcome unconfirmed. Latch ownership failure for this authority in this process. No claim that SQLite allocated nothing. |
| Setup/schema validation fails after open | Attempt owned cursor/connection cleanup; no result. Binding refusal persists. Any unconfirmed cleanup latches ownership failure. |
| SELECT, bound/type check or materialization fails | No partial result; end the read and attempt each close once. Successful local close acknowledgement does not turn the failed read into success or establish global quiescence. No automatic retry. |
| Normal business denial with successful cleanup | Preserve the existing refusal, including logical full-cap rejection. It does not poison the process solely because access was denied. Safety controls remain available under their existing rules. |
| Cursor close, read rollback/end or connection close fails | Refuse result/publication, invalidate façade and retain owner with unconfirmed cleanup. Latch process-local ownership failure; do not reopen another normal owner as a repair. |
| Mutation or commit acknowledgement fails | Keep existing effect/receipt uncertainty, attempt rollback/cleanup once and preserve the original error. No false terminal/refused result and no discarded charge. |
| Close fails after acknowledged commit | Keep committed observations distinct from cleanup uncertainty. Use the existing origin's mandatory failure-note path once; success is still refused and the process stays held. |
| Failure-note observation or publication fails | Add the failure to the original error; keep the hold and residue. No borrowed essential marker, fallback role, repeated diagnostic writer or swallowed failure. |
| Takeover fails | No business transaction/request/provider effect. Preserve #43's publication hold and uncertain current incarnation. Never move the barrier inside SQL to make a test pass. |
| Dispatch reservation cleanup fails after spawn | Retain the child ownership and accounting uncertainty; unwind locks and invoke existing bounded observer cleanup. Never launch again or claim the child did not start. |
| Offline source/destination backup or close fails | No success report or manifest. Preserve partial output and original source; no deletion, adoption, retry or provider continuation justified by the failure. |

The proposed ownership hold is process-local and shared by Store instances for
the same authority root. It refuses further normal readers and writers,
including safety calls, before opening another SQLite connection. This is an
explicit availability tradeoff after an unconfirmed ownership boundary, not a
quota gate. A status endpoint may report the in-memory held reason without
opening SQLite. Retain quarantined handles privately; never hand them back to a
caller, pool or retry loop.

One narrow diagnostic exception preserves the existing failure protocol: if a
failed mutation already has an operation token, its original-role uncertainty
note may attempt the required fresh guard-proof reads once under the same guard.
It cannot open a business writer, create a new intent, run a barrier or borrow a
safety token. A diagnostic owner failure terminates that one attempt; it cannot
recursively initiate another note. Read/open failures without a token do not
invent a durable mutation merely to log an error.

Application-lock cleanup is still attempted; an unlock is not a claim that an
uncertain database handle closed. Other processes do not inherit this in-memory
hold: they remain subject to actual SQLite locks, exact persistent receipts,
guard residue and ordinary qualification. No cross-process quiescence claim is
made. There is no automatic reset/restart. An operator-started fresh process must
perform existing qualification, preserving persistent uncertainty; it cannot
certify that an older process has exited. This limitation belongs in the new
profile's operator documentation and tests.

## Offline backup ownership

The recovery command named `checkpoint` remains the existing offline backup
operation; this proposal introduces no SQL checkpoint calls. Preserve the
ordering: lifecycle lock → guard → existing writable freeze connection with
`BEGIN IMMEDIATE` → separate existing/read-only backup source → fresh owned
destination. Keep the source freeze and guard across the database backup, file
copy and fingerprint phase. Do not back up through the same connection holding
the write reservation or shorten this phase to an online read snapshot.

Close destination and source readers before ending/closing the freeze owner.
Release database and guard scopes before waiting for the broker; retain the
lifecycle lock. Reacquire the existing frozen boundary and compare fingerprints
before publishing the manifest. Failure in either phase preserves output without
a successful manifest. The destination can be created only inside the existing
fresh-directory protocol. Immutable reads remain confined to the verified
offline archive rejection path. Finite restore/activation still refuse before
destination or provider effects.

## Required qualification before any merge

All controls below are proposed and NOT RUN for this increment. Use disposable
stores and simulated providers; retain initial failures, exact sources, scripts
and logs. A source scan is supporting evidence, not a substitute for observed
ownership at runtime.

1. **Coverage and escape controls.** Instrument every listed open, cursor,
   transaction and close. Exercise bootstrap, all online reads, preflight,
   receipt proof, barrier, mutation, dispatch, export and offline backup.
   Reject cursor/connection/lazy-result escape and use after scope exit. Deliberate
   bypasses of the owner, a missing close and writable read-open must fail tests.
2. **Binding and bounds.** Exercise v0–v3 compatibility and exact v4 tuple;
   stripped/mixed bindings, missing database, symlinks/changed identity according
   to the existing filesystem contract, oversized or duplicate rows, malformed
   JSON/types, and every result limit. No failed read creates/reinitializes a
   database or returns partial data. Validate same-snapshot length/data checks.
3. **Two real processes.** Pause A after an actual SELECT establishes a snapshot;
   B attempts a safety action. Verify guard-before-database event order and B's
   effect after A's owned scope ends. A may then block in artifact/ZIP/HTTP work
   while B completes. Record observed waits without asserting a latency bound or
   reader gap for raw external SQLite users.
4. **Failed close and contention.** Inject open, setup, query, cursor-close,
   rollback/end, connection-close and guard-close failures separately and in
   combination. Preserve the first error, attempt each cleanup once, refuse
   success and test process hold/diagnostic recursion limits. A second process
   may encounter native contention; it must never infer A's release from its
   registry or reuse storage credit. No accepted store or Docker reset is used.
5. **Full-cap safety matrix.** Use a separate valid full-cap fixture for logout,
   revoke/rotate, remove/demote, cancellation, closure, settlement and restart
   recovery. Confirm effects and accounting at logical row/ordinary-intent caps
   without new growth or a read-drain/checkpoint gate. Separately verify that a
   real ownership failure visibly refuses safety rather than claiming success.
6. **Both #43 regressions.** Cancellation/revocation/closure/expiry before claim
   retain the token through final commit and close and allow later ordinary work
   on clean success. Cross-process unrelated publication qualifies before
   request preparation; another late cancellation prevents spawn. Takeover
   remains forbidden inside SQL. Commit/close failures preserve uncertainty.
7. **Export and dispatch publication.** Slow artifact/ZIP/HTTP work retains no
   online owner; final authorization changes refuse export. Final spawn stays
   inside its existing fence; provider wait and observer cleanup occur after
   database scopes unwind. Failure cannot turn a spawned run into not-admitted.
8. **Offline boundary.** Prove distinct freeze/source/destination owners, retained
   lifecycle ordering, no provider wait with a database scope, and the second
   fingerprint comparison. Inject partial backup and source/destination close
   failures; preserve output and refuse manifest. Finite restore still refuses.
9. **Frozen evidence.** Keep the #41 WAL-reader witness and archive unchanged,
   alongside #43 failures/fixes and all earlier evidence. Run their existing
   source-pinned harnesses only if separately requested; do not rewrite them to
   make this new profile appear to satisfy their historical claims.

The original #41 observation remains a counterexample: its held SELECT reader
coexisted with WAL growth beyond the automatic threshold. Short owned readers
do not prove the absence of other readers or establish a future WAL bound.
[Retained #41 evidence](evidence/2026-10-18/sqlite-guard/README.md).
The fifth headroom witness remains **FAIL**, available capacity **UNKNOWN** and
`quota_enforced:false`. No page ceiling, journal switch, `journal_size_limit`,
automatic retry, deletion, migration, capacity arithmetic, real-data admission
or source-candidate promotion is included. Raw SQLite SHM files can still occur.

## Review decision and implementation boundary

Coordinating review must explicitly accept the proposed v4 binding, rejection-only
legacy discovery boundary, process-local ownership hold and its single diagnostic
exception, bounded result inventory, and separate offline owner. Those choices
must not be inferred from successful local tests.

After approval, implement the owner and typed adapters, convert v4 call sites,
then qualify the full inventory and both original #43 regressions. Obtain
independent review and exact-head CI before conditional merge. Workflow/pin
adoption remains the coordinating chat's separate work. Until that approval,
this branch contains only the proposal, candidate policy, source inventory and
provenance;
accepted #43/#44 runtime and protected worktrees remain untouched.
