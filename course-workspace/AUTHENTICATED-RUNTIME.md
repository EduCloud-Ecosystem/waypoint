# Authenticated authority and local notebook enforcement

This opt-in build connects the reviewed ownership protocol to actual local
Docker/JupyterHub/Python/R notebooks. It uses one Docker daemon with two logical
worker namespaces. It does not establish independent-host fencing, provider
integration, hosted course acceptance, or production readiness.

## Access and effects

The authority accepts a fixed bounded JSON protocol over mutually authenticated
TLS. Each certificate has an explicit operator, Hub, or guardian role and worker
and course scope in a private ACL. Clients pin the server CA, hostname, and
authority incarnation. Every request has a short validity window and globally
unique nonce. Replay tombstones survive service restart; the ledger refuses new
requests at its bounded capacity. It never prunes or silently recreates state.
No response is a reusable admission grant. Unknown fields, credential revocation,
wrong scopes, lost state, and unavailable authority refuse access.

Operators preview a server-owned plan, review its returned digest and summary,
then apply that exact plan reference. Requests cannot select arbitrary filesystem
paths. Both endpoint scopes must be authorized. Fresh principal checks protect
SQL mutations and run again after acquiring endpoint exclusion before any
barrier or Docker effects. Ordinary and recovery records retain their linked
history. Runtime barrier floors and their history update in one SQLite transaction.

An authority-local Hub has no Docker socket. Login, cookie refresh, notebook
spawn, and polling consult the authenticated authority. The service creates and
starts an approved notebook inside the same ownership admission exclusion,
checking the expected epoch and fresh Hub and guardian authorization before
effects. Image, subject roster, resources, environment, bind home, and internal
network come from trusted configuration. Arbitrary student Docker options are
rejected. Stops carry the saved exact container ID and epoch, so old Hub state
cannot target a new replacement. Changed spawn credentials cause safe recreation
of the owned ephemeral container while preserving its home.

Each worker has an independent trusted guardian with its own scoped certificate.
It stops exact owned notebooks when fresh ownership observation fails. The
authority independently watches the guardian's live Docker ID, start identity,
fresh heartbeat, and ACL, and stops notebooks if that guardian dies, stalls, or
is revoked. Healthy supervision also checks the fixed notebook policy. A stop
disables restart before removing the ephemeral container; saved homes remain.
The guardian and authority are privileged host components; their Docker sockets,
keys, ACL, and state never enter a learner notebook.

This is bounded polling enforcement, not an instantaneous or highly available
guarantee. Simultaneous authority and guardian failure, Docker daemon unavailability,
host compromise, and runtime escape remain outside its guarantee. A healthy
guardian polls each course independently (at most 16 course loops per worker),
so a slow course observation cannot delay a sibling course's heartbeat. An
unexpected loop failure exits the guardian process and invokes the authority's
fail-closed supervision. A healthy loop normally polls once a second; its RPC timeout is three seconds and
heartbeat validity ten seconds in the native rehearsal. Stop completion also
depends on Docker.
Hub authority requests have a ten-second bounded socket timeout so container
creation can complete; guardian observation retains its three-second timeout.
The opt-in Hub allows 120 seconds for notebook HTTP startup. Browser readiness
requests allow ten seconds and eight redirects; its retry deadline can be
overrun by a bounded in-flight request. These are test readiness limits, not
authorization leases or performance guarantees.
When Docker confirms a previous learner runtime has stopped, the harness waits
up to 45 seconds for the authenticated Hub self API to clear that server's
metadata before requesting a replacement. User notebook URLs do not implicitly
spawn; this wait allows Hub's normal polling cleanup and changes no runtime policy.
The initial Guardian snapshot waits at most one second for endpoint exclusion;
busy cycles retry after 0.1 seconds without issuing a heartbeat. Pending stops
retain 0.1-second attempts. Watchdog checks start with that quick attempt; after
repeated contention they wait up to one second for an available gap, clipped to
the remaining ten-second failure window. Neither path renews authorization.
A ten-second continuous-inability threshold, evaluated on watchdog cycles,
closes the authority service; this is not a strict wall-clock stop deadline.
The guardian keeps exact pending stops and retries when
exclusion becomes available; it issues no heartbeat while those stops are pending.
This does not guarantee recovery from permanently held locks or loss of both
control paths.

## Repeat the local test

Use a disposable local Docker environment with at least 6,656 MiB and four CPUs
available to the daemon. Static namespace reservations include all three Hubs
and their possible learner slots, including the standby receiver. The control
plane uses at most 512 MiB and 0.6 CPU; the remaining reserve covers host overhead.
Each synthetic learner gets 512 MiB and 0.1 CPU; each Hub gets 1,024 MiB
and one CPU. The three namespaces reserve 3.3 CPUs, with 0.7 more reserved
for the control plane (0.6 CPU) and host overhead. A measured cold notebook
startup under this allocation took about 100 seconds. These limits demonstrate
enforcement, not class capacity or interactive performance.

```sh
python -m pip install -r course-workspace/requirements-test.txt
docker build -t waypoint-authority-hub:local course-workspace/hub
docker build -t waypoint-authority-course:local course-workspace/course
docker build --build-arg HUB_IMAGE=waypoint-authority-hub:local \
  -t waypoint-authority-controller:local \
  -f course-workspace/controller.Dockerfile course-workspace
python course-workspace/rehearsal/verify_authenticated_runtime.py \
  --output /tmp/AUTHENTICATED-RUNTIME.json
```

Run from a clean reviewed checkout. The harness refuses built Hub/controller
modules whose bytes differ from the checkout and records immutable executed
image IDs, source revision, code/recipe hashes, controls, and verified cleanup.
`--allow-dirty` is for development diagnostics only. Generated certificates,
passwords, Hub tokens, private plans, and raw container logs are excluded from
public evidence. Cleanup selects only this invocation's names and labels;
course and Hub images are retained. No real learner data or external provider
calls are involved. The dedicated GitHub workflow repeats this on disposable
Linux infrastructure without deployment credentials.

The fixture captures its original host creator and directory identity before
bootstrap. Disposal stops its Hub, learner and guardian writers and verifies
the service has exited before returning private bind files to that creator.
Descriptor traversal refuses path escapes, hardlinks and unexpected mounts;
private files remain mode 0600 and directories 0700. The controller is removed
last, followed by owned networks/volumes and the host directory. Any failed
handoff or remaining resource makes cleanup fail; running the whole harness
as root is unnecessary. Service stopping uses a Linux process descriptor and
rechecks start identity so a recycled process ID cannot receive its signal.

Private authority, replay, and plan state uses a Docker-managed Linux volume;
backend locks, barriers, and heartbeats use a separate private Linux volume.
Guardians mount only the backend volume. Fresh private child directories retain
the same sentinel and inode checks across service restarts. Learner homes remain
on their original host bindings. This follows
[Docker Desktop's database-storage guidance](https://docs.docker.com/desktop/settings-and-maintenance/settings/).
The fixture removes these owned volumes only after stopping its participants;
this is local persistence, not off-host recovery.

The rehearsal runs actual Hub authentication and Python/R notebook WebSockets,
saves files, interrupts a transfer after its durable PREPARED record, restarts
the service, introduces a reviewed synthetic snapshot change, and reconciles
to the receiver. It verifies old and recovery records remain, the peer course
continues, source Hub restart cannot revive the old epoch, and the receiver
opens saved coursework. It also tests authority outage, guardian death and
revocation, fresh Hub credential refusal, persistence, and precise cleanup.
The interruption exists only in the disposable fixture helper; the service
entrypoint contains no fault-injection or bootstrap mode.

## What remains before hosted adoption

Keep the default DockerSpawner deployment until an explicit hosted adoption
decision. The controller/guardian image and test PKI are local rehearsal tools,
not a turnkey production certificate/deployment system. A hosted adapter needs
trusted real worker identities, physically independent endpoint fencing, worker
enrollment, certificate issuance/rotation/revocation operations, institution OIDC
and HTTPS verification, hard disk/inode quotas, encrypted off-host recovery,
durable private state operations, and representative course-load testing.
The 100,000-entry permanent replay ledger and bounded barrier/event histories
also need an explicit long-lived operating and capacity strategy; exhausting
them refuses access rather than deleting history or resetting the authority.

Reviewed transfer proofs inspect bounded stopped-home evidence (10,000 entries
and 64 MiB aggregated per course, with a depth limit of 64). Concurrent changes,
oversized trees, or unsupported special files refuse that transfer proof. Routine
admission uses fresh trusted runtime metadata and pinned home-root identity;
it does not issue or substitute a retained-work proof. These workload bounds
require representative course validation before hosted use. Learner roster
revocation and institutional identity/session invalidation are separate from
privileged client certificate revocation.

Protocol references: [Python TLS verification](https://docs.python.org/3.12/library/ssl.html),
[JupyterHub Spawner contract](https://jupyterhub.readthedocs.io/en/stable/reference/spawners.html),
[Docker restart policy behavior](https://docs.docker.com/engine/containers/start-containers-automatically/).
