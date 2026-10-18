# Storage observation and retention review

`storage-status` and `retention-plan` are bounded, read-only operator reports.
They do not enforce a quota, reserve space, migrate a store, delete files or
authorize deletion. Every report states `quota_enforced: false` and
`deletion_authorized: false`. Whole-store admission remains open in
[issue 29](https://github.com/EduCloud-Ecosystem/waypoint/issues/29).

With the workspace service and other operator writers stopped, supply its
canonical directory without symbolic-link path components:

```sh
python -m research_workspace storage-status --state /absolute/private/state
python -m research_workspace retention-plan --state /absolute/private/state
```

Reports go to standard output. Save them outside the observed state if desired.
They contain relative paths, sizes, hashes and reasons, not credential-bearing
file contents or database rows. They are private operational observations unless
their entire source is separately approved synthetic/public data.

## Inventory boundary

Logical bytes are the sum of regular-file lengths. Filesystem entries count the
root itself, every directory and every regular file. SQLite, WAL/SHM, broker
authority/output, lock files, temporary files and unknown/orphan files all count.
Directory allocation, filesystem block overhead and sparse-file allocation are
not inferred from logical bytes. Docker images, identity-provider volumes,
external checkpoints and replay outputs are outside this root and require
separate accounting.

The scanner uses directory descriptors and refuses symbolic links, hard-linked
files, special files, excessive nesting, growing/replaced files or a changing
directory. It makes two inventories and compares content and filesystem identity
metadata. This is an observation of a stable interval, not a lease against later
operator changes. A fingerprint is an integrity reference, not an authenticity
proof or a deletion capability.

The observation ceiling is 128 MiB of logical file content, 16,384 entries,
64 MiB per file and 16 directory levels. These are finite scanner limits, not
the workspace's enforced capacity. Refusal returns `complete: false`, observed
lower bounds and zero review candidates, with a nonzero CLI exit status. Missing,
unreadable or unscanned storage is never treated as available allowance.

The report opens an existing `server.lock` read-only and attempts its exclusive
lock without waiting. A busy lock refuses the observation. A missing lock is
reported without creating one; inventory is still possible, but retention
classification is unavailable. Lock replacement invalidates the observation.
Other operator writers must remain stopped because legacy mutation commands do
not yet participate in a whole-store coordinator.

## Retention boundary

The command never opens the source SQLite database through SQLite and never
calls the broker. Nonempty WAL or rollback-journal files prevent classification;
the tool neither ignores those changes nor checkpoints them. The inventory still
counts and protects them. Reconciliation remains an explicit operator task.

For a checkpointed base database with an existing offline lifecycle lock, the
tool deserializes bounded copied bytes into memory. It changes only the copied
header's journal-format bytes as documented by
[SQLite](https://www.sqlite.org/c3ref/deserialize.html). It checks the exact
supported schema including primary/unique constraints and indexes, rejects duplicate
identities, refuses views/triggers/virtual tables, validates
database integrity, keeps temporary operations in memory and bounds query work
and returned rows. SQLite builds forcing disk temporary storage are refused.
Unsupported features or uncertain authority protect every
artifact; there is no disk fallback or automatic repair.

A file becomes a **review candidate** only when it is an exact input, recipe or
result artifact whose recorded hash matches, from a successful fully settled run
in an explicitly closed project. Workspace and broker records must agree on
project/run identity, project budget, owner, runtime, status, cleanup and
conservative charge. Full receipt validation and hash-checked broker output must
agree with the accepted result.
A candidate still needs a human retention decision and recovery review. It is
never automatically safe to delete.

Authority, credentials/revocations, directories, broker records, failed or
unresolved work, active projects, unknown files and orphans remain protected.
There is no deletion command or scheduler.

## Observed verification

The retained [stopped synthetic pilot observation](evidence/2026-10-17/storage-inventory/observation.json)
counted 163,779 logical bytes and 82 filesystem entries, with zero review
candidates. Content hashes and captured identity metadata matched before and
after both commands. Read access may update access timestamps. This evidence is
local synthetic data and does not establish host capacity or hosted readiness.

## Dependent admission work

This foundation intentionally installs no new policy into accepted pilot stores.
The proposed defaults for a future fresh synthetic store are 64 MiB logical bytes
and 4,096 entries. They are protocol values, not measured host capacity.

The complete admission change must cover every writer under a consistent lock
order: offline lifecycle lock, storage coordinator, SQLite transaction, then
identity publication lock. Before file/provider effects, durable idempotent
intents must reserve complete remaining growth, atomic replacement peaks and
derived authority/settlement headroom. Broker execution must release storage
and database locks so revocation, cancellation, logout and settlement remain
possible at full admission.

Uncertain commit/fsync/rename, orphan input, result-before-commit, broker-init and
worker-death outcomes must retain their reservations and stop dispatch visibly.
Restart cannot manufacture fresh storage/compute allowance or replay work.
Both broker call paths need incrementally capped output reads. Checkpoints,
held restores and explicit migrations must retain policy, reservations and
protected artifacts, with destination preflight before copying and before source
sealing. External backup/replay outputs need their own peak-capacity admission.
Physical free space is an additional signal, never a guarantee against other
writers.

Hosted capacity, manual screen-reader/focus-order and new human acceptance,
private/student data, operational ownership and source promotion remain open.
The four source environments stay disabled; original failed controls and cleanup
evidence are unchanged. Belay and parent pilot worktrees are outside this work.
