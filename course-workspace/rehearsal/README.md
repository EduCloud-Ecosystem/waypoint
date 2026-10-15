# Synthetic identity-to-course rehearsal

This is a disposable acceptance harness, not a deployment command. It uses real
Keycloak and Forgejo instances with generated synthetic identities, the actual
JupyterHub runtime, and a pinned Cairn checkout. No institutional credentials,
cloud account, real learner record or existing deployment is used.

The workflow in `.github/workflows/course-rehearsal.yml` runs the complete path:

1. Create a new temporary loop image and mount XFS with project quotas. Provision
   three notebook homes with hard byte/inode limits. Only the newly created image
   is formatted; no caller-supplied block device is accepted.
2. Generate a one-day fixture CA/certificate, realm and confidential PKCE client.
   Start Keycloak, nginx and the current Hub on a private Docker network. Publish
   only the reverse proxy on an ephemeral loopback port.
3. Authenticate the instructor through Forgejo OAuth into Cairn. Create a
   roster-restricted course, assignment and pinned instructor grading policy.
4. Authenticate Alice through Forgejo, accept the assignment, and follow the
   actual Cairn workspace link. Authenticate separately through Keycloak into
   the notebook. These two identities are deliberately separate; there is no
   claim of unified sign-on or automatic roster synchronization.
5. Execute Python and R over HTTPS notebook WebSockets. Save `solution.py`, use
   JupyterLab's visible Cairn link to select its saved bytes, receive them in Cairn,
   preview the exact file digest and confirm. Lose the first confirmation response
   and retry the same receipt; there must be one accepted Git commit. Reject an
   incorrect Origin, peer learner, changed idempotency payload and unsafe path.
   Forgejo delivers its registered signed hook through a private fixture relay;
   no instructor Grade request is sent. Local-subset testing uses the same Cairn
   page with an explicit device upload instead of the full Jupyter bridge.
6. Follow the submitted commit with five rapid Git pushes, alternating correct/incorrect answers. Assert
   each grade records its own immutable commit and scores 10/10 or 0/10 despite
   the forged learner `grading.json`. Replay an actual signed delivery 12 times
   concurrently and once after restarting Cairn; the history must still contain
   six grades. Deny bad signatures and ignore a real draft-branch push. Inspect
   the learner-visible test breakdown and commit-specific history.
7. Reject an unrostered identity, student requests for instructor functions,
   another student's grade and another student's notebook file. Remove Alice
   from the Hub roster, stop active servers and recreate the Hub. Verify her
   previous notebook session and attempts to start another server are denied.
8. With two active notebooks, issue 24 authenticated content requests at up to
   six concurrent requests and execute Python/R simultaneously. A third allowed
   learner must receive HTTP 429; stop one server, then verify that learner can
   start, run Python and leave the other learner's saved file intact. Record
   observed request latencies, without claiming class-size capacity.
9. Remove only the invocation's containers/networks/volumes and unmount its
   temporary filesystem. Preserve the evidence in the supplied private output
   directory; a failed run must not produce the overall `PASS.json`.

The browser uses a fixture-only certificate exception; the Hub and Python/WSS
clients explicitly trust the generated CA and retain TLS certificate validation.
No trust certificate or hosts entry is installed on the user's computer. Chromium
and the HTTP client resolve only the fixture `.rehearsal.test` names locally.

## Run the repeatable pilot

Use the [operator runbook](RUNBOOK.md) for build prerequisites, exact local and
full-Linux commands, evidence interpretation and failure cleanup. The workflow
pins the accepted Cairn source and builds its dashboard, server, LMS simulator
and bounded checkout image before running the full pilot.

The current pilot extends the course flow above with:

- Stop/resume of a real notebook server, with retained source bytes and Python
  execution in a replacement container.
- Two real Forgejo learner identities, deterministic assessment proposals,
  browser instructor score adjustments, and learner-visible reviewed feedback.
  Unassessable, stale and unauthorized publication attempts must fail.
- Signed simulated LTI instructor/learner connections, score readback, a lost
  response recovered without duplicate writes, and preservation of an external
  grade conflict across restart.
- Five then fifteen distinct authenticated learners issuing 100 and 300 HTTP
  requests, ownership checks, latency observations and restart persistence.
  This read workload does not measure fifteen concurrent notebooks or grading.
- Exact source, binary, dashboard, fixture and image identities; an overall
  `PASS.json` only after all required phases and verified cleanup complete.

## Institutional recovery rehearsal

See [encrypted recovery](../RECOVERY.md#recover-an-institutional-course) for the
current-roster restore command and `verify_institutional_recovery.py` acceptance
harness. It destroys the synthetic source workspace and restores saved homes
onto a new XFS filesystem while the independent fixture IdP remains available.

## Course retirement rehearsal

The [retirement/export guide](../COURSE-RETIREMENT.md) describes the public CLI
and `verify_retirement.py --output /tmp/NEW_PRIVATE_DIRECTORY` acceptance path.
It closes a fresh synthetic course, exports retained work (including a removed
learner), verifies corruption controls and interruption recovery, restores its
protected snapshot into a closed namespace and applies actual Restic retention.
Only bounded `RETIREMENT-PASS.json` is uploaded; private exports, plans, identities,
environment and keys remain excluded. It proves component closure and saved-work
integrity on disposable Linux, not institutional retention or off-host acceptance.

## Remaining production gates

The [multiple-course rehearsal](../MULTIPLE-COURSES.md) runs two managed courses
with the same synthetic subjects on distinct origins/identity/storage. It checks
aggregate limits, cookie/file boundaries and second-course continuity through
saturation, revocation, retirement/release and protected closed restoration.
`MULTI-COURSE-PASS.json` requires every control and resource/authority cleanup.
It runs in its own fresh Linux CI job, parallel to the complete course/lifecycle
rehearsal. Only that bounded report is uploaded. This tests closed recovery, not live
replacement enrollment or class capacity.
Sequential identity cold starts can outlast quota-health freshness. The fixture
verifies actual quotas before login callbacks and explicit spawns; it does not
extend the production freshness gate. Failure diagnostics expose only bounded
HTTP, container-state and quota-health scalars.

Passing this harness does not establish institutional identity acceptance,
class-size capacity, uptime or provider disaster recovery. Select the actual
worker and off-host backup account, rehearse real roster changes, certificate
renewal/reboot, quota monitoring and incident response, then repeat the course
path with institution-approved synthetic accounts before enrolling learners.
Cairn/Hub roster synchronization and automatic submission on save are not implemented.
Selected-file handoff is explicit and separately authenticated; it is tested here.
Actual Forgejo Git push/webhook delivery is now exercised synthetically, including
six queued revisions and replay after restart. Other hosts' live webhook paths,
interrupted-worker recovery and real class-size capacity remain separate gates.
No production service is upgraded by the rehearsal.

The relay binds only the fixture Docker bridge on Linux or loopback on macOS;
Forgejo's test allowlist is restricted to that bridge/host endpoint. It forwards
the exact signed body/headers and records delivery status in private output.
This is not a public relay service. Fixture setup follows the official
[Forgejo webhook](https://forgejo.org/docs/v16.0/user/repository/webhooks/) and
[webhook allowlist](https://forgejo.org/docs/v16.0/admin/config-cheat-sheet/#webhook-webhook)
documentation.

Assessment proposals here are deterministic imports, with no LLM calls. Instructor
review and score publication are exercised through Cairn's current dashboard.
This verifies integration and permissions; it does not establish feedback quality
or instructor calibration. Simulated LTI delivery is not Brightspace acceptance.

Fixture configuration follows the primary [Keycloak container](https://www.keycloak.org/server/containers)
and [reverse-proxy](https://www.keycloak.org/server/reverseproxy) guidance and
[Forgejo Docker installation](https://forgejo.org/docs/v16.0/admin/installation/docker/).
Images use explicit versions and manifest digests; fixture upgrades need a fresh
accepted run rather than assuming compatibility.

## Resource amendment rehearsal

The independent [resource-change harness](../RESOURCE-CHANGES.md) runs two
managed synthetic courses and checks actual learner limits, normal Hub recreation,
interrupted contraction and resume, stale approval refusal, unchanged peer work
and closed encrypted recovery. Its CI job uploads only bounded
`RESOURCE-AMENDMENT-PASS.json` after all required controls and cleanup. Private
plans, receipts, environments, worker authority and raw logs are excluded.

## Reviewed replacement rehearsal

The independent [cutover harness](../COURSE-CUTOVER.md) keeps an authenticated peer
live while a stopped course restores into a closed namespace and transfers its
identity through a reviewed journaled decision. It covers resume, pre-use rollback,
repeated approvals, real receiver work and refusal of rollback after divergence.
Its bridge-only relay is disposable fixture routing, not a hosted service. Only
bounded `CUTOVER-PASS.json` is uploaded after all controls and verified cleanup;
private plans, full receipts, environments, authority and plaintext backups stay
excluded. Older worker controls refuse the new lineage records.

## Logical-worker ownership contract rehearsal

The independent [ownership contract](../CROSS-WORKER-CONTRACT.md) uses two logical
workers, separate private fixture ledgers and owned supervised service processes
on one machine. It checks persistent course-specific fencing and ownership CAS,
including interruption, source-client restart refusal and unchanged peer work.
This is a standalone protocol prototype: real Waypoint enrollment, existing Hub
runtime enforcement, authenticated remote transport, provider integration and
actual two-host isolation remain separate acceptance work. Synthetic backup fields
exercise the provenance contract; they do not prove encrypted/off-host recovery.
Only bounded `OWNERSHIP-CONTRACT-PASS.json` may be uploaded after every required
control and exact-owned cleanup; private authority, plans, ledgers and raw logs
are excluded.

## Pending-transfer reconciliation rehearsal

The independent [pending-recovery harness](../PENDING-RECONCILIATION.md) extends
the local protocol with fresh reviewed ownership decisions for interrupted
transfers. Both divergent coursework copies and the original transfer history
remain retained; both endpoint revival barriers stay in effect. The fixture uses
owned synthetic processes on one machine and preserves peer-course continuity.
Only bounded `PENDING-RECONCILIATION-PASS.json` may be uploaded after every required
control and exact-owned cleanup. Private authority, plans, ledgers and raw logs
are excluded. Existing ordinary ownership controls run separately; hosted
runtime/provider enforcement and actual two-host acceptance remain open.

## Independent worker control rehearsal

The [independent worker adapter](../INDEPENDENT-WORKERS.md) runs two worker-server
processes with real mutual TLS, disjoint private state and supervised synthetic
services. It checks durable quarantine, interrupted transfer, exact resume,
divergent-copy reconciliation and saved-file recovery. A listener reconnect
preserves the running peer; an agent restart is tested after explicitly stopping
that peer. The bounded `REMOTE-WORKERS-PASS.json` requires clean unchanged source
and verified owned cleanup. This does not exercise Docker, Python/R notebooks,
encrypted backup transfer, production activation or actual host isolation.
