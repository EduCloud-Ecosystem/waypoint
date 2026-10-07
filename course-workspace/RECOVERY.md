# Encrypted recovery and a replacement worker

The one-learner trial saves encrypted recovery packages to an operator-owned
Restic repository over SFTP or HTTPS S3. Packages contain quiesced Hub/home
archives, configuration, deployment scripts and the actual Hub/course images.
Recovery does not rebuild against changing upstream packages. Use the same CPU
architecture on the replacement. This version supports the private synthetic
trial; institution-authenticated course migration is a separate acceptance gate.

## Configure storage

Install Restic and Python 3.12+ on the Docker worker. Put the repository on a
different machine/account failure domain. For SFTP, configure a dedicated SSH
alias with a verified host key and key-based access. Unknown keys and interactive
prompts are refused. For S3, use HTTPS and scoped credentials in standard AWS
environment variables. Never embed credentials in repository URLs.

```sh
sudo install -d -m 700 /etc/educloud
sudo python3 recovery.py keygen --destination /etc/educloud/restic-password
```

Preserve an independent secure copy of this password; it is intentionally absent
from its own encrypted backup. Create `/etc/educloud/recovery.json`, mode 0600:

```json
{
  "version": 1,
  "repository": "sftp:educloud-backups:/srv/backups/course-trial",
  "password_file": "/etc/educloud/restic-password",
  "keep_last": 7,
  "keep_daily": 7,
  "keep_weekly": 4
}
```

An S3 alternative is `s3:https://REGION.digitaloceanspaces.com/BUCKET/PREFIX`.
These are configuration examples, not provisioned or verified destinations.
Local repositories require explicit `local_test: true` and do not establish
off-host protection. Run commands as the configuration owner with Docker access
(root on the rendered worker):

```sh
python3 recovery.py init --config /etc/educloud/recovery.json
python3 recovery.py backup --config /etc/educloud/recovery.json --directory /srv/educloud/pilot
python3 recovery.py snapshots --config /etc/educloud/recovery.json
python3 recovery.py check --config /etc/educloud/recovery.json
python3 recovery.py status --directory /srv/educloud/pilot
```

Save notebook edits first. Immutable images export before the offline checkpoint;
the Hub resumes before encryption/upload. Unsaved edits and kernel variables are
not captured. Plaintext staging is private and removed on normal success/failure.
A power loss/forced kill can leave a private `.recovery-*` directory; inspect and
remove it only after confirming no backup is running. Direct Docker mutations
must not run during maintenance. Failure status retains the preceding successful
snapshot ID/time. Do not use real learner records in this synthetic trial.

## Recover without the original host

Install Docker/Compose, Python and Restic on a fresh worker. Obtain this recovery
tool and the independently held repository credentials. Choose an exact
64-character snapshot ID. The destination must not already exist:

```sh
python3 recovery.py restore --config /etc/educloud/recovery.json \
  --snapshot FULL_SNAPSHOT_ID --destination /srv/educloud/recovered --port 18000
python3 /srv/educloud/recovered/runtime/pilot.py start --no-build \
  --directory /srv/educloud/recovered
```

Restic verifies restored contents; EduCloud checks inventory, checksums, volume
manifests and architecture. Recovery loads preserved images and creates a fresh
namespace, refusing existing destinations/volumes. The original synthetic login
password is preserved inside the package. No server starts automatically.
The copied `runtime/` contains deployment/backup scripts, so the original source
checkout is unnecessary after recovery. Repository write/key access grants
authority over these executable images/scripts; protect it accordingly.

## Schedule and retention

`worker.py render` supplies daily **02:00 UTC maintenance** and hourly health
systemd units. It does not enable timers. First configure the remote repository,
escrow its key and pass a separate-worker restore, then enable the units:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now educloud-backup.timer educloud-backup-health.timer
systemctl list-timers 'educloud-*'
journalctl -u educloud-backup.service -u educloud-backup-health.service
```

The service uses mode-0077 creation and applies retention only after a successful
verified backup. Retention selects this instance's exact host/tag pair, keeps
the configured recent snapshots and prunes unused data. Other instances are
excluded. Retiring an old instance after migration is an operator decision.

```sh
python3 recovery.py retention --config /etc/educloud/recovery.json --instance trial-INSTANCE
python3 recovery.py retention --config /etc/educloud/recovery.json --instance trial-INSTANCE --apply
```

The first command previews; the second deletes expired snapshots. Status exits
nonzero after failure or when the last success is older than 26 hours. Errors
appear in systemd/journal; no email/paging destination is configured. Assign an
operator to respond. Root-only `/etc/educloud/backend.env` can supply S3
environment credentials to the backup service.

## Prepare a dedicated worker

Render cloud-init for a **fresh Ubuntu 24.04 worker**, keeping Cairn's database
host separate. Supply a public key, trusted operator IP/CIDR, and a reviewed
immutable Waypoint commit:

```sh
python3 worker.py render --public-key ~/.ssh/educloud.pub \
  --ssh-cidr YOUR_OPERATOR_IP/32 --release FULL_GIT_COMMIT \
  --directory output/worker-config
```

This installs tools, fetches that commit, initializes a private trial and permits
only operator SSH through the host firewall. The notebook remains loopback-only.
No Droplet is purchased, images built, class started or backup timer enabled.
Build or restore images and use the SSH tunnel in MINIMAL-PILOT.md. Retain provider
console access in case an operator-IP change needs a firewall update.

```sh
sudo python3 /opt/educloud/waypoint/course-workspace/worker.py doctor --directory /srv/educloud
```

Doctor reports Docker RAM/CPU and staging-disk capacity; it does not measure
Docker-VM disk on macOS or establish class capacity. Allow space for exported
images, home archives and restore staging. Public use still requires hard home
quotas, identity/TLS, lifecycle policy and measured load acceptance.

## Verification

`verify_recovery.py source` exercises synthetic Python/R notebooks, uploads an
encrypted repository, then removes source containers/volumes. The target phase
tests a wrong key, restores the package, starts its bundled deployment and checks
saved files/kernels. `--require-clean` rejects a target with source images cached.
The independent-worker workflow runs these phases in separate Linux runner jobs,
transferring only the test repository and snapshot metadata. Its public fixture
password is solely test data and must never be used for real records. This
rehearsal does not validate the eventual SFTP/S3 account or promise uptime.

References: [repository setup](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html),
[restore](https://restic.readthedocs.io/en/stable/050_restore.html), and
[retention](https://restic.readthedocs.io/en/stable/060_forget.html).
