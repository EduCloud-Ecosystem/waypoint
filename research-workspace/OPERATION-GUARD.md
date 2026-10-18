# Durable operation intents and dispatch holds

This slice introduces a separately versioned guard for fresh disposable local
stores. It does not enforce storage quotas, change SQLite journal mode, migrate
accepted stores or authorize deletion. `quota_enforced` remains false. The
rejected admission draft and its original five failure witnesses stay intact.

## Authority and operation contract

A guarded store binds its configuration, database and guard sidecar to one
authority identifier and supported format. A missing, replaced or unsupported
binding cannot fall back to legacy operation. Legacy stores are opened without
installing guard state. Only explicit fresh initialization creates the profile.

Each intent has bounded stable operation/resource identifiers, an operation
kind, request digest, optional idempotency key, owner generation and phase.
An intent is durable before any related business file, database mutation or
provider effect. Initial intent failure permits no such effect. Sidecar writes
use exclusive temporary files, file fsync, atomic replacement and parent fsync.
Failed temporary files remain protected. Bootstrap of a fresh directory and
the intent authority itself is distinct from the business effects it authorizes.

The sidecar phases are prepared, effect and uncertain. Completion is never
inferred from a successful-looking sidecar rename. The database stores an exact
receipt for each intent, with started, committed or refused outcomes. The
immutable digest binds authority ID, operation ID, sequence, kind, resource,
subject, request key, request hash and essential-slot identity. Serialization
generation is separate from operation sequence. A durable prepared sidecar
precedes a committed started receipt, which precedes external effects.

Business publication and terminal receipt commit in one SQLite transaction.
Only an explicitly tracked pre-effect refusal may become refused. An exception
type or zero SQL changes is not proof of no effect. A terminal run requires a
terminal outcome, verified cleanup and complete resolved accounting. Interrupted
or unknown work retains its intent even if a cleanup or charging flag is true.
Uncertain work cannot retry itself or receive another project allocation.

Normal startup validates exact supported schema and bidirectional intent/receipt
identities, then commits a real FULL-synchronous authority barrier before trusting
recovered terminal proof. A fresh SELECT alone is not durability acknowledgement.
Explicit uncertain phases and failed temporary files still hold admission.
Mismatched older sidecars/databases are detectable; rolling both back together
cannot be detected without an external trusted anchor. The profile makes no
claim to detect that joint rollback.

The map permits 4,096 ordinary intents and one serialized essential slot. This
is a finite metadata limit, not a storage quota or a physical growth estimate.
Completed ordinary records are retained. Each queued run reserves one further
intent slot for claim; all other ordinary operations honor these reservations.
Project and run admission flags distinguish their one- and two-slot requirements.
Already admitted operations can finish using their own slots when capacity is
otherwise exhausted. Exhaustion visibly pauses new work.
Essential-slot reuse writes a new incarnation before replacing its exact started
receipt and watermark. An uncertain essential slot is never replaced: later
safety writes remain covered by the original dirty marker and cannot certify it.

Active ownership is an in-process lease, separate from the durable intent.
Normal active initialization may defer a conflicting request without poisoning
the worker. Unfinished work without supported live ownership requires
reconciliation. Failure latches in memory before cleanup is attempted; rollback,
close and failure-note errors cannot retain an apparently active lease. An
unfinished intent without terminal database proof holds dispatch after restart
even if its failure note cannot be written. If all effects and their joint proof
committed and only acknowledgement/close failed, a fresh successful durability
barrier can establish that exact completion without replaying any effect.

## Effect and control boundaries

Claim checks the hold and changes run state under the same coordinator lock.
The actual broker spawn is fenced against hold creation and current identity.
Storage/database locks are released before waiting for broker execution. A
spawn attempt with an uncertain outcome remains possibly launched, never
silently classified as never dispatched. Cancellation remains available. Local
lock order is lifecycle, guard, SQLite, then local identity publication; remote
identity checks precede these locks. Popen and filesystem synchronization have no
hard latency guarantee in this slice.

Final run/result records are serialized and bounded before result-file writes.
Any subsequent error, including ValueError, retains the intent and any result
file. Worker claim and double-settlement failures visibly hold new projects and
runs. Read, logout, revocation, cancellation and settlement paths remain callable;
failed writes do not return success. Public status exposes a bounded reason,
not raw errors or private paths. Browser refusal preserves the user's draft and
the request's idempotency key.

Project creation persists a stable project/init intent before broker
initialization. Repeating a completed key returns the same project; an uncertain
key cannot create another project or allowance. Broker waiting occurs outside
the storage/database locks.

## Recovery and acceptance

Checkpoint, held restore and source/destination fingerprints include guard
sidecars and failed guard temporary files. Unresolved intents block activation
before source sealing. Recovery retains revocations, conservative charges,
stale-source protection and no automatic replay. Guard-aware recovery must not
silently drop authority because it is outside the existing run directory.

Guarded checkpoints use private-checkpoint-v2; existing v1 recovery remains
separate. Maintenance opens do not run startup barriers. Held configuration is
durable before restored authority is copied. Paired cutover intents bind the
checkpoint, both paths/authorities and both verified fingerprints before source
sealing. Provider waiting releases guard/database locks but retains lifecycle
locks. Destination recovery/config publication precedes source completion, and
destination terminal proof is published last. An interrupted v2 cutover requires
explicit reconciliation; neither retry nor the already-active path replays the
provider cutover. Read-only storage inventory protects this new profile without
claiming it is eligible for retention or deletion.

## Operator use

Only `python -m research_workspace init --operation-guard` with a fresh empty
state directory with an existing durable parent creates this opt-in disposable
profile. Fresh authority/checkpoint/restore directory entries are synchronized
in their parents, and copied directory trees are synchronized before publication.
Supply the ordinary broker, immutable-image and finite compute-budget arguments as usual. New mode
names make older serving binaries reject it. Existing accepted states are not
migrated. Do not remove the guard binding, receipts, pending intents or temporary
files to bypass a hold; retain them for explicit reconciliation. No automatic
repair or quota admission is introduced here.

Required verification converts the first four frozen witnesses to passing
regressions while preserving the original observations. It covers claim,
preparation and launch races; initial-intent, rollback, commit, close, rename,
file/parent-fsync and double-settlement faults; restart across intent/effect/commit
boundaries; normal overlapping initialization; unpublished result survival;
held restore; and browser/API paused status with preserved drafts.

The fifth headroom witness stays blocked. Future capacity work must derive
observed plus remaining operation growth plus remaining essential growth plus
new growth, debiting only verified operation-owned consumption. Broker ledger
serialization, snapshots/watchdogs/temporary files and SQLite bounds remain
separate unfinished work. All source candidates stay disabled and prior source,
identity, failure and liability evidence remains unchanged.

The [local acceptance record](evidence/2026-10-17/operation-guard/README.md)
contains 235 passing offline controls, nine real browser/HTTP checks and twelve
local Docker/provider checks, with retained incomplete browser attempts and
hash-bound prior evidence. These checks accept only the fresh local profile.
