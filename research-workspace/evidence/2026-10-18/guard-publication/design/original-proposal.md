# Bounded guard publication proposal

**Status: DESIGN ONLY, awaiting coordinating root review before runtime code.**
Base: accepted PR #42, `66a48dfe29d61d3f6f48f016f2c474e1ac6af4f6`.
Propose a fresh opt-in profile with one fixed ordinary scratch path and one
fixed safety scratch path. Preserve failed scratch and refuse further use of
that path. This bounds the named guard payload family under controlled fresh
state, at the cost of explicitly reduced safety-action availability.

The consequential decision is **refuse and retain debt when a mandatory safety
publication cannot succeed**. An unresolved essential intent cannot authorize
a later safety action by borrowing its marker. Existing profiles retain their
current behavior. This proposal adds no quota, capacity ledger, admission
allowance, cleanup protocol, SQLite checkpoint policy or provider operation.

## Fresh profile and compatibility

Propose `init --bounded-guard-publication`, mutually exclusive with the existing
finite-profile selectors. It inherits the unchanged finite row and field
limits and PR #42's bounded export protocol. Initialization requires a fresh
root; partial initialization is preserved and cannot be adopted or retried in
place. Existing stores receive no migration or new default.

| Binding | Existing bounded export profile | Proposed profile |
| --- | --- | --- |
| Mode | `local-finite-public-data-v2` | `local-finite-public-data-v3` |
| Logical policy format | `educloud-finite-authority-v2` | `educloud-finite-authority-v3` |
| Guard binding format | `educloud-operation-guard-v3` | `educloud-operation-guard-v4` |
| SQLite `user_version` | `0x45464132` | `0x45464133` |
| Export protocol | `educloud-bounded-export-v1` | Unchanged |

The exact new logical policy adds this `guard_publication` object:

```json
{
  "format": "educloud-bounded-guard-publication-v1",
  "ordinary_scratch": ".operation-guard-ordinary.pending",
  "safety_scratch": ".operation-guard-safety.pending",
  "max_payload_bytes": 4194304,
  "unresolved_essential": "refuse",
  "residue": "preserve",
  "failure_notes": "origin_role"
}
```

Bind the canonical whole-policy digest into configuration and guard, with the
sole matching database authority ID and database version. Select behavior only
from a complete supported tuple. Mixed, missing, stripped, downgraded or unknown
bindings refuse before business effects; never fall back to an older profile.
OIDC and restore/cutover refusals include direct and configuration-stripped
entry points. The existing coordinated-rollback limitation remains.

## Scratch ownership and publication

Keep `operation-guard.json` as current authority and `operation-guard.lock` as
the existing process/thread serialization mechanism. Both scratch paths are
relative to the same private root. Every inspection, creation and publication
holds the existing RLock and file lock; guard readers close descriptors before
releasing it. This is not an ownership protocol for SQLite or external readers.

1. Validate the exact current incarnation, profile and operation context. Choose
   the role from trusted internal control flow, never from an HTTP field or a
   free-form diagnostic label. Serialize the complete next value before creating
   anything; reject more than 4,194,304 bytes or counter exhaustion.
2. Create the selected fixed path with `O_WRONLY | O_CREAT | O_EXCL |
   O_NOFOLLOW`, mode `0600`. Require a regular file with one link. Any pre-existing
   entry occupies the slot, including an empty file. Never truncate, append to,
   adopt, rename or delete residue, and never try a random or opposite-role path.
3. Write only the serialized bytes, handling short writes without exceeding the
   ceiling. Synchronize the file and close its descriptor. Attempt every owned
   descriptor close once even if another close fails; uncertain close results
   are failures, not permission to retry a possibly recycled descriptor number.
4. Verify the candidate path still identifies the newly created device/inode,
   one-link regular file, exact length and exact payload. Verify the current path
   still matches the captured predecessor. Replace current with **this call's
   own successfully prepared candidate**, synchronize the parent directory, then
   verify the new current incarnation and the absence of that scratch path.
5. Only acknowledged completion of all stages permits the next publication to
   create that role's name again. Successful replacement consumes its own
   candidate; it never licenses handling a file inherited from another attempt.

Use a validated parent-directory descriptor for relative operations and retain
the existing lock-identity checks. A candidate or current-file mismatch holds
all mutations. The assumptions are a controlled private root and cooperative
writers using this lock; privileged hostile filesystem mutation is outside the
claim. A supported deployment must qualify its synchronization primitives.
Python documents atomic successful replacement and file synchronization as
separate operations; replacement acknowledgement alone is not this protocol's
durability boundary. [Python OS interfaces](https://docs.python.org/3/library/os.html#os.replace).

## Roles and required intent

| Origin | Required scratch role for prepare, effect and failure note |
| --- | --- |
| Empty bootstrap, user/session admission, project creation, submission, claim and run dispatch | Ordinary |
| Logout, revoke/rotate, existing-member demotion/removal, cancel, close, conservative settlement and restart recovery | Safety |
| Authenticated observation and bounded export | Neither; no guard publication |

Keep the originating role on the internal operation token. An ordinary worker,
claim or run failure cannot become a safety action merely because it is an
error. Replace the new profile's unscoped `Guard.fail` behavior with explicit
origin routing; do not iterate over unrelated active safety tokens or create
an essential fallback marker. An idle ordinary error may attempt an ordinary
diagnostic intent only if ordinary admission still permits it. Otherwise report
the error and latch the process; do not claim the failed diagnosis is durable.

`Store.finish` is a safety operation, while an uncertainty note for its underlying
run token remains ordinary. Failure to publish either required transition must
propagate to the caller. Preserve the original error if a diagnostic write also
fails. A prior SQL commit or provider effect can make the outcome unknown; the
failure response must not imply rollback, refund or confirmed completion.

Every new safety request needs its own durable prepared intent, started receipt
and durable effect transition before business writes. The new profile disables
`borrowed=True` for mutation. If the sole essential logical entry is unresolved,
nonterminal or uncertain, retain it and refuse a new safety action even when the
safety scratch path is absent. Only the existing exact terminal receipt proof,
with a non-uncertain phase, permits replacing that logical entry and its receipt.
Publication identity never substitutes for SQL or provider proof.

## Exact incarnation and uncertainty

Add a bounded `publication` object to the new guard format, inside the same
4 MiB payload ceiling. Its exact fields are `id` (32 lowercase hex characters),
`owner` (the existing process BOOT identifier), `role` (`ordinary` or `safety`),
`operation_id` (an existing operation ID), `stage` (`bootstrap`, `prepare`,
`effect` or `uncertainty-note`), `previous_generation`, `previous_sha256` and
`generation`. The new generation equals the enclosing guard generation and the
previous generation plus one, within the existing signed-64-bit ceiling.

Only the first empty bootstrap permits null operation and predecessor fields;
its generation is 1. Later records must name a present operation of the matching
role. Record the SHA-256 of the exact previous current bytes, rather than a
reserialized approximation. This is one replacement identity, not a retained
history or new ledger. Existing intent IDs, sequences, digests, receipts and
unresolved liabilities remain authoritative for operation outcomes.

| Failure boundary | Required result |
| --- | --- |
| Serialization or exclusive creation fails before this call owns a file | No new business effect. Preserve existing entries; report refusal. An absent new intent is not a durable record of the refusal. |
| Write, file sync, close or candidate verification fails | Retain the occupied slot and prior current. Latch the process; no same-role retry or cross-role diagnostic fallback. |
| Replacement fails or its acknowledgement is lost | Treat replacement outcome as unverified, even if scratch is absent or current looks correct. Preserve observed files and identity; no retry in that request. |
| Replacement returns but parent sync or final verification fails | Report failure and hold the process. Visible current does not certify durability or the business operation. Do not overwrite it with a compensating note. |
| Started receipt, business commit or acknowledgement fails after guard publication | Preserve the existing started/uncertain debt. Any permissible note uses its origin role; failure cannot certify completion. |
| All publication stages succeed | The role's candidate is consumed and its name may be used for the next required save. Other unresolved intents and residue remain. |

A publication failure prevents further publication by that process, including
safety publication, until a fresh-process qualification. This conservative hold
is additional to permanently occupied slots; the two must not be conflated.
No exception path may write directly to current to evade either hold.

## Restart and another writer taking over

Under the guard lock, inspect current, both fixed paths and the complete
`.operation-guard-*` namespace before `resume` changes SQL. Unknown names,
unsupported types, multiple links, oversize files, invalid bindings or changed
incarnations refuse mutation and leave the bound UNKNOWN. Recognized ordinary
residue is preserved without adoption or promotion; its bytes are not used to
reconstruct missing authority or infer completion.

For a valid current incarnation, require exact schema/authority/receipt proof
first. Synchronize that observed current file and its parent, close descriptors,
perform the existing FULL-synchronous SQL startup barrier, and revalidate the
same current incarnation and proof. This qualifies the state observed now; it
does not turn an earlier failed response into acknowledged success. Missing
receipt proof refuses mutation rather than manufacturing receipts or replaying
an intent. The startup barrier is the existing authority-durability operation,
not an exemption for a business or safety mutation.

The same takeover qualification is required before a cooperative writer uses a
current incarnation it has neither durably published nor previously qualified.
A per-process failure latch alone cannot protect other processes. Cache only
the exact successfully qualified incarnation under the lock; a changed identity
invalidates it. Do not run takeover barriers inside an already active business
transaction or after a publication failure in that same process.

After those barriers, apply this availability contract:

| Observed state | Ordinary work | New safety action |
| --- | --- | --- |
| Both scratch paths absent, exact proof, no unresolved holds | Existing ordinary limits apply | Fresh intent required |
| Ordinary residue or unresolved ordinary intent, otherwise valid exact proof | Refused | Allowed only with safety path absent and no unresolved essential entry |
| Safety residue or unresolved essential entry | Refused | Refused; no borrowing |
| Missing/mismatched proof, unverified barriers or unsupported guard family | Refused | Refused |

Thus a failed ordinary prepare followed by missing SQL registration can also
block safety actions despite an empty safety path. A reservation of a pathname
does not guarantee that logout, revocation, cancellation or settlement can
complete. Where existing authenticated reads remain valid they may report the
hold or export evidence, but may not adopt residue or repair authority.

## Exhaustion and the limited byte claim

Report a specific held reason such as `safety_slot_exhausted`,
`essential_intent_unresolved` or `publication_unverified`, with no success
response for the requested action. Preserve access state, compute liability,
reservations and conservative charges as actually observed. In particular,
failed revocation may leave access active, and failed settlement leaves debt
unresolved. Existing provider cleanup behavior is unchanged; successful provider
cleanup alone cannot certify accounting settlement.

Under controlled fresh state, each of the three named payload paths is at most
4,194,304 bytes. Their simultaneous logical sizes therefore sum to at most
**12,582,912 bytes (12 MiB)**, including ordinary residue alongside a safety
candidate. This is a conditional bound on named regular-file payload bytes.
The separate zero-payload lock, filesystem metadata/allocation/journaling,
unlinked generations retained by external readers, SQLite/WAL/SHM/temporary
files, other writers, artifacts, backups, memory and elapsed time are excluded.
No whole-action, inode, physical-space or available-capacity bound follows.

Foreign or oversize entries cannot be deleted or truncated to recover the
claim. Safety-slot exhaustion is terminal for further mutations in that root
within this slice; no cleanup, automatic recovery or restore bypass is supplied.
Available capacity remains UNKNOWN, `quota_enforced:false`, and fifth-headroom
FAIL remains intact.

## Required qualification after design approval

Runtime implementation and qualification are **NOT RUN**. Future disposable
controls must cover:

- Exact-fit and one-byte-overflow serialization before creation; simultaneous
  current plus ordinary residue plus safety candidate; repeated failures with
  no third scratch path, and competing processes using the existing file lock.
- Short writes; file-sync and every close failure; replacement failure before
  and after its effect; parent-sync failure; receipt/commit acknowledgement
  failure; process death; fresh restart and takeover by an already-running
  second process. Inspect every retained incarnation and original error.
- Empty residue, symlinks, hardlinks, unknown prefix names and changed current
  identity. No adoption, overwrite, truncation, deletion, random fallback or
  inference of successful completion from path disappearance.
- Every listed safety action with ordinary residue, safety residue, full
  ordinary intent allowance, an unresolved essential intent with empty scratch,
  and missing receipt proof. Verify failures preserve debt and report the actual
  authority state; ordinary diagnostic errors never consume the safety slot.
- Bootstrap failure and strict tuple binding; all earlier profiles; bounded
  export's authorization/reader behavior; direct and stripped-config OIDC and
  restore refusals. Negative controls must catch cross-role fallback, borrowed
  essential mutation, residue deletion and a missing durability barrier.

Keep the frozen #39/#41 harnesses and all accepted evidence byte-identical;
historical witnesses retain their original source bindings. New controls must
exercise the new production profile. Preserve disabled source environments,
the original PR #31 cleanup failure and later supplemental conservative charge,
the active Outfitter pin, stopped stores, Belay, and all parent/rejected/dirty
worktrees. Coordinating root review must accept the exhaustion, takeover and
compatibility contract before runtime implementation.

## Implementation locations for later review

The base already provides the lock, serializer, receipts and startup barrier in
[operation_guard.py](research_workspace/operation_guard.py), versioned exact
policies in [finite_authority.py](research_workspace/finite_authority.py), and
publication origins in [store.py](research_workspace/store.py) and
[runtime.py](research_workspace/runtime.py). New-profile routing would also
touch [CLI selection](research_workspace/__main__.py), bounded export profile
selection and recovery refusal detection. It must not change the old branches.
The [accepted protocol witness](SQLITE-GUARD-PROTOCOL.md) explains the existing
unbounded retry history; the [export proposal](BOUNDED-EXPORT-PROPOSAL.md) fixes
the compatibility boundary inherited here.
