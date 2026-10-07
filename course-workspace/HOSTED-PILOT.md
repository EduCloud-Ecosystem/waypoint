# Prepare a hosted course without opening it to learners

These tools prepare a dedicated Linux worker. The current release is tested with
synthetic accounts and disposable storage. It does not provision a paid host,
install institutional credentials or enable a public service.

## Hard home quotas

An administrator must supply an **empty, dedicated XFS filesystem mounted with
project quotas (`prjquota`) enabled**. Persist and verify the mount across reboot.
`homes.py` never formats a device and refuses another filesystem, disabled
accounting/enforcement, an unmanaged directory, or mismatched existing volumes.
Do not use the disposable loop-image test as a production storage design.

Create a private roster file with one stable lowercase identity subject per line
(Keycloak UUIDs for an institutional course; `alice` and `bob` for a synthetic
trial). Reserve enough storage for every roster member, even those not active:

```sh
sudo python3 homes.py provision --root /srv/educloud/homes \
  --instance python-r-pilot --subjects-file /etc/educloud/subjects.txt \
  --quota-mb 5120 --inode-limit 100000
sudo python3 homes.py inspect --root /srv/educloud/homes --instance python-r-pilot
python3 homes.py render-timer --root /srv/educloud/homes \
  --instance python-r-pilot --output output/quota-timers
```

Use the same instance, root, byte limit and inode limit in the Hub environment:
`WORKSPACE_HOME_ROOT`, `WORKSPACE_HOME_QUOTA_MB`, `WORKSPACE_HOME_INODE_LIMIT`.
Provision before starting the Hub. Existing volumes are never converted in place;
migrate them through a tested backup/restore into a fresh instance.

Install the rendered units in `/etc/systemd/system`, review their fixed checkout
path, then explicitly enable `educloud-home-quota-python-r-pilot.timer` with
`systemctl enable --now` after `systemctl daemon-reload`. The inspector refreshes
a root-owned status file every minute; the Hub mounts only that status directory,
read-only. Missing, unhealthy, mismatched or more than 120-second-old status
blocks new starts. Existing sessions retain their kernel-enforced limits; a
monitor failure alone does not terminate them. Removing quotas administratively
requires immediate operator intervention for existing sessions.

The status JSON reports used bytes and inodes for each home. Reservations leave
512 MiB of filesystem overhead. This is not a complete worker disk monitor:
Docker images, Hub database, logs and recovery staging need separate capacity
monitoring and an operator response. No paging destination is configured.

When a home fills, the learner must remove files or contact the operator. Changes
to an existing limit are intentionally refused by provisioning. Retention,
account departure and quota increases need a separate maintenance procedure.

## Render HTTPS and institutional identity configuration

```sh
python3 public_course.py --domain work.example.edu \
  --issuer https://auth.example.edu/realms/educloud \
  --instance python-r-pilot --subjects-file /etc/educloud/subjects.txt \
  --home-root /srv/educloud/homes --output output/public-course
```

This creates a private directory with `.env`, `nginx.conf` and
`keycloak-client.json`. The client secret is empty so the Hub refuses startup
until the operator supplies it. Import/review the client in the existing realm,
obtain its secret privately and enter it in `.env`. The client uses authorization
code with PKCE S256, an exact callback and origin, and no direct password grants.
The Hub uses the `sub` claim, not names or email. Keep the roster file and Hub
allowlist synchronized; provisioning storage does not grant authentication.

Use a dedicated origin and wildcard DNS/certificate covering that origin and
its learner subdomains. The nginx file is intended for a dedicated host, replacing
its default site and included inside nginx's `http` context. Install the certificate
and restricted private key at `/etc/educloud/tls/fullchain.pem` and `privkey.pem`.
Review certificate renewal and run `nginx -t` before any service reload. The
configuration redirects HTTP, preserves per-user hosts and HTTPS forwarding,
replaces client-supplied forwarding addresses, supports WebSocket upgrades and
rejects unknown TLS hosts. Access logging is disabled to avoid notebook URLs;
error logs still need restricted access and retention.

The backend remains bound to `127.0.0.1`. Review the generated active limit against
worker RAM; four active 2 GiB learners plus the Hub need more than an 8 GiB host.
After installing the private environment, pass it to Compose explicitly from
`course-workspace` (its `WORKSPACE_ENV_FILE` points to that same file):

```sh
docker compose --env-file output/public-course/.env -f compose.yaml -f quota-compose.yaml --profile build build
docker compose --env-file output/public-course/.env -f compose.yaml -f quota-compose.yaml up -d hub
```

Keep public firewall ports closed until certificates, identity, storage and
acceptance checks are ready. Rendered worker setup permits only operator SSH.
Opening ports, installing DNS and enrolling learners are separate operations.

## Acceptance and limits

`verify_quotas.py` requires root on a disposable Linux runner. It formats only a
new temporary loop image, proves byte/inode exhaustion, peer write independence,
Python/R operation, encrypted quota-backed recovery and quota-drift detection.
`verify_https.py` uses ephemeral loopback ports and self-signed fixture certificates
to check actual nginx behavior, forwarded headers and WebSocket upgrade handling.
Neither test uses an institutional identity provider or real learner records.

Before real use, rehearse allowed/denied identity login, active-session revocation,
root and per-user browser origins, real kernel WebSockets through TLS, certificate
renewal, reboot/mount recovery, target-host load and the actual off-host backup
account. Then run a complete Cairn assignment/submission/grading journey. The
synthetic encrypted recovery tool currently refuses institutional-mode packages;
institutional migration and identity preservation require their own rehearsal.
