# Disabled assessment input lifecycle (AS1)

AS1 connects the existing scoped Store, SQLite Owner, operation Guard and current
authorization to Outfitter's owned-data protocol. This is source and synthetic
test infrastructure. The production Application always installs the disabled
service and refuses assessment routes before reading their bodies. There is no
CLI flag, configuration switch, migration or production constructor that enables
AS1. The positive constructor and injected peer policy exist only in tests.

The existing study, assessment manifest v1, export and scoped-profile digests
remain separate. AS1 refuses study execution, export, inventory and recovery;
it does not reuse study Worker tokens, make a compute reservation or introduce
a second allowance ledger. A future enabled profile requires separate review.

## Finite metadata and ownership

The closed AS1 policy permits 16 lifetime rows, at most four per subject. Its
per-row manifest is at most 65,536 bytes; complete field and JSON limits are
67,584 and 135,168 bytes. Aggregate limits are 1,081,344 field bytes and 2,359,296
JSON bytes. A metadata row is at most 1,024 bytes, summaries 32 KiB, a broker
status 16 KiB, and at most eight observations of 1,024 bytes each. Requests and
replies are bounded at 128 KiB and 64 KiB; stream chunks are at most 64 KiB.
These are serialization bounds, **not whole-store or physical-disk quotas**.

The guard permits 128 ordinary lifetime intents, counting initialization and
tombstones, and one essential slot. Ordinary admission preserves 64 sequence
increments and 256 generation increments. Each fresh essential transaction
checks its own four-publication allowance. Row versions reserve the last
integer below 2^53 for an absorbing denial. The existing publication scratch
and ownership failure holds still apply; exhaustion can refuse safety writes.
There is no automatic reset, renewal, reclamation or promise of unlimited retries.

Only typed, exact SQL roles are allowed. Count, type, ASCII, size and aggregate
checks occur in the owned snapshot before materializing one full row or at most
16 metadata rows. Auth invalidation and all matching local denials commit in
one transaction. Ordinary and safety transactions use `keep=False`: the SQL
receipt, Owner close and token release finish under the same guard lock before
IPC or HTTP body waits. Only the immediate child spawn occurs within its final
authorization fence. No token remains available to spend publications later.

## Upload and publication behavior

The synthetic path prepares a scope, streams an exact bounded body once, seals
it, and supports status, cancellation and absorbing denial. A failed or ambiguous
writer is never silently reopened. Real unnamed-pipe capabilities belong to the
HTTP process; descriptor duplication and single close attempts are synchronized.
Constructor, child and metadata close uncertainty latches a process/store hold
and prevents another child launch. Denial closes every local lifeline before
attempting metadata IPC. Restarted processes cannot reconstruct those capabilities.

Body reads use an absolute deadline and current-authorization checks between
bounded reads, including while the socket is quiet. Buffered header read-ahead
is consumed before waiting for socket readiness. EOF, would-block and deadline
refusal are distinct; a rejected dripping request may end with a transport reset.
No reset is reported as a successful JSON error delivery.

Before publishing, a fresh guard/Owner/current-session/project fence copies and
hashes the bounded response. OIDC logout uses the same identity fence. All locks
are released before sending HTTP bytes. A revocation completed before that fence
refuses publication; a later revocation cannot retract bytes already authorized.
Responses contain closed metadata, never input content, content digests, private
paths or raw helper diagnostics. Every upload/execution/resource/cleanup trust
flag remains false. An observed synthetic removal requires the protocol's denial,
writer-drain and access-revocation observations, and still proves no real cleanup.

## Verification and remaining gates

The [source-bound targeted record](evidence/2026-10-19/as1-completion/validation.json)
retains prior failures and checker corrections. The inherited 99-method run had
98 passing methods and one method with nine failed subtests at the overflow
refusal precondition, not nine executed safety-action bypasses; its original log
is unchanged. The corrected 99-method regression passed separately. The latest
targeted run passed 55 methods with no skips. Two additional deterministic
controls were then added for buffered would-block and denial propagation; the
frozen full-suite result is recorded separately when available. Counts overlap
and must not be added together. Full-suite, hosted and final independent review
remain pending at this source checkpoint.

Local connected tests use archived Darwin helpers built from Outfitter
`4de6a090664a9a3ccd8ba0417b00af3ff5290a58`, with exact binary hashes in the record.
The hosted controls job builds fresh Linux `broker.test` and `ledger.test` helpers
from accepted source `8584ca8db9a7e5eaf8d29d9670349b57f3939ab6`, records its source
tree, source and binary hashes, and supplies them to the existing test suite.
Those are different builds and platforms. Neither executes an engine or provider.
The existing actual-Docker and clean-source-build jobs and pins are unchanged.

To run the synthetic controls with already built, reviewed helpers:

```sh
WAYPOINT_OWNED_PEER_ASSETS=/absolute/path/to/reviewed/helpers \
  python -W error::ResourceWarning -m unittest discover -s tests -v
```

Without that variable the 15 connected methods explicitly skip. Supplied but
missing or incompatible helpers fail. Operational upload readiness, encryption,
actual-resource measurement, trusted cleanup, host capacity and real-data use
are not qualified by these controls. No deployment, spending or resource grant
is implied.
