# SQLite and guard write protocol qualification

**Decision: GO for a bounded protocol proposal and disposable qualification;
NO-GO for quota admission or runtime protocol changes.** The finite logical
profile bounds records, but it does not bound write history. A held reader can
retain WAL while safety operations continue. Failed guard writes can accumulate
protected temporary files while persisted counters remain unchanged.

This proposal pins the unchanged application at
`e79a50c6074e3ecf579a5bc5b0732c473d62502d` and the unchanged active Outfitter pin
`5827c84e9952cc67b85e91ceff4282e0ead6923b`.
The [machine-readable contract](qualification/sqlite-guard/contract-v1.json)
and [disposable witnesses](qualification/sqlite-guard/qualify.py) identify exact
source bytes. Outfitter's storage-writer qualification is a separate component
assignment. This change edits no application or broker source, journal setting,
accepted store, rejected draft or parent worktree.

## What can be bounded now

| Writer or quantity | Current supported bound | Excluded from that bound |
| --- | --- | --- |
| Guard candidate serialization | At most 4 MiB before temporary creation | Allocation, metadata, old guard, earlier failed temps |
| One valid guard save | Current plus candidate at most 8 MiB, two logical regular files | Other writes in the same action, previous or future failed temps, SQLite |
| Full guard map | 4,096 ordinary plus one essential item; bounded IDs, fields and counters | 4,097 real admissions, repeated reuse or failure history |
| Authority records | The [finite profile](FINITE-AUTHORITY.md) row and encoded-field ceilings | Database pages, indexes, WAL, SHM, temporary files |
| Restart recovery batch | At most 20 queued/running records under the admitted queue invariant; each run at most 256 KiB | Engine page/transaction peak and imported or tampered state |
| Complete store or action | **UNKNOWN** | No finite bytes/inodes or physical-space claim |

The full-map fixture contains maximum-width valid fields, matching database
receipts and 4,097 synthetic items. Its observed current and temporary files are
reported separately from the enforced 4 MiB serializer ceiling. One completed
replacement does not pay for the next candidate. Prior failed temporary files
remain additional protected occupancy; no observed disappearance grants credit.

## Current write paths

A normal Store mutation can prepare a guard candidate, commit a separate started
SQL receipt, mark the guard effect, and commit business data with the terminal
receipt. Rollback and acknowledgement failures can trigger uncertainty-note
writes. One Store call is therefore not one save or one SQL commit. A numerical
whole-action bound needs the entire success and failure path, not a multiplier
chosen from one observation.

`Guard.resume` writes a FULL-synchronous startup barrier without advancing guard
counters. Borrowed essential operations can also commit repeated safety changes
without changing guard bytes. A failed initial guard save can leave the old
counter values and one more uniquely named temporary file on every attempt.
The 64-bit counters and finite intent population do not cap those histories.

`Store.connect` applies query-only after the existing connection setup.
`Store.snapshot` holds a read transaction across input/recipe/result file reads.
Guard observations and recovery backup create other connections. The guard's
writer lock does not register, expire or drain these readers. Open/close,
checkpoint and engine activity remain writers that a future protocol must own.

## SQLite facts and remaining proof

SQLite describes how long-lived readers can prevent checkpoint completion and
WAL reuse even while commits continue. Automatic checkpointing is a threshold,
not a hard WAL ceiling. Releasing the reader and completing a checkpoint is a
state transition, not proof that future readers or writes fit. The source-pinned
witness measures while a SELECT-established snapshot and keeper connection are
open, then performs a separate explicit checkpoint on that disposable fixture.
[SQLite WAL documentation](https://www.sqlite.org/wal.html).

`max_page_count` limits main-database pages. `journal_size_limit` applies to
retained journal/WAL size at reset or transaction boundaries, not every live
peak. `cache_spill` permits dirty pages to spill during a transaction. None alone
establishes a whole-store maximum. This qualification reads existing settings;
it installs no pragmas, page ceiling or checkpoint policy in production.
[SQLite pragmas](https://www.sqlite.org/pragma.html).

SQLite has temporary writer classes beyond the main database and WAL. Their
reachability depends on statements, plans, compile options and runtime behavior;
logical row limits do not supply their storage overhead.
[SQLite temporary files](https://www.sqlite.org/tempfiles.html).
File/parent fsync fault injection tests application acknowledgement boundaries.
It does not emulate SQLite VFS failures, physical disk exhaustion, power loss or
storage hardware guarantees. [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html).

Reports record Python and SQLite versions, SQLite source ID, compile options,
Python extension hash and observed settings. This is a runtime fingerprint, not
complete dynamic-library/VFS attestation. A future accepted engine build must
include the documented WAL-reset fix or a verified backport; current SQLite
documentation identifies 3.51.3 and later and specific backports. No engine or
CI runtime is upgraded in this proposal, and these sequential witnesses do not
qualify concurrent checkpoint/write races.
[SQLite WAL reset notes](https://www.sqlite.org/wal.html#walreset).

## Proposed finite protocol and decision gates

The following sequence is a design candidate, not an implementation or a claim
that all required bounds are known. It reuses existing authority IDs, operation
IDs, sequences and receipts; it introduces no alternative capacity ledger.

1. **Validate and classify.** Require the exact finite authority/schema and a
   qualified SQLite/VFS build. Inventory current files and protected failed
   scratch. Unknown ownership or an unsupported writer leaves the complete
   bound UNKNOWN. Preserve previous failures and charges.
2. **Own readers and writers.** A future coordinator must cover every Store,
   guard observation, export, checkpoint and maintenance connection. A controlled
   reader gap must be available before any claimed checkpoint-based reuse.
   Existing external or unbounded readers defeat that claim.
3. **Prepare a complete write allowance.** Serialize the guard candidate before
   effects; account for current guard, full candidate, protected prior scratch,
   exact SQL statement/row/field plan, engine transaction peak, and remaining
   required safety writes. A known record ceiling cannot substitute for the
   missing engine peak. UNKNOWN refuses the finite-bound claim.
4. **Publish intent and execute.** Preserve the existing candidate write, file
   sync, replacement, parent sync, started receipt and business/terminal receipt
   ordering. Each transition needs its own success/failure ownership. Commit
   acknowledgement loss remains unknown until existing exact proof and a valid
   durability barrier establish completion.
5. **Replenish only with proof.** Any reuse requires a controlled checkpoint or
   proven terminal replacement for that exact incarnation, with all relevant
   readers/writers accounted for. Matching inventory scans, a stopped caller,
   successful-looking rename or completed compute receipt do not establish it.
   Do not remove failed files or discharge the next replacement's liability.

Three substantive choices remain for root review before implementation:

| Decision | Why it changes behavior | Current position |
| --- | --- | --- |
| Reader ownership and checkpoint gate | Waiting for reader drain can block logout, revocation, cancellation or settlement; uncontrolled readers make indefinite safety writes incompatible with finite WAL history | No gate added; current safety behavior retained |
| Finite retry slots and exhaustion | Reusing a slot cannot overwrite an uncertain prior incarnation; exhausting slots needs an explicit preservation and safety policy | Unique failed temps retained; retry count UNKNOWN |
| Engine and transaction ceilings | A specific build, page/VFS protocol and complete temporary-writer proof are needed; observed samples cannot set a defensible page or disk budget | No numeric engine allowance selected; default WAL/FULL and other settings unchanged |

Until these choices and proofs are resolved, an implementer must not wire this
proposal into quota admission, silently stop safety controls, enable restore,
change journal mode/settings or delete prior state. A journal-mode alternative
would be a separate architecture decision, not a fallback in this protocol.

## Qualification and interpretation

Run from any directory with a new report destination:

```sh
python3 research-workspace/qualification/sqlite-guard/qualify.py --output /tmp/new-sqlite-guard-report.json
```

The harness accepts no state-root argument and creates only disposable fixtures.
It pins production files before running nine controls: held-reader safety WAL,
startup barriers, pre/post-commit acknowledgement loss, full-map replacement,
repeated failed temporary writes, post-rename sync uncertainty, nine independent
safety actions with a reader, borrowed essential writes, and a fresh-process
20-record recovery batch. Provider actions are simulated or absent. The safety
matrix verifies effects, but does not fill every row cap; #40's independent
full-cap safety checks remain separate evidence.

The [acceptance and retained failure record](evidence/2026-10-18/sqlite-guard/README.md)
records observed sizes and exact provenance. A passing test may demonstrate a
missing bound. The failed in-process restart assumption is retained alongside
its corrected fresh-process control. These witnesses do not implement the
proposed gate, retry slots, engine ceilings or storage admission.

The #39 frozen contract and #40 evidence remain unchanged. The fifth consumed
headroom witness remains **FAIL**, available capacity **UNKNOWN**, and
`quota_enforced:false`. Source candidates remain disabled; original replay and
cleanup evidence, later full conservative charges, and all identity/real-data
and operational gates remain intact. No live provider, source promotion,
accepted-store migration, restore enablement or deletion is authorized here.
