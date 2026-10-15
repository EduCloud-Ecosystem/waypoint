# Prepare the independent host pilot

Start with a dedicated, SSH-only Droplet running the existing worker-channel
Python/R rehearsal. Then move to separate worker hosts and a separate controller
for recovery acceptance. The first stage exercises the cloud host, but still
uses one Docker daemon and local recovery staging. It cannot accept host loss,
network backup transfer or classroom capacity.

## Account and spending inputs

Before creating anything, record the DigitalOcean team/project, region, operator,
SSH public key reference, allowed operator IPv4/IPv6 CIDRs, UTC expiry, spending
ceiling and teardown owner. Keep API tokens and private keys out of chat, source,
cloud-init and reports. A locally authenticated provider CLI or manual dashboard
creation is sufficient; this pilot does not implement an Outfitter provider adapter.

Proposed first host: a new Ubuntu 24.04 x86-64 **Basic Regular 4 vCPU / 8 GiB /
160 GiB** Droplet. The full notebook rehearsal requires at least four CPUs and
6656 MiB Docker memory. The older 2 vCPU / 4 GiB trial supports a different,
single-learner test and is too small for this rehearsal.

DigitalOcean lists $0.07143/hour and a $48 monthly cap for this plan, checked
October 15, 2026. A 72-hour compute window is approximately **$5.15**, excluding
taxes, optional resources and excess transfer. Propose a **$10 total ceiling**
and a named operator responsible for ending the trial. These are planning limits,
not an automated provider cap. Recheck the selected region and checkout price.
[Published Droplet pricing](https://www.digitalocean.com/pricing/droplets).

No GPU, domain, public web port, extra disk, managed database or paid backup is
needed for this first disposable synthetic run. Preserve reports off-host before
teardown. Actual coursework recovery in the next stage needs an independently
accessible encrypted backup destination and key custodian. Do not reuse a Cairn
or database host; an existing dedicated test host requires inventory review first.

## Render the dedicated host setup

Use a clean reviewed commit containing these preparation tools. Replace the
example key path and documentation-only CIDR with actual operator values.

```sh
python3 course-workspace/worker.py render \
  --mode worker-channel-test \
  --public-key /absolute/path/to/operator.pub \
  --ssh-cidr 192.0.2.5/32 \
  --release REVIEWED_FULL_COMMIT_SHA \
  --directory /private/new-pilot-setup
```

This writes local cloud-init only. Review it before supplying it at Droplet
creation. It pins the checkout, installs Docker/Compose, Python/venv, OpenSSL,
Restic and XFS tools, and configures the `educloud` SSH/sudo account. It creates
no course, authority, managed certificates or legacy backup/health timers.
Those old timers target the single-course Compose layout and must not be enabled
against this worker-channel fixture.

Attach a DigitalOcean Cloud Firewall allowing inbound TCP 22 only from the
operator CIDRs, with no public notebook, authority, worker or Docker API ports.
Check both IPv4 and IPv6. Host UFW alone is insufficient protection for Docker
published ports. Confirm the SSH host-key fingerprint through the provider console
before accepting it locally. Configure a local SSH alias; private keys stay local.
Use a public key registered with the team or the reviewed cloud-init account.
[DigitalOcean SSH setup](https://docs.digitalocean.com/products/droplets/how-to/add-ssh-keys/).

The bootstrap needs outbound package/repository/image downloads. Confirm
`cloud-init status --wait` completes successfully, the pinned checkout exists,
the intended cloud firewall is attached, and no unexpected public ports are
reachable. The local preflight below does not verify provider networking or bills.

## Run the first hosted smoke test

On the dedicated host, use the administrator shell. Do not grant notebook
containers or the controller a Docker socket. The privileged fixture runner
performs bounded setup and cleanup; its deployed controller and Hubs remain
socketless.

```sh
sudo -i
cd /opt/educloud/waypoint
python3 -m venv course-workspace/.venv
course-workspace/.venv/bin/pip install -r course-workspace/requirements-test.txt
course-workspace/.venv/bin/python course-workspace/pilot_host.py \
  --release REVIEWED_FULL_COMMIT_SHA --directory /srv/educloud/pilot \
  > /srv/educloud/pilot/host-preflight.json
```

Continue only on zero exit and `READY_FOR_SMOKE`. This read-only check requires
native Linux Docker, Ubuntu 24.04, x86-64, Python 3.12+, clean pinned source,
the rehearsal CPU/RAM floor, 20 GiB free in both Docker and staging filesystems,
private staging, and no existing containers or volumes. A refusal never cleans
resources. Inspect existing resources privately and select a clean dedicated
host; do not prune a shared daemon. It measures configured capacity and free
disk, not available memory under competing processes or sustained CPU performance.

```sh
docker build -t waypoint-authority-hub:local course-workspace/hub
docker build -t waypoint-authority-course:local course-workspace/course
docker build --build-arg HUB_IMAGE=waypoint-authority-hub:local \
  -t waypoint-authority-controller:local \
  -f course-workspace/controller.Dockerfile course-workspace
course-workspace/.venv/bin/python course-workspace/rehearsal/verify_remote_notebooks.py \
  --output /srv/educloud/pilot/acceptance-001
```

Keep the SSH session alive or use an operator-managed persistent terminal; never
start a second fixture while an earlier one may still own resources. Each attempt
uses a new output directory. Retain failures and sanitized diagnostics alongside
later results. Review `REMOTE-NOTEBOOKS.json`: require PASS, all 17 phases,
unchanged clean source, expected image hashes and verified cleanup. Preserve its
scope and false independent-host/backup/production flags. Copy only bounded
reports to operator storage; private fixture directories contain credentials.

Record the provider resource ID, region, size, source commit, UTC run window,
preflight and acceptance reports in a private host inventory. Record actual
charges separately from estimates. A successful run accepts only this synthetic
host/profile. It does not leave a durable student notebook deployment running.

## Build and accept the separate host recovery stage

Use worker A and worker B on distinct hosts/daemons, with Hubs colocated with
their respective workers so returned notebook addresses remain reachable. Put
the ownership authority and enrollment state on a third failure domain; the
operator Mac can serve as a temporary controller only with explicit private
routing and an awake, supervised test window. For unattended testing, use a
separate controller host. Do not duplicate the authority database between workers.

Before this stage, build and test these remaining deployment pieces:

1. A role-specific installer and inventory binding actual host identity, storage,
   worker configuration and mutually authenticated private endpoints. Supply exact
   routes, certificate names/fingerprints and firewall peers. Keep controller state,
   worker journals and homes on their assigned hosts; enroll Hubs and Guardians
   through reviewed managed credential operations.
2. Encrypted off-host transfer into an empty, stopped receiver, preserving the
   exact metadata and content required by the ownership proof. The current
   rehearsal's local copy helper is not this transport. Do not attach the legacy
   single-course recovery/timer code to the new layout without integration tests.
3. A separate-host acceptance runner and evidence schema recording distinct
   kernels/daemons, real network operations and reviewed restoration. Preserve
   source and receiver copies, quarantine, reservations and ownership history
   on every interrupted or uncertain operation.
4. A reviewed host-incarnation replacement workflow. Current worker identity
   binds the Linux boot ID: a real host reboot must remain closed when that
   binding changes. Agent/container restart acceptance does not establish host
   reboot recovery. Never edit identity records or recreate journals to bypass it.

Then exercise saved Python/R files, channel and controller outage, one-worker
failure with peer continuity, rotation/revocation, encrypted restore, interrupted
transfer, stale-source refusal and explicitly reviewed recovery. Loss of contact
or certificate revocation is not proof the old host stopped. Until independent
fencing has been implemented and verified, an unreachable source remains pending;
there is no automatic promotion of its receiver.

The next-stage host sizes, storage, spend, lifetime and RPO/RTO must be selected
against its actual per-host resource reservations. Do not multiply or redistribute
the one-daemon fixture caps and call that a validated production capacity plan.

## Evidence and teardown record

| Record | Required value |
| --- | --- |
| Pilot stage | Single-host synthetic smoke / separate-host recovery |
| Team, project, exact resource IDs, region | Private operator inventory |
| Budget, start, expiry and teardown owner | Approved before provisioning |
| Source and images | Full commit and recorded immutable image hashes |
| Cloud firewall and verified SSH identity | Operator observation |
| Acceptance and cleanup | Exact report paths; retain failed attempts |
| Retained work and evidence | Verified off-host location and custodian |
| Provider teardown | Exact resource inventory and final charge review |

At the approved end of the run, verify evidence export and owned fixture cleanup.
Have the teardown owner review the exact Droplet ID and any separately billable
resources before removal. No cleanup command in this preparation deletes a host,
volume or backup. Powering off a Droplet does not stop billing; remove approved
resources through the provider and verify the resulting inventory.
[DigitalOcean billing](https://docs.digitalocean.com/products/droplets/details/pricing/).

Allow one setup/build session and one acceptance/triage session inside the proposed
72-hour window. The separate-host stage follows its deployment and recovery
implementation; it is not promised by the first Droplet run.
