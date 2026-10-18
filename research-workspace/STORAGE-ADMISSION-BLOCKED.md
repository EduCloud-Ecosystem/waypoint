# Storage admission implementation blockers

Whole-store quota enforcement remains **BLOCKED and not implemented**. The next
admission draft does not yet make uncertainty, essential writes and recovery
safe under the same capacity authority. It must not be enabled or merged as
working enforcement. This is a concrete design/evidence checkpoint for
[issue 29](https://github.com/EduCloud-Ecosystem/waypoint/issues/29), not a claim
that the contract is impossible.

The accepted baseline is `2a7d6cd08ce7881a5ea36d761e53fc0c6f0e4b71` from PR35.
It includes PR34's bounded read-only inventory/retention reports and PR35's
incremental broker response limits. Both explicitly stop short of whole-store
admission. This checkpoint changes documentation and inert evidence only.

## Reproduced failures

The [frozen draft and reproducer](evidence/2026-10-17/storage-admission-blocked/README.md)
bind the three proposed implementation files to an exact patch and accepted
base. Five cases reproduce unsafe or unavailable draft behavior, each with a
comparison control. Fresh synthetic state and intercepted process calls isolate
these cases from accepted pilot stores and real provider effects.

| Draft boundary | Reproduced behavior | Required correction |
| --- | --- | --- |
| Claim and launch | A hold introduced after the preliminary claim lock is released still permits a running claim and reaches an intercepted broker-launch call. | Serialize the hold check with the claim transition, then fence launch against uncertainty arriving after preparation. |
| Failed rollback | An injected rollback I/O exception leaves a pending transaction classified as active after its connection closes. The hold is false. | Latch uncertainty before rollback and discard the in-process active marker in an unconditional cleanup path. |
| Result before database commit | A synthetic oversized run record fails after `result.json` is written. Zero SQL changes clears the intent; fallback settlement leaves the run marked settled without a hold. | Track filesystem/provider effect boundaries explicitly. Only proven pre-effect refusals may clear an intent. Bound final records before effects, and retain ambiguity after any later failure. |
| Project initialization | A worker observing a normal unlocked project-initialization intent stops and latches a permanent hold, although initialization then commits. | Distinguish an actively owned operation from stale or failed work without admitting a second conflicting initialization. |
| Essential headroom | A legal full-budget inventory model passes admission; consuming one reserved byte in a protected temporary file causes the essential check to refuse. | Debit outstanding growth as it becomes observed storage. Prove that essential writes fit the original envelope at every intermediate step. |

The rollback case models the database exception and coordinator persistence in
memory. The claim, result and project cases use real disposable SQLite databases
and filesystem writes, but simulated faults, a stub broker and an intercepted
launch. The headroom case is arithmetic with physical free-space checks mocked.
These are not actual disk-full, power-loss, provider cleanup or hosted-capacity
trials. The oversized run record is a deliberately injected internal value; no
ordinary reviewed recipe is shown to produce it.

## Capacity proof still required

The draft's 64 MiB logical-byte and 4,096-entry defaults are proposed protocol
values, not an enabled policy or measured host capacity. Its row/page ceilings,
run envelopes and fixed broker-writer multiplier are unaccepted assumptions.
Passing the existing 184 offline tests would not validate these new guarantees.

The pinned Outfitter ledger writer does enforce a 96 KiB encoded record limit
and a 64 MiB ledger limit, and uses atomic replacement files. Those individual
limits do not establish a whole-root peak bound. The proof must include output
sidecars, input/recipe snapshots, watchdog ready files, lock files, simultaneous
run/watchdog/cancel/project-init writers, retained failed temporary files and
writers that survive a CLI observer timeout. See the pinned
[ledger writer](https://github.com/EduCloud-Ecosystem/outfitter/blob/5827c84e9952cc67b85e91ceff4282e0ead6923b/internal/ledger/store.go)
and [broker lifecycle](https://github.com/EduCloud-Ecosystem/outfitter/blob/5827c84e9952cc67b85e91ceff4282e0ead6923b/internal/broker/broker.go).
The draft does not yet establish the maximum writer overlap across failures or
bind every external writer to its storage coordinator.

The SQLite allowance also needs a source-backed bound for live pages,
replacement/transient pages, indexes, journal headers/records and the exact
supported VFS/settings. A page ceiling can refuse a write; it does not by itself
prove settlement or revocation will succeed. Logical capacity cannot guarantee
physical free space against unrelated host writers. Physical write failure must
retain uncertainty and refuse acknowledgement, not silently release allowance.

## Implementation sequence and acceptance

1. **Authority and failure transitions.** Define durable operation states and
   the separate in-process ownership state. Bind policy and intents to the
   database and broker authority; reconcile rows/intents before admission.
   Missing or stale policy cannot create fresh allowance. Make transaction exit,
   rollback failure, post-commit callbacks and worker double-failure fail closed.
   Surface the hold through the service and keep the user's input draft intact.

2. **Every writer and the peak equation.** Derive a versioned writer inventory
   and encoded bounds from the pinned implementation. Under serialization,
   require observed bytes/entries plus remaining admitted growth plus new growth
   plus remaining essential growth to fit both limits. Include root/directories,
   SQLite/WAL/SHM/journals, broker authority/output/snapshots/watch files, unknown
   and orphan artifacts, and atomic replacement peaks. Do not count the same
   growth as both consumed storage and still-required reserve. Preserve finite
   intents through duplicate retries and restart; uncertain work cannot replay,
   receive a refund or recover allowance automatically.

3. **Locking and dispatch.** Preserve the order lifecycle lock, storage
   coordinator, SQLite transaction, identity publication lock. Keep storage and
   database locks out of provider execution. Coordinate claim and launch fencing
   without blocking logout, revocation, cancellation or settlement. Use
   descriptor-based traversal and validate the exact snapshot input tree before
   provider effects. Refuse unknown races rather than treating unobserved usage
   as zero.

4. **Recovery and migration.** Explicit offline migration must install policy
   without silently changing accepted stores. Checkpoint and held restore must
   retain policy, intents, revocations, liabilities, unknown files and empty
   directories. Establish destination peak capacity before copying and again
   before sealing the source. Keep admission held through uncertain cutover.
   External backup and replay outputs need separate finite peak envelopes;
   source-root admission does not cover them.

5. **Acceptance before enabling.** Add deterministic last-byte/last-entry
   concurrency, duplicate/no-effect refusal and full-admission safety-write
   cases. Inject partial writes, ENOSPC, file/directory fsync, rename, database
   commit/rollback, worker double-failure and process death. Test WAL/temp/orphan,
   links, growing files, held restore and missing/stale authority. Run read-only
   retention regressions and verify browser refusal preserves the draft. Repeat
   independent review on the immutable implementation head before final CI and
   merge. The five retained defect witnesses are not substitutes for this suite.

## Preserved boundaries

The local `storage.py`, `store.py` and `runtime.py` draft remains uncommitted and
is retained as an inert patch for review and recovery. No accepted store is
migrated, and no quota or deletion command is enabled by this checkpoint.
Read-only retention does not authorize deletion.

All four source environments remain disabled. The original failed replay,
source-build evidence, identity evidence and liabilities are unchanged; later
cleanup observations do not rewrite the original failure or grant a refund.
Belay, the original Waypoint checkout and parent pilot worktrees remain outside
this work. Hosted capacity, private/student data, human acceptance and source
promotion remain open gates.
