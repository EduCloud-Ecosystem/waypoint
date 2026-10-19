# Reviewed local worker enrollment and certificate rotation

This opt-in component enrolls runtime credentials for workers already approved in
the immutable local runtime configuration and registered ownership authority.
It does not provision hosts, expand the course configuration, enroll an unknown
provider, or prove hardware identity. Its actual Docker rehearsal uses two logical
workers on one daemon and synthetic learners.

## What is recorded and enforced

A private SQLite registry pins the authority incarnation, runtime configuration,
CA certificate, home-root bindings, and each registered worker's configured
provider/boot identity and evidence. A worker gets one Guardian slot and one Hub
slot for each configured course. Roles and scopes derive from these approved slots;
credentials cannot supply additional worker/course permissions.

Every change has a reviewed plan ID, digest and expected revision. Applying it
checks current trusted binding observations, then commits the new credential
state, consumed approval and linked history in one transaction. Binding observations
occur before that transaction; this is not atomic attestation across databases or
providers. Runtime admission retains its own fresh checks under endpoint exclusion.
The managed policy also checks scoped worker/daemon, home-root, loaded configuration
and fresh pinned public CA bytes on runtime lookups without nested ownership SQL or content-tree
scans. A policy captures canonical public CA bytes at startup, then freshly reopens
and proves the current file matches on each lookup; it caches no authorization or
liveness decision. A PEM formatting change safely requires a service restart. Missing or changed managed state refuses runtime access rather than falling
back to the legacy ACL. Static operator grants remain separately controlled.

The backend retains public image-label metadata only for configured learner and
Guardian content IDs, after checking the returned image ID at startup. Unconfigured
Hub image metadata remains freshly inspected. Container identity, daemon identity,
permissions and heartbeat eligibility are checked independently of that metadata.

The lifecycle is explicit:

| Action | Result |
| --- | --- |
| Enroll | Activate a complete Guardian/Hub credential set for an approved worker |
| Stage | Record a new certificate for one slot; it has no runtime permission |
| Activate | Activate the staged certificate with a fixed 1–300-second old/new overlap |
| Retire | End an old overlap immediately |
| Revoke | Permanently deny a selected certificate |
| Revoke worker | Permanently deny all that worker's credentials; retain its history and reservations |

Certificate expiry and overlap deadlines are checked freshly on access and
Guardian supervision. Historical certificate and normalized public-key identities
are never reusable across managed Hub/Guardian slots, including same-slot renewal.
This first implementation requires fresh keys. Static operator key rotation is
outside this registry; an exact operator leaf cannot become a managed runtime
credential. The verifier accepts named-curve P-256 and standard RSA client leaves of at least
2,048 bits,
normalizes key identity, verifies current dates, explicit client-auth purpose and
non-CA constraints against the pinned CA. Explicit EC-parameter leaves, RSA-PSS leaves and other unsupported key algorithms
are refused.
[OpenSSL's verification rules](https://docs.openssl.org/3.6/man1/openssl-verification-options/)
explain the explicit trust-anchor and client-purpose checks.

Hub retirement refuses fresh Hub access. It does not itself guarantee termination
of an existing notebook while its Guardian remains authorized. Guardian expiry or
revocation, including worker revocation, invokes the existing exact-runtime stop
paths. Saved homes and ownership history remain. These are bounded polling controls,
not instantaneous or highly available enforcement.

Guardian observation uses a 0.1-second endpoint-acquisition budget. Only a known
acquisition conflict returns an authenticated, scoped busy reply with no epoch;
it renews no heartbeat or permission. The Guardian defers that observation and
tries again. Other transport, identity, authorization and storage errors retain
the exact-runtime stop behavior. The three-second RPC budget, ten-second heartbeat
eligibility age and continuous-lock-inability watchdog bound remain unchanged.
Eligibility age is not an absolute stop deadline: lock acquisition and Docker
stopping take additional time. Prolonged exclusion closes the authority service,
which can affect all courses; restart preserves saved homes and reviewed history.

## Trusted local operator workflow

Run this tool as the trusted local administrator with access to the private runtime
and service state. It has no remote self-enrollment endpoint. The service configuration
must explicitly include an absolute `enrollment_directory` beneath an existing
private directory. Create a fresh registry before starting the managed service;
creation refuses existing state. The default service without that field keeps its
existing ACL behavior. A present invalid/missing registry refuses managed startup.

```sh
python worker_credentials.py --runtime-config /private/waypoint/runtime.json \
  --service-config /private/waypoint/service.json create
```

A private mode-0600 certificate-source JSON for one worker maps canonical slots to
absolute public-leaf certificate paths, for example:

```json
{
  "guardian": "/private/worker-one/guardian/client.crt",
  "hub:course-main": "/private/worker-one/course-main/client.crt"
}
```

These files contain public certificates, not keys or certificate/key bundles.
Keep private client keys at their runtime destination. The tool verifies the public
leaf again when applying the reviewed plan; a replaced certificate or changed scope
cannot silently change the approval.

```sh
python worker_credentials.py --runtime-config /private/waypoint/runtime.json \
  --service-config /private/waypoint/service.json preview --action enroll \
  --worker worker-one --credentials /private/waypoint/worker-one-certificates.json \
  --output /private/waypoint/enrollment-review.json
```

Inspect the private review and its returned `review_sha256`, then apply that exact
review file and digest with `apply --plan-file ... --review-sha256 ...`. A repeated
or stale approval refuses; make a fresh review instead of resetting history.

For rotation, preview/apply `stage` with the selected `--slot` and a one-slot
certificate-source JSON. Preview/apply `activate` with the same new public leaf
and `--overlap-seconds`. Activation alone does not install a private key or update
an already-loaded Guardian TLS context. Install the approved certificate/key at
the worker using a controlled participant stop and restart, test the new identity,
then retire the old overlap. Service restart or notebook recreation may be needed;
no seamless restart or zero-downtime guarantee is claimed. `status` reports bounded
counts. Worker revocation has no automatic undo or re-enrollment shortcut.

## Repeat the actual local test

Use the clean reviewed checkout and the disposable environment described in
[the authenticated runtime guide](AUTHENTICATED-RUNTIME.md). Automatic selection
on a daemon with at least six CPUs uses the six-CPU profile and requires at least
6,656 MiB. Its controller receives one CPU, and the
fixture reserves 1.5 CPUs for the controller, both Guardians and overhead;
the complete reservation is 5.25 CPUs. The four/five-CPU Linux profiles retain
their 0.4-CPU controller. Reports record these selected limits and compare them
with actual Docker quotas. These describe disposable component tests; representative
class capacity requires its own rehearsal. Capacity preflight checks installed
daemon resources and fixture limits; it does not reserve idle resources against
other containers or host processes. Run acceptance without competing local builds
or test suites. Build the Hub/course/controller images
using that guide, then:

```sh
python course-workspace/rehearsal/verify_worker_enrollment.py \
  --output /tmp/ENROLLMENT-RUNTIME.json
```

Add `--profile-cpus 4` to reproduce the existing CI service caps on a larger local
daemon, including its 0.4-CPU controller and 0.1-CPU Guardians. Both authenticated
runtime drivers accept existing profiles 4, 5 and 6 only when the selected profile
fits the actual daemon. Authenticated reports v4 and enrollment reports v3 record
`profile_cpus` separately from actual host capacity (`null` means automatic).
Historical report versions retain their original rules and cannot be relabelled
as selected-profile evidence. This does not isolate a physical four-CPU host.

The test creates a fresh registry, proves legacy runtime ACL entries cannot bypass
unenrolled status, enrolls reviewed worker credentials and executes Python/R with
saved files. It checks inert staging across authority restart, old/new mutual TLS
overlap, retirement, actual credential installation, and restored coursework.
It also holds a real endpoint lock for four seconds: busy responses leave the
heartbeat unchanged and preserve both notebooks. A thirteen-second exclusion
proves heartbeat expiry and authority shutdown, followed by saved-work recovery.
The brief-hold recovery probe records only fixed release, holder-exit and
heartbeat booleans. Malformed replies, expired heartbeats and probe exceptions
fail explicitly rather than being swallowed by a generic readiness wait. The
four-second hold, ten-second recovery wait and production deadlines are unchanged.
It lets an old Guardian's five-second overlap expire without an operator retirement
call, confirms its worker's notebook stops while the peer worker continues, then
installs the replacement and reopens saved work. Worker revocation and service
restart preserve prior ownership decisions and enrollment state. An intentional
authority outage may correctly replace ephemeral notebooks; continuity means saved
coursework across that outage, and exact peer-runtime continuity during worker-local
failures while authority remains available.

The report has ten ordered controls, twenty reviewed source fingerprints, recipe
and image hashes, actual capacity/allocation and verified owned cleanup. The existing
legacy twelve-control runtime test remains separate. The CI workflow runs them
sequentially. Dirty runs are diagnostics only and cannot establish acceptance.
Public reports omit credentials, plans, raw logs, private state and learner contents.
The disposable issuer is co-located with the privileged fixture controller and its
temporary keys; the registry and review plans contain only public certificate metadata.
This test does not demonstrate production issuer/process isolation.

## Remaining adoption boundaries

CA/server certificate rotation, an operated issuer, institution OIDC/HTTPS,
independent-host/provider enforcement, encrypted off-host recovery and representative
class load remain separate work. Private state requires reliable storage and clock
operations. Inode/sentinel/history/clock checks detect ordinary loss, substitution,
corruption and time regression; they do not prove recovery from a privileged complete
state-image rollback. Reaching plan/event quotas refuses further credential changes;
existing valid credentials remain usable until expiry or revocation. Missing, corrupt
or oversized private state can refuse runtime lookup. There is no automatic pruning,
reset, tombstone deletion or reused credential identity.
Long-lived capacity and recovery need an operating policy before hosted adoption.
