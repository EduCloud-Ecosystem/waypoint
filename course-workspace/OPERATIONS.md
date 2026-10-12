# Course worker operations

This release adds an operator workflow around the existing dedicated Linux
worker, quota homes and encrypted recovery tools. It does not purchase a server,
open public ports, register an identity client, or send external notifications.
Run host commands on the dedicated worker, not against a remote Docker daemon
from a laptop. Keep deployment/configuration directories private (0700) and
credential/configuration files owner-only (0600).

## Prepare and start a course

For multiple courses, review the shared [allocation policy](MULTIPLE-COURSES.md)
and enroll each stopped course before startup. Claims reserve maximum learner/Hub
RAM and CPU until explicitly released. Once managed, the worker refuses unenrolled
startup. Native Linux starts require worker administrator/root for the shared lock;
Desktop trials remain a separate single-course test path.
Use [resource changes](RESOURCE-CHANGES.md) for RAM/CPU or active learner caps;
stop, preview a private plan, apply it and explicitly restart after verification.
An interrupted resource change blocks other target lifecycle operations until resumed.

1. Render the worker with an immutable reviewed Waypoint release:

   ```sh
   python3 worker.py render --mode institutional \
     --public-key /secure/operator.pub --ssh-cidr 192.0.2.10/32 \
     --release FULL_40_CHARACTER_COMMIT_SHA --directory output/worker-setup
   ```

   Supply the generated cloud-init when creating the dedicated worker. The
   institutional mode prepares an empty deployment directory; it does not create
   `pilot.env` or synthetic users. Only operator SSH is allowed by the generated
   firewall policy. Timers are installed but not enabled.

2. Supply an empty dedicated XFS mount with project quotas and the approved
   stable-subject roster. Follow [HOSTED-PILOT.md](HOSTED-PILOT.md) to provision
   homes and render the institutional environment, nginx and identity-client
   configuration. Put the deployment at `/srv/educloud/pilot` when using the
   generated systemd units. Set its private client secret, enable the quota
   inspector timer, and build or restore the tested Hub and course images.
   Preserve the image IDs; rebuilding a tag later is not a reproducible release.

   The [course manifest](COURSE-MANIFEST.md) combines these settings and owners
   into one reviewable input and generates a fresh private deployment directory.
   It validates consistency offline; actual host/identity/backup acceptance
   remains required. If using a different directory, update service paths.

3. Run startup preflight before admitting anyone:

   ```sh
   sudo python3 deployment.py preflight --directory /srv/educloud/pilot
   sudo python3 deployment.py start --directory /srv/educloud/pilot
   ```

   Preflight requires compatible local Linux images, at least two Docker CPUs,
   and memory for `active limit × learner memory + 2 GiB` (Hub and host reserve).
   It verifies hard quotas and a provisioned home for every approved learner,
   including consistent policy for retained homes. Lower the configured active
   limit if the worker cannot accommodate it. This is an admission floor, not
   a measured class-capacity guarantee. Startup never pulls or rebuilds images.

4. Complete the [testing worksheet](rehearsal/TESTING-CHECKLIST.md) for actual
   identity login/revocation, TLS/user subdomains, kernel WebSockets and target
   capacity. Configure the actual encrypted off-host repository using
   [RECOVERY.md](RECOVERY.md), take a backup and verify replacement-host recovery.
   Only after acceptance route learners to the service and enable its timers.

For an upgrade, retain a verified backup and prior immutable images, stop the
course, install the reviewed release, run preflight, start and verify the course.
A failed data migration is recovered from the backup in a fresh destination;
do not assume that reinstalling an older binary reverses a schema change.

Use the [reviewed image upgrade workflow](COURSE-UPGRADES.md) for a Hub/course
image change. It requires a full candidate canary, takes a fresh stopped-course
encrypted backup, validates a frozen plan and journals the pin update. Startup
is explicit; an interrupted upgrade blocks start/backup/roster maintenance.

For course closure, use [reviewed retirement and export](COURSE-RETIREMENT.md).
Stop writers, prepare a protected encrypted snapshot, review the frozen plan and
seal the worker. Startup and policy changes remain blocked after retirement and
protected restoration. Verified exports include every retained home's `work`
folder; other files remain retained. The recorded owner/date requires a retention
decision, never automatic deletion. Review backup/monitoring schedules and hide
the Cairn workspace binding as part of the institution's closure procedure.

## Worker health and local alerts

`health.py` collects bounded read-only checks for worker storage/inodes, Hub
readiness, backup freshness/outcome, quota status and verified TLS expiry. Its
private JSON report contains check codes and measurements, not learner content,
credentials or raw backend diagnostics. A failed/unavailable check never becomes
healthy because a probe could not complete. Native Docker storage must be
verified on the actual worker; Docker Desktop or a remote daemon cannot establish
worker disk capacity from the operator's filesystem.

Configure `/etc/educloud/health.json` using [health.example.json](health.example.json). Adjust the instance, paths,
origin and thresholds to the actual worker; the six-GiB free-space default is
not a storage-sizing calculation. The backup evidence is local status from a
successful Restic backup/check, not proof that the remote repository is reachable
right now. Run periodic repository and replacement-host drills separately. Run a
manual check first, then install the units generated by `worker.py` and enable
`educloud-worker-health.timer`. Read the private report at
`/srv/educloud/health.json` and the service exit state. A failed service/local
report is a local alert only. An institution must connect its monitoring system
and test its notification destination before claiming that anyone will be paged.
No email, chat, webhook or paging destination is configured by these tools.

```sh
sudo install -m 600 health.example.json /etc/educloud/health.json
# Edit /etc/educloud/health.json privately for this worker, then:
sudo python3 health.py --config /etc/educloud/health.json --output /srv/educloud/health.json
sudo systemctl enable --now educloud-worker-health.timer
```

Exit 0 means all configured checks passed, 1 means a failed/unavailable check,
and 2 means configuration/output failure. Alert on a stale report or nonzero
exit as well as critical check values. A valid certificate on the main origin
is not proof that all learner subdomains are routed correctly; retain the
browser/WebSocket acceptance checks.

## Roster and quota maintenance

Maintenance is institutional-only and intentionally requires a stopped course.
Stop the Hub and check that no learner or unrelated container still references
the course homes. Create and inspect a private maintenance plan before applying
it. The plan binds the current environment, storage inventory and requested
change; a stale plan is rejected. Resume the same plan after an interrupted
apply. Before adding a new roster subject, provision its home with the existing
`homes.py provision` command while the course is stopped; inspect the resulting
inventory. The maintenance command intentionally does not provision or delete
homes implicitly.

```sh
sudo python3 deployment.py stop --directory /srv/educloud/pilot
sudo python3 maintenance.py preview --directory /srv/educloud/pilot \
  --subjects-file /etc/educloud/subjects.txt --quota-mb 6144 --inode-limit 120000 \
  --plan /srv/educloud/maintenance-plan.json
# Inspect the private plan and retained-home list before applying it.
sudo python3 maintenance.py apply --directory /srv/educloud/pilot \
  --plan /srv/educloud/maintenance-plan.json
sudo python3 recovery.py backup --directory /srv/educloud/pilot --config /etc/educloud/recovery.json
sudo python3 deployment.py preflight --directory /srv/educloud/pilot
sudo python3 deployment.py start --directory /srv/educloud/pilot
```

The initial release permits a nonempty approved roster; to retire a course or
revoke everyone, leave the Hub stopped. Do not substitute a fabricated learner
identity. Pending markers block startup and backup rather than serving a partially
changed policy.

Removed learners' homes remain retained and backed up. A roster change does not
rename or transfer identities, migrate issuers, delete student work or perform
institutional account deletion. Existing volumes are never adopted merely
because their names match. Home-limit changes apply consistently to all retained
homes in the course and refuse limits below actual usage. Data erasure and
identity migration require a separate reviewed procedure.

## Completion boundary

Local and disposable-runner results establish tested tooling. Real worker
capacity, institutional identity, notification delivery, off-host credentials,
replacement-host recovery and course support ownership still require acceptance
in their actual environment. Belay development and tutoring integration are
outside this release.
