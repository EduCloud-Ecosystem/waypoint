# Synthetic identity-to-course rehearsal

This is a disposable acceptance harness, not a deployment command. It uses real
Keycloak and Forgejo instances with generated synthetic identities, the actual
JupyterHub runtime, and a pinned Cairn checkout. No institutional credentials,
cloud account, real learner record or existing deployment is used.

The workflow in `.github/workflows/course-rehearsal.yml` runs the complete path:

1. Create a new temporary loop image and mount XFS with project quotas. Provision
   two notebook homes with hard byte/inode limits. Only the newly created image
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
5. Execute Python and R over HTTPS notebook WebSockets. Save `solution.py`,
   download its actual contents and upload those bytes through the learner's
   authenticated Forgejo repository API. This represents the explicit file
   transfer required today; saving a notebook alone does not submit coursework.
6. Request grading as the instructor. A correct answer must receive 10/10; an
   incorrect answer must receive 0/10 even when the learner supplies a forged
   999-point `grading.json`. Check the learner-visible breakdown/history.
7. Reject an unrostered identity, student requests for instructor functions,
   another student's grade and another student's notebook file. Remove Alice
   from the Hub roster, stop active servers and recreate the Hub. Verify her
   previous notebook session and attempts to start another server are denied.
8. Remove only the invocation's containers/networks/volumes and unmount its
   temporary filesystem. Preserve the evidence in the supplied private output
   directory; a failed run must not produce the overall `PASS.json`.

The browser uses a fixture-only certificate exception; the Hub and Python/WSS
clients explicitly trust the generated CA and retain TLS certificate validation.
No trust certificate or hosts entry is installed on the user's computer. Chromium
and the HTTP client resolve only the fixture `.rehearsal.test` names locally.

## Run the full rehearsal

Use a disposable Linux worker with root access, Docker/Compose, `xfsprogs`,
OpenSSL, Git, Go 1.25+, Python 3.12+ and enough disk/RAM for the course images,
Keycloak, two bounded learners and the browser. The CI runner is the reference
execution environment. Do not run filesystem tests against a live class worker.

```sh
python3 -m venv .venv
.venv/bin/pip install -r course-workspace/rehearsal/requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
# Use the reviewed Cairn commit pinned by the workflow, in a separate checkout.
(cd /path/to/cairn && go build -o /tmp/cairn-rehearsal ./cmd/cairn)
sudo /absolute/path/to/.venv/bin/python course-workspace/rehearsal/verify_course.py \
  --cairn-binary /tmp/cairn-rehearsal --cairn-source /path/to/cairn \
  --output /tmp/educloud-course-rehearsal
```

If browser binaries were installed in a user-specific cache, set
`PLAYWRIGHT_BROWSERS_PATH` explicitly when installing and preserve it through
sudo, as the workflow does. The output directory must be new. Logs, disposable
keys and fixture database files stay in that mode-0700 directory. Never publish
the whole directory: CI publishes only the successful bounded report and the
three synthetic screenshots, retained for seven days.

## Run the Forgejo/Cairn phase locally on macOS or Linux

This subset needs Docker, the existing `educloud-python-r:2026-10-06` image,
a built Cairn binary and the Python/browser dependencies above. It uses a
synthetic source-file fixture instead of a live notebook, and reports that limit.
It does not exercise notebook identity, TLS or XFS quotas.

```sh
course-workspace/.venv/bin/python course-workspace/rehearsal/cairn_journey.py \
  --cairn-binary /tmp/cairn-rehearsal \
  --output course-workspace/output/course-rehearsal-local
```

## Institutional recovery rehearsal

See [encrypted recovery](../RECOVERY.md#recover-an-institutional-course) for the
current-roster restore command and `verify_institutional_recovery.py` acceptance
harness. It destroys the synthetic source workspace and restores saved homes
onto a new XFS filesystem while the independent fixture IdP remains available.

## Remaining production gates

Passing this harness does not establish institutional identity acceptance,
class-size capacity, uptime or provider disaster recovery. Select the actual
worker and off-host backup account, rehearse real roster changes, certificate
renewal/reboot, quota monitoring and incident response, then repeat the course
path with institution-approved synthetic accounts before enrolling learners.
Cairn/Hub roster synchronization and automatic file transfer are not implemented.
This run requests grading explicitly; Forgejo push-webhook delivery is a separate
gate. Cairn's new session fix is merged, but no production service is upgraded by
the rehearsal.

Fixture configuration follows the primary [Keycloak container](https://www.keycloak.org/server/containers)
and [reverse-proxy](https://www.keycloak.org/server/reverseproxy) guidance and
[Forgejo Docker installation](https://forgejo.org/docs/v16.0/admin/installation/docker/).
Images use explicit versions and manifest digests; fixture upgrades need a fresh
accepted run rather than assuming compatibility.
