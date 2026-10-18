# Scoped SQLite ownership

The disposable v4 profile gives each SQLite connection and cursor a finite
application scope. Copied, bounded query results can outlive that scope; live
connections, cursors and transactions cannot be returned through Store's public
factories. Unconfirmed cleanup holds subsequent work for that resolved state
root in the same process. A hold also refuses safety actions; it does not claim
that access was revoked, an observer was stopped, compute debt was discharged,
or storage became available.

This is the narrow implementation of the reviewed
[proposal](SQLITE-OWNERSHIP-PROPOSAL.md). It inherits the v3 logical row limits,
bounded export and publication policy. It adds no checkpoint/drain admission
policy, quota, capacity budget, journal repair, deletion, retry, pooling or
automatic hold reset.

## Select a fresh disposable profile

Add `--scoped-sqlite` to the `research_workspace init` command in the
[README](README.md). Use a new empty state directory, public/synthetic data and
the existing reviewed images. The flag is mutually exclusive with
`--finite-authority`, `--bounded-export` and `--bounded-guard-publication`.
Default initialization and profiles v0–v3 retain their existing behavior.

The explicit v4 binding is:

| Binding | Value |
| --- | --- |
| Mode | `local-finite-public-data-v4` |
| Policy format | `educloud-finite-authority-v4` |
| Guard format | `educloud-operation-guard-v5` |
| SQLite user version | `0x45464134` |
| Canonical policy SHA-256 | `ed235fd6d6bb3c10632c0c5dc2bb20c8d3cb9076ebb157c71c1cc9c8072a656e` |

There is no adoption, migration, OIDC, restore or activation path for v4. A
missing database or mixed binding is refused. If the config binding was
stripped, the legacy read-only discriminator may identify the v4 database only
to reject it; that exception never admits business reads or recreates files.
Archive inspection similarly uses an immutable, rejection-only offline probe.

## Connection and result lifetime

Online readers acquire the operation guard before opening an existing database
with `mode=ro` and `query_only`. Binding/schema checks and result materialization
share one read snapshot. SQLite evaluates registered type, row and byte limits
before payloads are copied into Python. Keyed lookups check uniqueness; a queue
with two eligible runs still permits one bounded claim.

Writers use distinct roles for initialization, mutations, started receipts,
barriers, final dispatch and offline backup. Existing writable connections
inspect WAL mode without changing it, and use connection-local FULL synchronous
and foreign-key settings. Only fresh initialization establishes WAL. There is
no immutable online reader, raw connection factory, or nested business/barrier
scope. Schema-reference databases have their own owned memory scope; the
legacy memory helpers remain unchanged for v0–v3.

An owner is single-use and belongs to its creating thread. Re-entry, reuse,
wrong-thread entry/exit and exit before entry are refused before changing scope
state. Proof, discovery, schema-reference, receipt and barrier roles accept
explicit complete statements. Joins, unions and subqueries cannot expand those
roles into business reads or writes.

The final broker spawn occurs inside the dispatch reservation. Artifact reads,
ZIP generation, HTTP streaming, broker collection and provider waits occur
after their SQL scopes end. A late authority check still precedes dispatch and
export publication. If a close fails after spawn, observer cleanup runs after
SQL and guard scopes unwind; the run remains uncertain.

Each cursor close, transaction rollback and connection close is attempted at
most once. Cleanup attempts continue after an earlier failure. The original
operation error remains primary, with additional cleanup failures recorded as
notes. Catching a cursor-close error inside a scope cannot turn its exit into
success. A successful local close is an acknowledgement from that API, not
proof that another connection, process, SQLite reader or writer has ended.

## Unconfirmed ownership and safety actions

The hold is shared by all threads and Store instances for the same resolved
root. Other roots are independent. Status reports
`sqlite_ownership_unconfirmed` without reopening SQLite. Constructors, status
checks and automatic worker recovery never clear the hold. There is no in-process
reset operation. A new process has a new in-memory registry; that fact does not
establish safe recovery or permit ignoring retained uncertain intents.

An existing operation token has one bounded diagnostic allowance, consumed
before its first diagnostic open. It can attempt the original operation's
uncertainty note using guard-proof/schema-reference reads. It cannot create a
new intent, open business SQL, run a barrier or recursively retry a failed note.
Untokened startup and barrier failures stay held without inventing an intent.

At clean, full logical row and intent limits, logout, revocation, key rotation,
membership removal/demotion, cancellation, closure, settlement and recovery
retain their existing essential path. After an ownership failure these actions
visibly refuse. HTTP logout returns a failure and retains its cookie; failed
revocation and settlement do not claim that access ended or liabilities cleared.
Preserve the original state and failure evidence for operator investigation.

## Offline backup boundary

The first phase retains the source freeze while separately owned source and
destination connections perform SQLite backup. Its guard, freeze, source,
destination and memory-reference owners unwind before broker/provider waits.
The second phase reopens the source, compares its fingerprint, and prepares the
manifest. SQL cleanup must succeed before the final manifest is written; the
operation guard and lifecycle lock remain held through publication.

This preserves the copied snapshot and fences cooperating application writers.
It does not fence raw external SQLite writers after the second freeze ends.
No final manifest is produced when either phase reports SQL cleanup failure;
partial output remains available for investigation. The backup command does not
constitute a SQLite checkpoint/drain or storage-capacity admission policy.

## Qualification and retained limits

Run the normal suite from `research-workspace/`:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -v
node --check static/app.js
```

For a separate, fresh synthetic qualification directory:

```sh
python3 qualify_sqlite_ownership.py --output /absolute/new/qualification-directory
```

The harness freezes its source and hashes, runs v4 controls and tests deliberately
broken variants. Its evidence covers application failure injection, real
two-process contention, both #43 regressions, full logical limits, bounded
export and offline owner ordering. It does not establish power-loss/VFS
behavior, provider cleanup, Docker execution of v4, physical capacity or global
quiescence. The unchanged standard CI Docker job qualifies its existing profiles.

The historical #39/#41 archives and failed fifth-headroom witness remain intact.
Available capacity remains **UNKNOWN**, `quota_enforced` remains **false**, the
source environments remain **disabled**, and the original cleanup gate remains
**unresolved**. Workflow pins, default behavior, Belay and parent pilot worktrees
are outside this increment.
