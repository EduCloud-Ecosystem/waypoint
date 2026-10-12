# Stopped-course resource changes

Change learner RAM, CPU and the active learner cap on an enrolled institutional
course without releasing its identity or storage reservation. The workflow never
starts services. Roster/quota maintenance, image upgrades and identity transfer
remain separate operations. Review actual worker capacity before changing limits;
passing admission arithmetic is not a class performance measurement.

## Preview, review, apply

Use the current tools as root on the native Linux worker. Keep the course,
approval directory and plan private. A plan contains the full environment,
including credentials; do not commit, email or upload it.

```sh
sudo python3 deployment.py stop --directory /srv/educloud/course-one
sudo python3 resource_amendment.py preview \
  --directory /srv/educloud/course-one --plan /secure/limits-plan.json \
  --memory-mb 768 --cpu 0.375 --active-limit 2
sudo python3 resource_amendment.py apply \
  --directory /srv/educloud/course-one --plan /secure/limits-plan.json
sudo python3 deployment.py preflight --directory /srv/educloud/course-one
sudo python3 deployment.py start --directory /srv/educloud/course-one
```

Preview requires a new plan file, all course and foreign volume writers stopped,
valid current enrollment, real retained-home quotas and compatible local images.
The only operator resource changes are `WORKSPACE_MEMORY_MB`,
`WORKSPACE_CPU_LIMIT` and `WORKSPACE_ACTIVE_LIMIT`. The generated internal
`WORKSPACE_RESOURCE_REVISION` marks each decision and survives later environment
rewrites. Do not edit it or remove completed history. Plans bind the exact old
and new environment, enrollment, worker ledger/policy/daemon, image IDs, home
policy, setup context, tool version and prior completed decision.

The bounded result reports old, new and transition reservations and the plan
hash. Apply rejects changed approvals and stale state. Keep the stopped course
and produce a fresh preview after resolving a stale binding; do not hand-edit a
plan or reservation. Ordinary Compose startup consumes the changed environment
and recreates the Hub when needed. Existing home bytes and retained users remain.

## Conservative transitions and interruption

During apply, the reservation uses the componentwise maximum of old and new
active count, per-learner RAM and per-learner CPU, plus the unchanged Hub reserve.
For example, moving from four 512 MiB learners to two 1024 MiB learners has equal
learner RAM totals, but temporarily reserves four 1024 MiB learners. This extra
headroom protects every intermediate configuration. A target that fits may still
be refused when its transition does not fit.

The course lock and shared worker lock serialize changes. Apply durably writes
an interruption journal before publishing the transition claim, then writes the
new environment and enrollment, verifies them, publishes the final reservation,
writes the completed receipt and removes the journal. Other courses keep their
claims. Interrupted changes block target startup, ordinary maintenance, upgrade,
release, retirement and new backups. Stop/status and peer-course operations remain
available. Never remove `.resource-amendment-pending.json` manually.

```sh
sudo python3 resource_amendment.py resume \
  --directory /srv/educloud/course-one --plan /secure/limits-plan.json
```

Resume uses the exact approved plan and verifies durable progress. Before the
transition claim is published, the old reservation remains; a peer may consume
headroom and make resume refuse. After publication, the conservative claim
remains until the new state is verified. Exact completed retries are idempotent
only while their durable target remains current. A fresh decision revision and
prior receipt prevent replay after limits cycle back. Missing or corrupt latest
history fails closed; investigate recovery instead of resetting the state.

## Encrypted recovery and evidence

Current backups validate completed history before contacting Restic or exporting
images, include the current runtime and retain the receipt inside encryption.
Fresh restoration keeps it as private `source-resource-amendment-receipt.json`
only. Source approvals, pending journals and the active decision revision are not
installed in the replacement namespace. Managed recovery still requires current
worker enrollment and does not bypass identity tombstones. This workflow does
not implement live replacement cutover.

Use current entry points. Historical scripts and direct root/Docker changes cannot
enforce newer guards. A revision marker is not protection against an administrator
rewriting all authoritative state outside these tools.

The disposable native acceptance harness is
`rehearsal/verify_resource_amendment.py --output /tmp/NEW_PRIVATE_DIRECTORY`.
It exercises real OIDC, XFS quotas, Python/R, normal Hub recreation, learner RAM/CPU,
active-cap refusals, interruption/resume, stale approvals, peer continuity and
closed encrypted restore. Only bounded `RESOURCE-AMENDMENT-PASS.json` is published
after all controls and owned-resource cleanup. It establishes synthetic component
behavior; hosted identity, off-host recovery and measured class capacity remain
separate acceptance gates.
