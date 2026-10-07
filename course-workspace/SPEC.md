# Python/R continuity pilot (2026-10-06)

Cairn remains the classroom control plane. This Waypoint deployment supplies a
browser workspace using JupyterHub and DockerSpawner; it does not require an
individual GitHub benefit, a paid IDE subscription, or an Outfitter broker.

One course per deployment. The operator supplies a maintained Docker host,
capacity, a course image, Keycloak realm/client, approved subject IDs, and a
separate HTTPS wildcard domain for learner content. Identity is independently
validated by the Hub. A Cairn link is navigation, never a login credential.

Each learner gets a non-root Python/R container, an isolated internal network,
and a persistent home volume. CPU, memory, processes, simultaneous servers,
idle time and maximum session age are bounded. Idle expiry stops compute; it
never deletes home data. Home data and the Hub database can be copied into a
provider-independent recovery archive after all course containers stop.

No arbitrary images, privileged learner containers, Docker socket in learner
containers, shared learner volume, or fallback to unauthenticated public mode.
Only synthetic alice/bob accounts are available in explicit loopback test mode.
Student code has no network egress: dependencies belong in the course image and
files enter through the authenticated notebook upload UI. Direct Git push/pull
from a running notebook is deliberately not part of this first pilot.

Completion evidence: browser login, Python and R kernel execution, two-user
separation, stop/recreate persistence, a checksum-verified archive restored to a
fresh deployment namespace, resource limits, and rejection of incomplete auth
configuration. Production readiness additionally requires live institutional
OIDC, wildcard HTTPS, target-host capacity/load testing, off-host encrypted
backup storage, a restore drill, filesystem quotas, and a named operator.
The shared host kernel remains a boundary to review before untrusted deployment.

This is a portable environment and recovery path, not automatic failover, a
promise of free compute, or a finished multi-institution service. No existing
Cairn assessment-integrity finding is resolved by adding a workspace.
