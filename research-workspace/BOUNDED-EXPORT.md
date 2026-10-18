# Fresh profile with scoped export readers

`init --bounded-export` creates a fresh local-key profile with the existing
finite authority limits and a complete authorized export operation. It is a
separate opt-in from `--finite-authority`; the flags are mutually exclusive.
Supply the ordinary new state directory, broker, subject, immutable images and
compute budget. Existing initialization defaults and stores retain their prior
behavior. There is no migration command.

The [approved proposal](BOUNDED-EXPORT-PROPOSAL.md) records the contract and its
version choice. The new mode is `local-finite-public-data-v2`, policy
`educloud-finite-authority-v2`, guard `educloud-operation-guard-v3` and SQLite
`user_version=0x45464132`. The policy includes
`export_protocol: "educloud-bounded-export-v1"` and the unchanged row/field limits.
Its canonical digest, database marker, guard binding and sole authority identity
must agree. Missing or mixed markers refuse, including on an already-open Store
and safety writes. Older finite v1 remains independently supported. Coordinated
rollback of every binding is not detectable without an external trusted anchor.

## What export guarantees

The HTTP route passes the local session token privately to `Store.export_bundle`.
The new profile refuses the subject-only `snapshot` entry point. Under the
existing guard lock followed by a scoped SQLite read transaction, export checks
session/project access and captures the complete selected manifest. SQL checks
row counts, types and field lengths before materializing records. At most 100
runs, each within 256 KiB, and one 32 KiB project record are accepted.

That connection and lock close before artifact I/O or ZIP construction. Artifact
reads use bounded descriptors and reject symlinks at every traversed component,
nonregular files, missing files and unsupported size or changing-read results.
Inputs and recipes must match captured hashes. Successful results must parse as
strict JSON and equal captured metrics after canonical encoding. Existing public
lineage, field, member and archive limits still apply, including 64 KiB artifact
reads and the 16 MiB total-member ceiling. No partial ZIP is written to disk or
sent to the client.

After the complete bundle exists, export reacquires guard then a fresh scoped
read transaction and checks current session/user/membership authority and exact
project/run state. The private manifest includes all run columns, particularly
`cancel_requested`, request identity and raw payload, plus session expiry and
the user's active state and key hash. These fields never enter the public ZIP.
Changes still present at the final comparison refuse the entire export; there
is no automatic retry or mixture of old and new terminal evidence.

The final successful equality and expiry check under the guard is the
authorization point. A later revoke or logout does not retract the bytes already
authorized; HTTP streaming holds no export connection or authority lock. Equality
proves current state, not absence of a transient membership change and revert.
Verified copied artifact bytes remain valid if their source files subsequently
change or disappear. Unchanged running/failed records preserve their current
status and unknown liabilities; export does not settle or refund them.

Failures return no archive, release connections/locks and attempt all opened
descriptor closes. A close with an uncertain outcome is not retried, since the
descriptor could already be closed; all remaining closes are still attempted and
publication refuses. Errors do not expose paths, credentials or private manifest
fields. Export adds no guard intents or accounting/authority mutations. SQLite's
own connection/open/close effects are not a storage guarantee.

## Qualification and limits

[Acceptance evidence](evidence/2026-10-18/bounded-export/README.md) records
disposable production-connected tests, independent review and preserved failures.
Concurrency tests block artifact reads and bundle construction while separate
threads revoke, rotate, cancel or settle work. HTTP verification blocks streaming
after final authorization and confirms that locks and readers are released.
No live broker/provider runs or accepted-store changes are involved.

The #41 witnesses now execute from a 39-file archive of
`1e4b04c2d07dd4730d904fe93acb1795ad1a4d1c`, SHA-256
`6bf625ca8c826e85d2cb80893e1e61fd673e52fff6b33a9bf1439c8e7977efdf`.
The bridge verifies the archive, its original source/evidence hashes and the
unchanged contract/harness before execution. Original #39/#40/#41 evidence is
preserved byte-for-byte. Historical qualification is not relabeled as evidence
for modified production code.

This removes the export reader across slow artifact and network operations; it
does not bound elapsed time, other readers, WAL history, retry scratch, physical
memory or disk use. No checkpoint or writer/reader framework is installed.
Capacity remains UNKNOWN, `quota_enforced:false`, and fifth-headroom FAIL. OIDC
and all finite restore/cutover paths remain refused. Source candidates remain
disabled; original cleanup evidence and later supplemental accounting remain
intact. Guard scratch exhaustion and whole-store admission need separate work.
