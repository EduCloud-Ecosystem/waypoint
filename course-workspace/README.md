# Portable browser Python/R coursework

This is a tested local continuity pilot for one course, built on JupyterHub and
DockerSpawner. Cairn remains the classroom/assignment application. Learner files
live in persistent home volumes; replacing the notebook container does not
remove them. Use Python notebooks or R notebooks in JupyterLab (not RStudio).

For the smallest manual test, start with the [one-learner trial](MINIMAL-PILOT.md).
It runs locally without a cloud account and can also use a private SSH tunnel to
a small dedicated worker. It retains files between sessions.
Continue with [encrypted recovery and dedicated worker setup](RECOVERY.md) for
preserved images, Restic transfer, replacement-worker restore and maintenance timers.
[Hosted pilot preparation](HOSTED-PILOT.md) covers hard home quotas, monitoring and
reviewable nginx/Keycloak configuration. The [synthetic course rehearsal](rehearsal/README.md)
connects real Keycloak, notebook, Forgejo and Cairn services through the learner journey.

## Reproduce the acceptance test

Requires Python 3.12+, Docker Engine with Compose v2, and enough disk/RAM to build
the course image and run two 2 GiB learner containers plus the 1 GiB Hub.
The first build downloads dependencies. No cloud or Education account is needed.

```sh
cd course-workspace
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python verify.py
```

The verifier creates unique loopback-only deployments with synthetic alice/bob
accounts and a generated password saved only in an ignored, mode-0600 env file.
It runs Python and R through notebook WebSockets, checks resource/network policy
and learner separation, recreates servers, rejects a live backup, then stops the
Hub and restores the archive into fresh volume names. It refuses an overwrite
and verifies the recovered files/kernels. Finally it removes **only its own**
containers, networks and volumes. Synthetic archives and PASS.txt remain under
ignored `output/verify-*/`; image build cache remains in Docker.

A fresh namespace on the same Docker host is the automated recovery rehearsal.
It does not establish second-host compatibility or production availability.
See [VALIDATION.md](VALIDATION.md) for observed evidence and unfinished gates.

## Configure an institutional course

Do not put actual learner data in the local-test deployment. Complete the live
acceptance gates below before changing a class's Cairn workspace link.

1. Assign a maintained, funded Docker worker host, an operator and a backup owner.
   Keep the socket-bearing Hub separate from identity databases, Cairn's database
   and unrelated sensitive services. The Hub controls Docker as root; a learner
   container does not receive the socket. Containers share the host kernel.
2. Copy `.env.example` to `.env` and restrict it (`chmod 600 .env`). Set a stable
   course instance name. Changing it creates a distinct deployment and volumes.
3. Use a dedicated HTTPS origin plus wildcard DNS/TLS for user subdomains.
   A host reverse proxy must route the origin and its subdomains to the loopback
   port, preserve Host/forwarded HTTPS headers, and support WebSockets. Do not
   share the learner-content domain with other authenticated applications.
4. Create a confidential Keycloak client with callback
   `https://work.example.edu/hub/oauth_callback`, PKCE S256 and the realm issuer.
   Set the client secret and approved `WORKSPACE_ALLOWED_SUBJECTS` (comma-separated
   stable lowercase Keycloak subject UUIDs, not emails or Cairn pseudonyms).
   Empty credentials/roster or HTTP production origins fail startup.
5. Size `WORKSPACE_ACTIVE_LIMIT` against actual RAM/CPU capacity. Defaults are
   four active learners, two concurrent starts, 1 CPU/2 GiB/128 processes each,
   30-minute idle expiry and eight-hour maximum session age. At capacity the Hub
   rejects additional starts; this is bounded admission, not a queue/autoscaler.
6. Provision the hard quota homes and enable their health timer as described in
   [hosted pilot preparation](HOSTED-PILOT.md). Match the byte/inode limits in
   `.env`. Institutional mode refuses startup without quota configuration.
   Build and start:

   ```sh
   docker compose -f compose.yaml -f quota-compose.yaml --profile build build
   docker compose -f compose.yaml -f quota-compose.yaml up -d hub
   ```

7. Test institutional login, unauthorized/removed roster members, per-user
   subdomain HTTPS and WebSockets, concurrency, disk quotas and recovery. Only
   then set Cairn's `CAIRN_WORKSPACE_URLS` to this course origin.

No student chooses images or limits. Package installation is an image-maintainer
operation: edit the course Dockerfile/requirements, build, validate, then roll
out. Python dependencies have version/hash locks; the base image is digest-pinned.
Debian and npm transitive dependencies are not fully frozen by the recipe.
**Preserve the tested built Hub and course images** by immutable registry digest
or `docker image save`; retain their architecture and image IDs with the backup.
A fresh build months later is not guaranteed to reproduce the same OS packages.

## Files, submission and access

Every learner gets their own entire `/home/learner` volume and internal network.
Learners cannot directly reach one another or the Internet. Course dependencies
must be preinstalled; use notebook upload/download for materials and results.
Direct Git operations and external data APIs require a later reviewed egress
policy. Saving a notebook does not submit it to Cairn: commit/upload deliverables
to the assigned repository through the normal submission workflow.

The Cairn link provides navigation only. Hub login/roster are independent. The
Hub checks the current configured roster again before each spawn. To revoke an
active learner, remove their allowed subject, restart the Hub (which stops
servers), invalidate their Hub/identity sessions, then test access is denied.
Changing an allowlist alone does not terminate an already-running server.
Retain or delete their home only under the institution's retention policy.

## Back up and move to another host

Volumes survive restarts but are not backups. Schedule a maintenance window and
stop the Hub gracefully; its shutdown stops learner containers. If a crash left
an orphan, identify it with `docker ps --filter label=educloud.workspace.instance=COURSE`
and stop that specific container. The backup command refuses any running
container that mounts a course volume, including an unlabelled writer.

```sh
docker compose stop hub
python3 backup.py backup --instance python-r-pilot --directory /secure/course-backup
# Copy/encrypt the archive off host and preserve the tested images separately.
# On the target host, load the matching images before restoring:
python3 backup.py restore --instance replacement-course --directory /secure/course-backup
```

Run recovery during an operator-controlled maintenance window with no concurrent
spawns or volume changes. Archive creation is an offline operation, not a
transactional live snapshot. The manifest includes checksums; restore validates
all archives before creating new volumes and refuses to overwrite existing ones.
A disposable, network-disabled helper reads/writes only the selected volume and
archive directory; extraction uses Python's data filter. The helper has CHOWN
and DAC_OVERRIDE for private home-directory modes. Learner containers have no
added capabilities. Archives include credentials in the Hub database and private
learner files: encrypt in transit/at rest, restrict access, and set retention.
Checksums detect corruption; authenticate the archive's source separately.

On the target, set the new instance name, **the same identity issuer and subject
mapping**, matching image IDs, and appropriate URL/callback/DNS. Start the Hub
and verify retained files and kernels before directing students there. A changed
issuer/subject mapping requires a reviewed identity/data migration. Restore may
show Compose's expected warning that the precreated Hub volume was not created
by Compose; it adopts that named volume. Never use `docker compose down -v` on a
real course. Returning to the source requires a deliberate data reconciliation;
this design does not implement automatic failover or active/active writes.

## Required before real learner use

- Institution-approved host, data destination, support owner and recurring budget.
- Live OIDC allowlist/revocation, wildcard HTTPS/subdomains and browser tests.
- Target-worker quota/mount verification, host disk/inode monitoring and retention/deletion.
- Target-host concurrency/resource exhaustion and shared-kernel threat review.
- Encrypted off-host backups, second-host restore and recorded recovery objectives.
- Course-image vulnerability review, update cadence and immutable-image retention.

The local test does not close Cairn's separate grading-integrity findings.
[JupyterHub security guidance](https://jupyterhub.readthedocs.io/en/stable/explanation/websecurity.html)
explains the per-user-domain requirement and container trust boundary.
