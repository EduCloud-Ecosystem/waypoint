# Worker-channel Python/R notebooks and Guardian enforcement

This opt-in runtime connects the existing socketless JupyterHub spawner and local
Guardian to the authenticated worker channel. `remote_runtime_service.py` keeps
ownership and managed credential decisions centrally. `worker_runtime.py` keeps
Docker, course homes, runtime barriers, quarantine and local enforcement on each
worker. Existing local runtime and ownership-core behavior remain unchanged.

## Authorization and failure boundaries

A Hub or Guardian authenticates to the central service with its managed client
certificate. The central service takes the existing course admission lock through
`RemoteBackend`, checks the exact worker binding, then dispatches a scoped runtime
operation. Before effects, the worker requests fresh central authorization over
that same held connection. Requests bind the session, sequence, command, principal,
worker, course and epoch. They are not reusable grants.

Central checks read only local configuration, CA, enrollment and ownership state
while answering a challenge. They never call the waiting worker recursively.
Worker checks retain fresh local storage/runtime identity, epoch floors, actual
Guardian identity and bounded heartbeat requirements. The worker adapter reuses
the already-held physical course lock through a private, thread-bound capability;
ordinary callers cannot create an admission bypass.

The local Guardian and worker supervisor are separate enforcers. Loss of the
central service prevents heartbeat renewal. Loss of the worker agent prevents
renewal through its channel; the Guardian can still stop local learners. Loss of
the Guardian is observed by the worker supervisor. These paths stop exact owned
containers and retain coursework. Simultaneous loss of both local enforcers is
not solved by this architecture; independent host fencing remains a separate gate.

Brief Guardian admission-lock contention can return a deferred response. It does
not renew a heartbeat or ownership epoch. Quarantine, corruption, authentication
failure and an error after acquisition do not become deferred success. No network
wait holds a shared runtime SQLite writer transaction.

Worker response waits for notebook stop and replacement spawn include the existing
five-second Docker shutdown allowance. Certificate and absolute session deadlines
still govern every effect. Explicit stop completion requires removal of the owned
container before coursework staging begins.

## Private configuration and credentials

The central configuration has exactly `version`, `control`, `runtime_bindings`
and `service`. The version is `educloud-remote-runtime-v1`; `control` is the
[existing independent-worker configuration](INDEPENDENT-WORKERS.md).
`runtime_bindings` maps each configured worker to its freshly established worker
configuration/home-binding SHA-256. `service` contains `bind`, integer `port`, and
absolute paths `certfile`, `keyfile`, `ca_file`, `acl_file`, `replay_directory` and
`enrollment_directory`. Configuration, state and key material must be private.

Managed enrollment is mandatory. Use `remote_credentials.py --config PATH` with
`create`, `status`, `preview` and `apply`. Preview actions are `enroll`, `stage`,
`activate`, `retire`, `revoke` and `revoke_worker`, using the existing explicit
review digest and compare-and-swap lifecycle. The registry is never silently
created by service startup. Granting access requires fresh worker observations.
Denial-only changes and status use the pinned local registry/configuration/CA and
registered worker records, allowing access to be revoked while a worker is offline.
Removing access is not proof that an unreachable host has been physically fenced.

Central startup validates its local pinned state without requiring every worker
online. Each runtime request verifies its own worker under held admission. A
healthy worker can therefore remain usable when another worker is unreachable;
there is no automatic transfer of the unreachable worker's courses.

```sh
python course-workspace/remote_runtime_service.py --config /private/remote.json
python course-workspace/worker_runtime.py --config /private/runtime.json \
  --journal /private/journal --certfile /private/server.crt \
  --keyfile /private/server.key --ca-file /private/ca.crt \
  --acl-file /private/channel-acl.json --host 127.0.0.1 --port 8444
```

The returned notebook address is worker-local and must be reachable by its Hub;
it is not a global cross-host address. Keep Docker sockets off the controller and
Hubs. Guardians need their own runtime state and local Docker access, not central
ownership state or other workers' homes.

## Repeatable local acceptance

The rehearsal uses one Docker daemon, two logical workers with disjoint state,
real mutual TLS, three socketless Hubs, actual Python/R kernels and independently
running Guardians. Trusted setup and recovery helpers are short lived. The central
service itself has neither a Docker socket nor course-home mounts. Synthetic
coursework is copied once to an empty stopped receiver using the existing archive
validation helper; this is local staging, not a network backup transport.

The v3 rehearsal selects a fixed fixture profile from the verified daemon CPU
count. The total below counts all three Hub/learner pairs and all five control
services, including the stopped replacement endpoint's full allowance.

| Daemon CPUs | Profile | Each Hub | Each learner | Central | Each worker | Each Guardian | Total caps |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 4 | four-cpu-remote | 0.5 | 0.1 | 0.5 | 0.5 | 0.25 | 3.8 |
| 5 | five-cpu-remote | 0.75 | 0.2 | 0.5 | 0.5 | 0.25 | 4.85 |
| 6–7 | six-cpu-remote | 1 | 0.25 | 0.5 | 0.5 | 0.25 | 5.75 |
| 8+ | eight-cpu-remote | 1 | 0.25 | 1 | 0.75 | 0.25 | 6.75 |

These are complete shared-daemon **actual container cap budgets**. The public
allocation field `reserved_cpu` records that total; it is not an aggregate
immutable ownership reservation proof. The ownership core still conservatively
reserves one CPU per Hub. On four- and five-CPU hosts, that logical Hub reservation
exceeds the actual Hub cap. Each worker configuration separately reserves its own worker
service and Guardian plus half the central service (one CPU per worker), while
the fixture checks the complete actual cap total against the daemon before
bootstrap. These local views must not be added together and presented as a
shared-host ownership capacity proof: for example, four-CPU logical course
reservations plus the global controls would total 5.3 CPUs, despite actual caps
totaling 3.8. At eight CPUs and above, the existing per-worker conservative
control reserve and actual caps remain unchanged.

This bounded one-daemon experiment changes neither production allocation nor
heartbeat, RPC, or failure-detection deadlines. It tests a control-service
resource hypothesis; it does not establish the cause of earlier kernel 403s or
claim that the prior failures are resolved. Older v2 reports retain their
historical profiles and are not relabeled as v3 evidence. Other rehearsals keep
their existing presets. A passing run does not establish capacity for another
profile or for a classroom. Cleanup verifies immutable
container identities and removes control processes before learners, so active
enforcers cannot compete with fixture teardown.

```sh
docker build -t waypoint-authority-hub:local course-workspace/hub
docker build -t waypoint-authority-course:local course-workspace/course
docker build --build-arg HUB_IMAGE=waypoint-authority-hub:local \
  -t waypoint-authority-controller:local \
  -f course-workspace/controller.Dockerfile course-workspace
python course-workspace/rehearsal/verify_remote_notebooks.py \
  --output /tmp/waypoint-remote-notebooks
```

Only a completed evidence report establishes which controls passed. Failed
attempts remain evidence. This fixture does not provision a host or establish
institutional authentication, cloud attestation, independent-kernel isolation,
encrypted off-host restore, resource-exhaustion safety or classroom capacity.
After local acceptance, the next gate is a synthetic Droplet rehearsal, followed
by a second independent host for loss/transfer/restore acceptance before students.
