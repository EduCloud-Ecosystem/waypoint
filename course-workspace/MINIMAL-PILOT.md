# Smallest useful Python/R trial

Run one synthetic learner at a time in JupyterLab, with Python and R, saved files,
1 GiB learner RAM, one CPU, and automatic idle shutdown after 15 minutes. The Hub
has its separate 1 GiB limit. There is no cloud purchase, domain, identity-server
setup or public notebook endpoint. This trial is for the operator testing course
exercises, not a class or real learner records. The alice/bob test accounts share
a generated password; only one can have a running server at a time.

## Start locally

Requires Python 3.12+, Docker Engine with Compose v2, at least 3 GiB assigned to
Docker, two CPUs, and approximately 10 GiB spare disk for builds and test files.
The RAM check measures total Docker capacity, not memory available after other
applications. Stop unrelated workloads yourself if the host is under pressure.

```sh
cd course-workspace
python3 pilot.py init
python3 pilot.py start
```

Open http://127.0.0.1:18000. Sign in as `alice`; the generated password is the
`WORKSPACE_TEST_PASSWORD` value in `output/minimal-pilot/pilot.env`. Keep this
private file out of chat, tickets and version control. Configuration is mode 0600.
Initialization refuses to overwrite an existing trial, keeping its password and
volume names stable. Use `--directory PATH --port PORT` for a separate trial.

Open `Welcome.ipynb` or create a Python/R notebook. Save, stop, restart, and
reopen it. Files persist; kernel memory does not. Packages are preinstalled and
notebook Internet access is disabled. Upload/download synthetic materials in
JupyterLab. Stop alice's server from **File → Hub Control Panel → Stop My Server**
before trying bob. Closing the browser alone does not immediately stop it.

```sh
python3 pilot.py status
python3 pilot.py stop
python3 pilot.py start --no-build
```

These commands never remove home volumes. Use the full [acceptance test](README.md)
to test two simultaneous users and isolation separately.

## Checkpoint and recover

Save your notebooks first. A checkpoint interrupts running kernels; unsaved
browser edits and in-memory variables are not captured.

```sh
python3 pilot.py checkpoint
# Or name an archive under an existing backup directory:
python3 pilot.py checkpoint --destination /secure/backups/trial-001
```

The command stops the Hub and its learner server, creates checksummed private
archives, records runtime image IDs/architecture, then resumes the same Hub.
A failed backup also attempts to resume it; a previously stopped Hub remains
stopped. Concurrent pilot commands are refused. Direct Docker commands are not
covered by this lock: do not run them during maintenance. An orphaned live writer
causes backup refusal rather than an inconsistent copy.

This checkpoint command's archives remain local and unencrypted. Use the
[encrypted recovery workflow](RECOVERY.md) to package images, configuration and
checkpoints into Restic. Alternatively, copy synthetic archives securely to
another machine and preserve the tested images
using `docker image save` or an immutable registry digest. See README.md for
restore commands. The checkpoint command itself has no retention or cloud upload;
the separate encrypted workflow provides scoped retention and scheduled transfer.

To exercise the small trial, including checkpoint, automatic resume, and recovery
into fresh volumes (uses unique disposable instances):

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/python verify_pilot.py
```

The verifier checks Python/R kernels, saved files, runtime limits, Internet
denial, admission refusal for a second learner, restart and recovered files.
It cleans only its generated containers/networks/volumes. Evidence and synthetic
archives remain in ignored `output/verify-small-*/`. Fresh-volume recovery on one
host is not proof of transfer to a second physical host.

## Optional small Droplet

The proposed remote test target is a **separate Ubuntu 24.04 Droplet with 4 GiB
RAM, two shared vCPUs and 80 GiB disk**. DigitalOcean's Basic Regular bundled plan
is $24/month before backups/taxes, checked October 7, 2026 against its
[published pricing](https://www.digitalocean.com/pricing/droplets). No Droplet is
created by these scripts, and this sizing still needs a target-host rehearsal.
Keep the existing Cairn/database host separate. No GPU, Spaces or managed
database is needed for this synthetic trial.

Install Docker with Compose and Python 3.12+, clone this repository on the
worker, and run the same commands there. Allow SSH only from the operator's
address/VPN; do not open port 18000. From your computer:

```sh
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:18000:127.0.0.1:18000 USER@WORKER
```

Then open the same localhost URL. Keep the tunnel running while testing. If
18000 is occupied, choose another port at initialization and use that port on
both sides of the tunnel. Restrict access to the host: local users can reach
loopback too. Public classroom use still requires the production identity,
HTTPS, filesystem quotas, off-host recovery and capacity gates in README.md.
