# Course setup manifest

An operator can describe one approved Python/R course in a versioned JSON
manifest and generate the configuration used by the existing deployment,
identity, proxy, recovery and health tools. The manifest records immutable Hub
and course images, the identity issuer/client and stable-subject roster, storage
and capacity policy, backup references, Cairn binding and support/restore owners.
It does not provision a host or apply changes to an identity provider or Cairn.

## Prepare and validate

Start from [course-manifest.example.json](course-manifest.example.json). Every
example host, subject, classroom and image digest is a placeholder; replace them
with approved values. Obtain the exact local image IDs with `docker image
inspect --format '{{.Id}}' IMAGE`, or use the immutable registry digest of the
tested image. A tag alone is refused. Preserve/load both images on the worker;
startup never pulls or builds them.

Keep the input manifest private: it contains roster subjects and infrastructure
references, although it contains no credential values. Use absolute canonical
paths. Store the OIDC secret and Restic password in separate owner-only regular
files (0600), with no symlink ancestors. The password needs at least 24
characters; both files must contain a literal value with no embedded whitespace
or shell/Compose interpolation. One terminal newline is permitted. The encrypted
backup password must also be escrowed independently.

Choose a fresh deployment directory under an operator-owned private parent.
`backup.staging_dir` must equal this directory: the recovery tool stages private
plaintext there. Home storage, local test repositories and secret paths must be
separate from it. Render never replaces an existing directory.

```sh
python3 course_manifest.py check \
  --manifest /etc/educloud/course.json --output /srv/educloud/python-r-deployment
python3 course_manifest.py render \
  --manifest /etc/educloud/course.json --output /srv/educloud/python-r-deployment
```

`check` validates configuration offline and does not read the secret files.
`render` also validates private secret files and atomically publishes a private
directory (0700) containing files restricted to their owner (0600). A valid
report establishes configuration consistency, not identity, image availability,
native quotas, backup reachability, capacity or public TLS acceptance. Refusal
reports contain fixed field names and guidance, never submitted values or raw
backend diagnostics. Both commands exit nonzero on refusal.

Institutional scope requires HTTPS origins and an SFTP or HTTPS S3 Restic
repository without embedded credentials. Explicit `synthetic` scope permits a
local repository and loopback HTTP Cairn for disposable rehearsals; it still
uses OIDC, TLS and quota-backed homes. It cannot establish off-host protection.
Unknown fields, mutable image references, inconsistent origins, overlapping
storage, invalid numbers and unsafe paths are refused.

## Review and consume the generated configuration

| File | Operator action |
| --- | --- |
| `.env` | Use with `deployment.py`; contains the private OIDC secret and immutable image pins. |
| `nginx.conf` | Review/install the server blocks on the host proxy, provide the referenced TLS files, and verify origin plus wildcard routing/WebSockets. |
| `keycloak-client.json` | Review/register the confidential PKCE client in the approved realm and assign the independently stored secret. |
| `recovery.json` | Use with `recovery.py init`, `backup`, `check` and retention; establish backend credentials/host trust separately. |
| `health.json` | Use with `health.py --config`; review thresholds and wire the institution's alert destination. |
| `cairn-binding.json` | Instructor reviews the classroom workspace URL; Cairn operator applies the approved classroom environment. |
| `manifest.json` | Normalized private setup record; preserve as the original configuration reference. |
| `validation.json` | Safe offline validation report; no roster, secret or infrastructure values. |

Cairn can be explicitly disabled with `{"enabled": false}`. When enabled, the
workspace URL must equal the course origin and submission budgets must match the
implemented contract: 32 files, 1 MiB per file and 4 MiB total. The binding file
does not authorize a learner or automatically change a classroom. Apply the
workspace URL through Cairn's instructor workflow and the generated approved
classroom settings through its operator workflow.

Follow [HOSTED-PILOT.md](HOSTED-PILOT.md) to provision every approved stable
subject's home on a dedicated XFS project-quota mount, with the manifest's byte
and inode limits. Review owners and the active limit against the actual worker.
Then use the generated directory:

```sh
sudo python3 deployment.py preflight --directory /srv/educloud/python-r-deployment
sudo python3 deployment.py start --directory /srv/educloud/python-r-deployment
sudo python3 recovery.py init --config /srv/educloud/python-r-deployment/recovery.json
sudo python3 recovery.py backup --directory /srv/educloud/python-r-deployment \
  --config /srv/educloud/python-r-deployment/recovery.json
sudo python3 health.py --config /srv/educloud/python-r-deployment/health.json \
  --output /srv/educloud/worker-health.json
```

Update generated systemd service paths/configuration before enabling timers;
their defaults still target `/srv/educloud/pilot` and `/etc/educloud`. Complete
the [testing checklist](rehearsal/TESTING-CHECKLIST.md) on the chosen host and
record off-host backup/replacement-host recovery before real learner use.

## Changes and recovery

Use stopped-course [maintenance](OPERATIONS.md#roster-and-quota-maintenance) for
roster/quota changes. The original manifest is a setup reference and does not
overwrite current authorization after maintenance. Prepare/review a fresh
manifest for subsequent setup changes; rendering into an active directory is
refused. Image upgrades and retirement remain separate operator workflows.

Encrypted backups preserve the private original manifest as
`source-manifest.json`. Restore retains it as historical context only, never as
an active manifest or a source of current authorization. Institutional restore
still requires independently supplied current credentials/roster; it pins both
archived Hub and course image IDs and creates fresh Hub sessions. See
[RECOVERY.md](RECOVERY.md) for the complete procedure and external dependencies.

The disposable Linux rehearsals consume the generated environment, identity
client, proxy policy, image pins, actual Cairn binding and recovery configuration.
Their safe evidence records the consumed manifest digest, rather than publishing
private manifest contents. This is component acceptance on synthetic infrastructure.
