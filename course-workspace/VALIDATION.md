# Observed continuity validation

## October 11 repeatable component pilot

Accepted Waypoint revision: `f05a504f640ef2f3574b7e2f9b7b032c2f17f84b`
(PR #9). The two successful acceptance runs below executed PR revision
`d6797980b701c162a58ce38bf11b7d76122fed4a`; its Git tree is identical to
the merge revision. GitHub conclusions and tree equivalence were checked on
2026-10-11. These results precede subsequent testing-readiness changes; rerun
the candidate to accept those changes.

- [Synthetic course rehearsal](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/38160152257)
  succeeded on Linux, exercising the full browser notebook-to-private-repository
  submission, grading, instructor review and simulated LMS-delivery journey.
  Required evidence covers Python/R, saved-file stop/resume persistence, learner
  isolation, roster revocation, restart persistence and bounded admission.
- [Institutional workspace recovery](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/38160152262)
  succeeded with current-policy restore, removed-user denial, old Hub session
  rejection, retained files/Python/R and quotas. This uses a fresh filesystem
  and namespace on the **same** runner with the synthetic IdP still available.
  It does not prove loss of that worker/IdP or access to a real backup account.
- The earlier recovery run
  [38159562190](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/38159562190)
  failed during teardown despite functional checks succeeding. It is not an
  accepted run. The subsequent fix verifies both original/replacement cleanup
  and publishes final success only afterwards.

The course workload includes 5/15 distinct authenticated Cairn HTTP users, not
15 concurrently executing notebooks or grading jobs. Notebook admission exercises
two active servers, rejects a third, then admits it after a slot is released.
Assessment proposals are deterministic fixtures with zero model calls; this
validates instructor review and delivery plumbing, not LLM quality or instructor
calibration. Fixture Git routing also does not prove hosted forge DNS/TLS.

No real worker, off-host repository, institution IdP or Brightspace tenant is
accepted by these runs. Human assistive-technology, zoom and mobile review also
remain distinct from automated browser checks. Use
[RUNBOOK.md](rehearsal/RUNBOOK.md) and the versioned
[testing worksheet](rehearsal/TESTING-CHECKLIST.md) for the next candidate.

The sections below are retained historical observations. Statements that OIDC,
full course rehearsal or institutional package restoration were unimplemented
describe their original dates and are superseded by the October 11 entry;
real deployment and class-size acceptance remain outstanding.


## October 7 recovery and hosted-preparation follow-up

Implementation head `7649109` adds hard quota-backed storage and recovery plus
reviewable HTTPS/Keycloak configuration. This follow-up supersedes the earlier
October 6 statements about unimplemented quotas/encrypted recovery. It does not
change the remaining live institutional acceptance gates.

- 31 local unit tests passed, including old recovery-bundle compatibility,
  checksum/architecture/link rejection, quota health and configuration refusal.
- The generated private configuration was exercised through its CLI locally:
  output mode 0700, file modes 0600, empty client secret, Compose loopback binding
  and a read-only quota-status mount were verified. No service was enabled.
- [Linux quota integration](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/37677205120)
  passed at this implementation head: a new disposable loop-backed XFS volume
  enforced 64 MiB/1,000-inode limits, a peer remained writable, Python/R and saved
  files worked, an encrypted quota-backed package restored to a fresh namespace
  with the same limits, and changed quota enforcement was detected. No physical
  disk was formatted. This is not a target-host capacity or reboot result.
- [Actual nginx verification](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/37677205127)
  passed at this head: configuration syntax, HTTPS redirects, root/user-subdomain
  TLS routing, replacement of spoofed forwarding headers, WebSocket upgrade and
  unknown-host TLS rejection. Certificates and backend were synthetic fixtures.
- [Independent-worker encrypted recovery](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/37673554357)
  passed for PR #4 at `65d6188`: source and replacement jobs were separate Linux
  runners; the replacement had no source images cached and recovered the saved
  file and Python/R from the encrypted package. A wrong key was refused.

No Droplet, institutional identity client, DNS/certificate or real off-host backup
account was provisioned. Public login/revocation, full browser OIDC/TLS behavior,
class-size load, selected-host recovery objectives and the full Cairn course
journey remain unverified. Recovery currently supports synthetic local-test
workspaces only. See [HOSTED-PILOT.md](HOSTED-PILOT.md) and [RECOVERY.md](RECOVERY.md).

## Original October 6 rehearsal

Executed October 6, 2026 (America/Los_Angeles), Docker Engine 29.4.3 on local
Docker Desktop, linux/arm64 containers. Synthetic alice/bob data only. No cloud
resources, institutional accounts or real learner records were used.

## Passed

- Built the digest-pinned Python 3.12 base, hash-locked JupyterHub 6.0.1 /
  DockerSpawner 14.0.0 Hub, and the combined Python/R course image.
- Five unit tests: required production credentials/roster, public test-auth
  refusal, invalid resource settings, stale-login roster rejection at spawn,
  and archive checksum/path/duplicate validation.
- `verify.py --reuse-hub-image python-r-pilot-hub:pilot` completed successfully
  with fresh `verify-4df00e92f4` and `verify-4df00e92f4-restore` deployments.
  Reused images had already been built from this implementation.
- Authenticated synthetic users through Hub login and notebook OAuth redirects.
  Python executed pandas arithmetic; R loaded ggplot2 and executed arithmetic,
  both over notebook kernel WebSockets.
- Each user's private marker survived server deletion/recreation and a complete
  offline archive/restore to new Hub/home volumes. Recovered Python/R kernels ran.
- Bob's authenticated request for Alice's file was rejected with 403/404.
- Inspected both runtime containers: non-root 1000:1000, 2 GiB memory and equal
  memory/swap ceiling, 1 CPU, 128 processes, read-only root, dropped capabilities,
  no-new-privileges, only the user's home mount and one internal network each.
- Direct TCP probes from Alice to Bob's notebook address and to 1.1.1.1:443 failed.
  Networks were separate; only the Hub joined each user network.
- Backup refused while the Hub still mounted its database volume. A second
  restore refused to overwrite the existing recovered volumes.
- The verifier removed its generated containers/networks/volumes after success.
- Playwright opened JupyterLab in a real browser; Python and R launcher tiles,
  Welcome.ipynb and the retained continuity.txt were visible after recreation.
  [Browser evidence](docs/python-r-workspace.png).

Tested image IDs (local Docker image IDs, not published registry digests):

```text
Hub:    sha256:92eabda1dd24389364965f7cf7c246b55d52628f6bb77edd3126498d80cb5ddd
Course: sha256:74352327a4d7386a1bf0474efa767066cf0020ffbb1e7a2bca6e4955772d58eb
```

## Problems found and corrected during rehearsal

The archive helper initially could not read a learner's mode-0700 runtime
folder after dropping all capabilities. Added DAC_OVERRIDE alongside CHOWN only
to the isolated archive helper; learner containers still drop every capability.
The smoke client initially received 403 from a fresh notebook API before its
OAuth browser exchange; it now visits the notebook HTML to complete login before
calling the API. The final fresh-deployment verifier includes both corrections.

## Explicit limits

This is same-host, new-namespace recovery, not an actual second physical host,
automatic failover, high availability or an uptime result. The screenshot used
the same local authentication mode; live institutional OIDC, issuer subject
mapping, wildcard HTTPS/user subdomains and revocation are not yet accepted.

Runtime limits were inspected and network/API separation was probed. CPU/RAM/disk
exhaustion, adversarial shared-kernel escapes, a class-size load test, idle/max-age
expiry under load and actual per-home filesystem quotas were not exercised.
There is no arbitrary provider or tenant broker. The default active limit is four,
not a supported class-size claim. This pass does not validate linux/amd64 builds.

Backups were local, unencrypted synthetic archives. Real deployment still needs
an approved encrypted off-host destination, retention, monitoring and restoration
with preserved images on the selected target architecture. Current builds depend
on upstream Debian/npm dependencies despite the base digest and Python locks.
The CI policy job and manually dispatched Docker job are supplied; only the
local executions above are claimed here.
