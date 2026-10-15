# Independent worker control and reviewed recovery

This opt-in adapter moves course ownership inspection and fencing behind a
worker-local authenticated service. Each worker keeps its own storage, course
locks, monotone revival barriers and durable admission quarantine. The controller
keeps the existing ownership authority and reviewed-plan history. It does not
mount remote homes or expose Docker over the network.

The first acceptance target is **two separate local worker processes**, real
mutual TLS and disjoint private state, with supervised synthetic learner processes.
This establishes the control protocol and interruption/recovery behavior. Actual
two-host isolation, Python/R notebook traffic, cloud identity attestation, encrypted
off-host backup transfer and representative course capacity need later acceptance.

## Safety and recovery contract

The `RemoteBackend` implements the existing ownership backend interface without
changing `ownership_contract.py`. A transfer acquires both worker-side course
locks in stable order. Fresh authorization under those held channels precedes any
fencing effect. Both workers then persist an admission quarantine and a floor
denying the old ownership epoch; the source's exact owned services are stopped.
The receiver must already be stopped. No transfer starts a receiver automatically.

The controller checks fresh worker identity, course evidence, held-session proof
and source quiescence before the ownership compare-and-swap and completion receipt.
An explicit successful completion command clears each worker's quarantine only
after the authority operation finishes. The worker trusts its authenticated,
course-scoped controller to make that completion assertion; it does not independently
read the controller's private SQLite database.

Disconnect, timeout, expired/revoked controller access, missing/corrupt journal or
agent restart never clears quarantine. A session timeout releases its physical
lock but cannot turn a fenced endpoint back into an eligible worker. Both coursework
copies and the original ownership history remain retained. Unreachable workers
are not evidence of a completed fence.

| Observed condition | Operator action |
| --- | --- |
| Transfer interrupted and reviewed inputs unchanged | Restore connectivity, then apply the exact original plan ID and digest again |
| Authority already records COMPLETE, but a worker missed completion | Apply the same completed plan again; fresh observations and both fences are checked before clearing quarantine |
| Pending transfer with changed retained coursework | Preserve both copies, produce a fresh reconciliation preview, review the selected original endpoint and apply that new plan |
| Changed worker boot, policy, storage binding or authority incarnation | Leave admission closed; do not edit identity records or recreate journals to force an old approval |
| Source remains unavailable | Keep the transfer pending and both reservations; do not infer a fence from a failed connection |

There is no reset, automatic rollback, deletion, re-enrollment, snapshot overwrite,
authority recreation or quarantine-expiry shortcut. The current workflow recovers
ownership over already-staged copies; it is not a backup transport or archive
restorer. A whole trusted-state rollback or malicious privileged controller is
outside this single-authority model.

## Bounded authenticated channels

`worker_transport.py` uses certificate-chain verification, hostname verification
and an explicit server-certificate fingerprint. The worker rechecks its private
controller certificate allowlist and course scope for every command and before
effects. Fresh TLS session challenges and increasing command sequence numbers
bind requests and replies to one connection; there are no automatic request retries
or reusable grants. Messages are limited to 64 KiB, sessions to 128 commands and a
default 20-second absolute lifetime, with a three-second socket timeout and eight
concurrent sessions by default.

`worker_channel_state.py` retains a private, linked SQLite quarantine history
separate from runtime barriers. Existing state is required on service startup;
ordinary missing/replaced/corrupt state refuses. History and storage are bounded;
exhaustion refuses mutations rather than pruning evidence. Certificates, private
keys, command payloads, raw errors and learner contents are excluded from output.
The TLS implementation follows Python's [explicit client and server context
requirements](https://docs.python.org/3.12/library/ssl.html).

The worker service CLI accepts exactly one configured local Docker worker. It
reuses that worker's existing runtime configuration and barrier database; the
Docker socket and homes stay on the worker. The CLI validates the Linux kernel
boot identity freshly against the configured worker identity. Configured provider
IDs and capacity observations do not establish cloud attestation or exclusive
host capacity.

## Trusted local operator

Keep the controller configuration, authority, plan directory and TLS key material
private. The configuration has version `educloud-independent-workers-v1`, the
existing `authority_id`, absolute `authority_directory` and `plan_directory`, and
a `workers` mapping. Each worker entry contains:

- `evidence`: its exact registered ownership worker record;
- `courses`: the explicit list of permitted course IDs;
- `channel`: `host`, `port`, `server_sha256`, `ca_file`, `certfile`, and `keyfile`.

The worker allowlist uses version `educloud-worker-channel-v1`, `authority_id`,
`worker_id`, and `controllers`, mapping approved client certificate SHA-256
fingerprints to course-ID lists. This is a separate privileged controller channel;
Hub and Guardian enrollment credentials do not grant controller access.

```sh
python course-workspace/remote_control.py --config /private/controller.json inventory
python course-workspace/remote_control.py --config /private/controller.json \
  preview --course course-main --receiver worker-two
python course-workspace/remote_control.py --config /private/controller.json \
  apply --course course-main --plan-id REVIEWED_ID --plan-sha256 REVIEWED_DIGEST
python course-workspace/remote_control.py --config /private/controller.json \
  reconcile-preview --course course-main --selected-worker worker-one
python course-workspace/remote_control.py --config /private/controller.json \
  reconcile-apply --course course-main --plan-id NEW_REVIEWED_ID --plan-sha256 NEW_REVIEWED_DIGEST
```

Plan files remain in the configured private directory. The CLI exposes no learner
launch, arbitrary remote command or caller-selected remote filesystem path.
Inventory reports authority reservations and each worker/course quarantine
separately: an authority COMPLETE decision is not sufficient proof that both
workers acknowledged completion. Core summaries retain their original fixture
flags; the separate rehearsal report describes the transport actually exercised.

The service's optional programmatic runtime callback runs under its existing
activation lock and must check the supplied authorization callback immediately
before effects. The default CLI leaves this callback disabled. Do not directly
invoke the old `RuntimeDispatcher` from it: that dispatcher would reacquire the
held activation lock. Production remote Hub/Guardian and managed enrollment
integration require a dedicated adapter and their own runtime acceptance.

Quarantine gates worker-channel activation, not every legacy local Docker path.
Do not attach this control service to an operating course with another controller,
legacy Hub or automatic-start path that can bypass that gate. The current component
rehearsal has exclusive control of its disposable services.

## Repeat local acceptance

From a clean checkout with Python 3.12 and OpenSSL:

```sh
python course-workspace/rehearsal/verify_remote_workers.py \
  --output /tmp/NEW_PRIVATE_INDEPENDENT_WORKER_RUN
```

The runner creates disposable credentials, two independent worker-server processes,
private databases and synthetic saved coursework. It tests normal transfer,
controller interruption, connection loss, exact resume, divergent-copy
reconciliation, peer continuity and restart persistence. Listener reconnection
preserves the running peer; actual agent-process restart is tested only after an
explicit peer stop and proves saved-file recovery, not uninterrupted peer execution.

Only the bounded report is suitable for sharing. A PASS requires every listed
control, a clean unchanged source revision and verified owned-process cleanup.
The runner also records bounded failure results. These reports describe local
component evidence and must not be relabeled as hosted deployment or course readiness.
