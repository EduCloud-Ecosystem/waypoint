# Reviewed pending-transfer reconciliation prototype

This is an extension of the [standalone ownership contract](CROSS-WORKER-CONTRACT.md).
It resolves an interrupted reviewed transfer in the private local authority. It
does not activate a production course, connect remote hosts or change routing.
The rehearsal uses two logical workers on one machine and supervised synthetic
processes. Real runtime/provider barriers, authenticated remote authority access
and encrypted off-host recovery remain separate acceptance work.

## Choose coursework explicitly

An interrupted transfer can leave its source and receiver reserved and closed to
admission. Exact resume refuses if retained work or snapshot evidence has changed.
Recovery requires a new reviewed decision selecting one of those two endpoints.
The selected endpoint's current retained tree and snapshot are bound independently
from the other endpoint's copy. Divergent files are preserved at both endpoints;
the controller does not decide which work is newer or merge, copy or overwrite it.

The logical course identity, resource profile, runtime and registered worker
incarnations must still match. This recovery path does not re-enroll a changed
worker, recover a missing authority, adopt changed identity bindings or alter
resource limits. Restore the inspected contract or use a separately reviewed
future recovery workflow for those cases. Never edit SQLite or remove a barrier
to make an old approval pass.

Both endpoints must be stopped before generating or applying the recovery plan.
The independent barriers are then established and inspected under both endpoint
exclusions. Unreachable inspection or a stop acknowledgment alone is insufficient.

## Preserve decisions and advance ownership

The recovery plan binds the authority incarnation, exact pending transaction row,
current owner and epoch, both worker registrations, both current trees/snapshots
and the current authority state. The original transfer plan, stage, proof and
receipt remain retained. Preparing recovery closes the old resume path; competing
or stale decisions refuse. Ownership advances to the current epoch plus one,
including when recovery selects the original source. It never restores an old
ownership epoch or silently cancels on timeout.

Both endpoints' activation and boot exclusions remain held while independent
persistent course barriers deny all epochs through the reviewed current epoch.
Fresh process and evidence checks precede the ownership comparison and receipt
completion. The selected next epoch must satisfy both inspected barrier floors;
the controller does not silently choose a different epoch. Neither endpoint is
started automatically. Both conservative reservations remain counted until the
new receipt is complete, then only the selected endpoint's active reservation is
counted. The other endpoint's fenced identity and files remain retained.

The same review process can replace an ordinary pending transfer or a pending
recovery decision whose inspected evidence has changed again. Each replacement
links the exact prior row, preserves every earlier decision and consumes a fresh
transaction identity. A PREPARED decision has not advanced ownership yet; a
COMMITTED decision has. The current ownership epoch determines the next epoch.
Exact resume uses the same plan; changed evidence requires a new review. No
timeout releases reservations or opens admission.

Short immediate SQLite transactions serialize authority writes; backend locks
and observations happen outside SQL transactions. This follows the existing
single-controller storage boundary and [SQLite transaction guidance](https://www.sqlite.org/lang_transaction.html).
It provides neither distributed high availability nor protection against an
administrator restoring every trusted authority and barrier to an older snapshot.

## Controller and operator boundaries

This is a trusted-controller Python API, not a student-facing recovery endpoint.
`reconcile_preview(course_id, selected_worker_id, new_private_plan_path)` writes
a new plan for review. `reconcile_apply(plan_path)` applies that exact plan;
`reconcile_resume(plan_path)` retries the same decision after interruption.
Preview does not start or stop services or change authority ownership.
Use current controller tools on the same private authority. Existing version-1
authorities remain readable by the updated controller; older controllers must
refuse authorities containing new recovery records. Keep the original authority
incarnation and backing database. There is no authority recreation or data reset.

Every backend fencing context must call the supplied authorization callback after
holding both endpoint exclusions and before changing a barrier or stopping a
process. This prevents an old controller that waited for a lock from stopping the
newly selected owner after recovery. Older backend interfaces are refused; updating
only the controller without its trusted fixture adapter is insufficient.

Before selecting an endpoint, inspect both retained copies and their fresh backup
markers privately. Treat the fixture's opaque snapshot markers as test evidence;
they do not prove an encrypted or off-host backup. Keep every unselected copy for
later operator review. A reply lost after durable completion is distinct from a
failed decision; inspect current state and retry only the exact reviewed plan.

The disposable backend publishes each barrier and its separate history index in
two durable file writes. Interruption before the second endpoint's barrier write
can resume the exact decision. An arbitrary failure between a barrier write and
its matching index write instead stays closed; this fixture has no automatic
repair for that uncertain state. A real adapter needs atomic durable barrier
authority or a separately reviewed repair mechanism. The rehearsal retains this
deliberately unresolved failure control rather than claiming every backend fault
has recovered.

## Disposable test

From a clean checkout using Python 3.12, choose a new private output path:

```sh
python3 course-workspace/rehearsal/verify_pending_reconciliation.py \
  --output /tmp/NEW_PRIVATE_RECONCILIATION_FIXTURE
```

The runner publishes only bounded `PENDING-RECONCILIATION-PASS.json` after all
required controls pass and its exact owned processes and fixture files are
cleaned up. Private plans, complete authority state, worker ledgers and raw logs
are excluded. Source revisions, fixture hashes and explicit scope flags remain
in the report. Existing ordinary ownership controls retain their own separate
runner and evidence. This component test does not establish hosted acceptance,
production Python/R transfers or measured class capacity.
