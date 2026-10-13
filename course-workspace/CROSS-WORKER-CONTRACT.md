# Cross-worker ownership and fencing contract

This build establishes an executable, standalone transfer contract and a local
rehearsal with two logical workers. Its privileged controller owns a private
SQLite authority; worker admission derives from that authority rather than a
copied local ledger. The fixture uses owned synthetic service processes and a
course-specific fencing backend. It has no production enrollment/startup adapter,
remote authenticated transport or cloud-provider adapter. It must not be used to
activate a real restored course.

## Required ownership invariants

- One logical course has one owner and a strictly increasing ownership epoch.
  The authority incarnation and registered worker identities are pinned.
- DNS-subtree and identity-client collisions are checked across courses. Physical
  identity is distinct from logical identity; identical physical names on separate
  workers are not proof of shared storage. Actual path/mount/port verification
  remains a production-adapter responsibility.
- A reviewed generated plan binds the current owner/epoch, source and receiver
  incarnations, resource profile, runtime and retained-tree fingerprints and
  backup-provenance contract fields. Changing those inputs invalidates approval.
- Receiver capacity is reserved while the full source reservation is retained.
  Uncertain completion cannot silently release either reservation.
- Both pending endpoints stay closed to new admission. Ownership changes only
  after an independently checked, persistent source revival barrier. Fencing and
  source activation share an exclusion context; both endpoint activation/boot
  exclusions stay held through the ownership CAS and receiver eligibility recheck.
  Fencing adapters reauthorize the pending/current decision after taking both
  exclusions and before stopping processes or changing barriers.
- A missing authority, missing history, stale approval, wrong worker or unavailable
  fencing backend refuses. Stop acknowledgments, unreachable guests, signed
  worker self-reports and historical power-off events alone are insufficient.
- Peer courses remain outside a course-specific barrier. Whole-worker fencing has
  different operational impact and is refused by this per-course prototype.
- No transfer deletes original work, resets a divergent tree or automatically
  starts a receiver. Further movement requires a fresh reviewed decision.

Fixture admission yields a local controller snapshot while endpoint exclusion is
held. It is not a reusable capability, signed transport token or authorization
credential. Only the trusted fixture controller starts its owned process inside
the matching admission context. Raw fixture launch primitives are not exposed to
students or presented as a production security boundary.

## Controller storage boundary

Use a private local filesystem for the controller database. SQLite transactions
serialize its ownership writes; short immediate transactions and expected-epoch
comparisons arbitrate concurrent decisions. The prototype uses rollback-journal
DELETE mode and synchronous EXTRA, with bounded lock waits. These choices are
supported by [SQLite isolation](https://www.sqlite.org/isolation.html) and
[SQLite synchronization guidance](https://www.sqlite.org/pragma.html#pragma_synchronous).
Do not share this database over a worker network filesystem or manually copy,
replace or remove an open database or hot journal.

SQLite does not supply distributed high availability or protection against restoring
all trusted state to an earlier snapshot. Ordinary missing/reset/replaced authority
state must not bootstrap enrolled workers. Authority recovery requires a reviewed
new incarnation and worker reconciliation; it is not implemented by this prototype.
The fixture's persistent source barrier is independent of worker process state.
An administrator replacing all trusted authority and fencing state remains outside
its guarantee.

## Provider and runtime acceptance still required

A real adapter must enforce the revival barrier at the runtime/provider layer,
including existing automatically restarting Hubs and learner containers. Existing
Waypoint command-line admission alone is insufficient for a revived source VM.
The hosted design needs independently controlled authority durability, authenticated
fresh observations, current encrypted off-host backup validation, exact host/runtime
bindings and a tested monitoring/support path.

DigitalOcean documents graceful shutdown as a request whose success is not
guaranteed, and also exposes later power-on. An observed power-off is therefore
not an enduring ownership barrier. This is a design inference from the
[Droplet action API](https://docs.digitalocean.com/products/droplets/reference/api/droplet-actions/),
not an implemented or tested provider guarantee.

The local fixture's snapshot identifiers and backup fields exercise the provenance
contract with synthetic files. They do not establish encrypted/off-host recovery,
actual two-host isolation, production Python/R transfer or class capacity.
Existing same-worker and independent-worker recovery rehearsals retain their own
separate acceptance evidence.

## Run the disposable contract test

From a clean Waypoint checkout with Python 3.12, run:

```sh
python3 course-workspace/rehearsal/verify_ownership_contract.py \
  --output /tmp/NEW_PRIVATE_OWNERSHIP_FIXTURE
```

Use a fresh output path. The runner owns only its generated processes and private
fixture state. It publishes bounded `OWNERSHIP-CONTRACT-PASS.json` only after every
required control and verified cleanup. The companion unit tests check process
supervision, descriptor exclusion, private publication and false-acceptance gates.
Private plans, complete controller/worker records and raw logs are excluded.
Returned grants and evidence flags describe fixture behavior, not production
runtime or provider capabilities.

The controller permits exact pending resume and exact completed retry while
reviewed inputs remain unchanged. It has no automatic cancellation, unfence,
authority reset, destructive rollback or real-service startup operation. A later
reviewed move consumes a fresh epoch; changed retained data requires a fresh
snapshot and review rather than forcing an old approval.

The [pending-transfer reconciliation prototype](PENDING-RECONCILIATION.md) adds
a fresh reviewed choice of either original endpoint for pending
PREPARED/COMMITTED transfers and interrupted recovery decisions. Both divergent copies and the exact original history
remain retained. The new controller consumes a fresh epoch and conservatively
counts both endpoints through completion. The updated fencing backend must invoke
its authorization callback under both endpoint locks before any side effect.
Existing version-1 authorities retain their schema and incarnation; all controller
and fixture backend tools must be updated together. Authority recreation, worker
re-enrollment and production runtime/provider enforcement remain outside this
local prototype.
