# Waypoint build roadmap

October 2026 course-computing track. The immediate consumer is a maintained,
portable browser Python/R course. The older Coolify publishing/federation plan
in PLAN.1.md is a separate track; completing this course worker does not imply
that multi-tenant hosting or federation is implemented.

## 1. Operable course worker - current build

- Institutional worker setup without synthetic credentials.
- Startup checks for the actual native Docker worker, compatible images,
  configured memory budget, approved learner homes and effective quotas.
- Private, bounded health reporting for storage/inodes, backup freshness and
  outcome, quota observations, Hub readiness and verified certificate expiry.
- Disabled-by-default monitoring units, explicit local-alert semantics and an
  example configuration for institutional monitoring integration.
- Reviewed stopped-course roster and quota changes with retained removed homes,
  stale-plan refusal, interruption journals and resumable apply.
- Unit, real local HTTP/TLS and disposable Linux/XFS/Docker acceptance checks.

Exit: passing current-release automated checks and an operator runbook. Live
hosting, notification delivery, institutional identity and second-host recovery
remain acceptance tasks against the chosen infrastructure.

## 2. Easier coursework submission

Design the Cairn-Waypoint boundary before implementation: explicit file selection,
assignment/identity confirmation, immutable artifact digest, submission receipt,
retry/deduplication and visible failure recovery. Saving a notebook must never
silently publish it or overwrite a previously accepted submission. Preserve the
current manual transfer path until the new flow passes isolated and combined
rehearsals. This requires coordinated Cairn changes; shared login is not implied.

## 3. Course onboarding and lifecycle

Build a reviewable course setup manifest and validation flow that brings together
image versions, identity issuer/client, roster, storage, capacity, backup ownership
and support ownership. Follow with deliberate image-upgrade/canary workflows,
course retirement/export and institution-approved retention/deletion. Expand
from one course only after resource attribution, port/origin separation and
failure isolation are tested. Do not expose an unrestricted self-service Docker
control plane or presume that a pseudonym is an authenticated identity.

## 4. Hosted acceptance

Use a funded source worker and separately controlled backup destination. Record
actual identity/revocation and TLS acceptance, capacity measurements, recovery
objectives, off-host backup and replacement-host restore. Exercise the operator
alert destination and escalation procedure. No paid host or external notification
is created by the local build. Rehearse replacement before placing real student
work on the service.

## 5. Broader hosting and integration

Resume the existing Coolify tenant-onboarding work against an accepted host.
Coordinate Belay's identity/integration contract with its developer; do not
rebuild that component here. Portage and Outfitter integrate only through scoped,
versioned contracts after their own acceptance. Federation/catalog work remains
separate from course-worker reliability.

The sequence is dependency-driven. External infrastructure does not block local
implementation of the operator tools or submission contract, but it does block
claims of live institutional acceptance.
