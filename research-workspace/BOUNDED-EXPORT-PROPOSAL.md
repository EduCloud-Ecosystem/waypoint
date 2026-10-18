# Bounded export reader proposal

**Status: narrow implementation GO after coordinating design review.** Base:
`1e4b04c2d07dd4730d904fe93acb1795ad1a4d1c` (accepted #41). The next slice closes
export database readers before artifact reads and bundle construction, then
revalidates authority and the exact captured state before returning bytes.
This removes one reader-lifetime dependency; it does not establish a maximum
WAL size, elapsed time, physical memory usage or available storage capacity.

## Explicit version binding

Propose `init --bounded-export` as a separate fresh, local-key-only opt-in. It
implies the same finite row/field limits and operation guard. Existing defaults
and `--finite-authority` retain #40 behavior; combining the two profile flags
refuses rather than silently choosing one. Existing stores are not migrated.

| Binding | Existing #40 profile | Proposed export profile |
| --- | --- | --- |
| Mode | `local-finite-public-data-v1` | `local-finite-public-data-v2` |
| Exact logical policy format | `educloud-finite-authority-v1` | `educloud-finite-authority-v2` |
| Policy addition | None | `export_protocol: "educloud-bounded-export-v1"` |
| Guard binding format | `educloud-operation-guard-v2` | `educloud-operation-guard-v3` |
| Database `user_version` | `0x45464131` | `0x45464132` |

The new policy keeps the existing limits unchanged. Its exact canonical digest
is bound into configuration and guard sidecar alongside the existing authority
ID. Select the profile only from a complete supported tuple: mode/policy,
database marker, guard format/digest and sole matching database authority ID.
Missing, mixed, unsupported or downgraded markers refuse before reads or safety
effects; never fall back to v1 or unrestricted behavior. Retain the existing
coordinated-rollback limitation. OIDC and restore/cutover remain refused for
both finite profiles, including direct and configuration-stripped entry points.

## Export contract

The new profile uses one Store export helper that accepts the selected project,
subject and required local session token. The HTTP handler passes the already
authenticated token privately; subject-only direct calls cannot bypass final
session authorization. Existing profiles retain their existing export path.
The old `snapshot` entry point refuses on the new profile, directing callers to
the complete export helper instead of exposing an incomplete publication path.

1. **Capture in a scoped read transaction.** Acquire the existing guard lock,
   then open the validated query-only SQLite connection and begin a read
   snapshot. Validate the current token/session, active user and membership;
   capture the selected project and its complete ordered run set. Materialize
   plain values only. Close the transaction and connection and release the
   guard before any artifact file is opened.
2. **Read and build without database readers or authority locks.** Read bounded
   regular files for captured inputs/recipes and only captured successful
   results. Refuse unsafe paths, unsupported file types, missing files, size
   overflow or read failure. Verify input/recipe bytes against captured hashes;
   strictly parse results and compare canonical values with captured metrics.
   Use the existing deterministic bundle builder and its lineage, public-field,
   run-count, member and total-size checks. Do not discover new terminal results
   from files that appear after capture. Construct the complete bytes in memory;
   do not create a partial ZIP on disk or send any HTTP success headers yet.
3. **Recheck under the existing lock order.** After verification and ZIP
   construction, reacquire guard then a fresh scoped query-only transaction.
   Revalidate profile/authority identity, the same unexpired session and active
   user, exact membership and the complete selected project/run manifest. Any
   difference refuses the entire export without automatic retry. Close this
   connection and release all locks before returning the completed bytes.

The private comparison manifest includes raw project payload; ordered run IDs
and every existing run column (`project_id`, `subject`, `status`, `payload`,
`cancel_requested`, `request_key`, `request_hash`); member role; session token
hash, subject and expiry; and user subject, active state and key hash. Compare
exact stored values and row presence, including newly added runs. Private
columns are comparison inputs only and never enter the public archive. Profile
and authority ID must remain identical; unrelated operation sequence changes
alone do not invalidate an otherwise identical selected manifest.

The final successful comparison, including a fresh expiry sample, is the export
authorization linearization point. Mutations using the existing guard are
ordered before or after it. Revocation/logout/rotation or a selected-state
change still present at that point refuses export. Equality proves the current
exact state, not detection of a transient membership change followed by a revert.
A change after it does not retract
the already-authorized bytes; streaming holds neither a reader nor a lock.
Direct external database writes and hostile privileged filesystem changes are
outside that serialization claim. Verified copied bytes remain the export's
artifact snapshot; later source-file changes cannot alter those bytes.

At most 100 project runs are materialized, each within the existing 256 KiB
stored-record limit, with a 32 KiB project record and fixed bounded authority
fields. Enforce row and field ceilings while collecting, before artifact I/O.
Existing public `runs.json`, 64 KiB input/recipe/result, 16 MiB total-member and
archive limits remain refusal ceilings, not permission to allocate unbounded
input before checking. An unchanged running/failed/unknown record remains in
its captured state. Settlement or cancellation during construction refuses
instead of mixing old lineage/accounting with new terminal results.

## Failure and acceptance requirements

An export failure returns no archive or partial publication, closes every
connection/file descriptor and releases locks. It allocates no operation intent,
changes no accounting/authority record and does not retry, settle, refund,
reconcile or remove files. SQLite open/close engine effects remain outside any
storage-bound claim. Authentication and export errors must not expose tokens,
private comparison fields, host paths or raw diagnostics.

Required disposable controls before acceptance:

- Block artifact I/O and ZIP construction independently; prove the capture
  connection is closed and revoke, rotate, cancel and settle each complete in
  separate fresh fixtures. The affected export must then refuse.
- Block HTTP streaming; prove the final connection and guard lock are already
  released. Demonstrate the documented final-authorization ordering.
- Alter or remove input/recipe/result files before or during their read;
  mismatches or read failures refuse. Later file changes cannot alter verified
  copied bytes and do not alone invalidate the export. Mismatch metrics, cancel only via
  `cancel_requested`, change membership/session/user authority, expire a session,
  close the project or add a run. Each difference still present at the final
  manifest/authority comparison refuses without retry.
- Export unchanged running and successful records coherently; retain unknown
  liabilities, conservative charges, failed controls and private-field exclusion.
- Verify cleanup on read, validation, ZIP and final-check failures; no guard
  intent/accounting mutation. Negative controls must catch an open reader and a
  missing final authority or private-column comparison.
- Exercise mixed/stripped profile bindings, fresh-only initialization, legacy
  compatibility, OIDC and all restore refusals, then appropriate full CI.

Preserve all #39/#40/#41 evidence bytes. Before production changes, freeze #41's
source-pinned harness and required production/test/catalog inputs from this base
in a hashed archive, using #39's established replay pattern. Its bridge must
verify and run those original bytes, without relabeling historical witnesses as
tests of changed source. New export controls exercise current production code.

No checkpoint, all-reader coordinator, scratch reuse, journal/default change,
quota admission or cleanup protocol is part of this slice. Capacity remains
UNKNOWN, `quota_enforced:false`, and fifth-headroom FAIL. Source candidates,
original cleanup evidence, broker pin, protected stores and worktrees remain
unchanged. Guard scratch exhaustion is a later separate decision and review.
