# Repeatable component pilot

Testing package `educloud-testing-v1` (2026-10-11). Use the fillable
[acceptance worksheet](TESTING-CHECKLIST.md) to record candidate versions, owners,
stage decisions, hosted prerequisites and rollback. Historical successful runs
are recorded separately in [VALIDATION.md](../VALIDATION.md).

Run this against synthetic fixtures on a development machine or a disposable
Linux runner. It is an acceptance rehearsal, not a deployment, real student
pilot, institutional SSO certification or a measured class-size capacity claim.
No production LMS or provider credentials are required. Keep the output private:
it contains temporary fixture credentials, application databases and raw logs.

## Prepare reviewed inputs

Use a separate Cairn checkout at the reviewed revision. Build the application and
LTI simulator from that same checkout with the Go version required by its
`go.mod`. Build its dashboard with Node 22 before running: the rehearsal mounts
`web/dist` from that same checkout and hashes the built assets. Record clean source revisions for release acceptance. Development runs
may use a dirty checkout, but `PASS.json` records that fact and cannot establish
that the committed revision alone produced the result.

```sh
# Run in the reviewed Cairn checkout.
(cd web && npm ci && npm run build)
go build -o /tmp/cairn-pilot ./cmd/cairn
go build -o /tmp/cairn-lti-pilot ./cmd/lti-simulator
docker build -f Dockerfile.checkout -t cairn-checkout:local .
```

From the Waypoint repository root, prepare Python/browser dependencies and the
course image. The full Linux path also builds its Hub fixture.

```sh
python3 -m venv course-workspace/.venv
course-workspace/.venv/bin/pip install -r course-workspace/rehearsal/requirements.txt
course-workspace/.venv/bin/python -m playwright install chromium
test -f course-workspace/course/Dockerfile
docker build -f course-workspace/course/Dockerfile -t educloud-python-r:2026-10-06 course-workspace/course
docker image inspect educloud-python-r:2026-10-06 --format '{{.Id}}'
```

Linux workers additionally need Docker/Compose, Git, OpenSSL, `xfsprogs`, browser
system dependencies and root access for the disposable loop-backed XFS fixture.
Use Playwright's `install --with-deps chromium` on that runner. Only the newly
created loop image is formatted; never supply a physical disk or live class home
filesystem to a test command. The workflow is the reference full-run recipe.

## Choose the scope

**Local subset (macOS or Linux):** real Forgejo/Cairn OAuth, roster and grade
isolation, version-pinned submission grading, webhook replay, reviewed assessment,
simulated LTI passback, distinct-user load rehearsal and restart persistence.
The source file is synthetic; this path does not certify Python/R notebooks,
Keycloak, notebook TLS, XFS quotas or notebook recovery.

```sh
course-workspace/.venv/bin/python course-workspace/rehearsal/cairn_journey.py \
  --cairn-binary /tmp/cairn-pilot \
  --cairn-source /absolute/path/to/reviewed-cairn \
  --lti-binary /tmp/cairn-lti-pilot \
  --output /tmp/educloud-pilot-local-unique-run
```

**Full Linux:** adds actual Keycloak authentication, Python and R notebook
execution, saved-file handoff/persistence, cross-learner isolation, roster
revocation and bounded notebook load. It includes the local phases; a local
result cannot substitute for any missing notebook phase.

```sh
sudo /absolute/path/to/course-workspace/.venv/bin/python \
  course-workspace/rehearsal/verify_course.py \
  --cairn-binary /tmp/cairn-pilot \
  --cairn-source /absolute/path/to/reviewed-cairn \
  --lti-binary /tmp/cairn-lti-pilot \
  --output /tmp/educloud-pilot-linux-unique-run
```

Preserve the configured `PLAYWRIGHT_BROWSERS_PATH` through sudo when the runner
uses an explicit browser cache. Each command requires a **new** output directory.
Do not reuse a successful run's output to diagnose or rerun a failure.

## Interpret the evidence

`PASS.json` is created only after all required phases for its stated scope pass
and cleanup is independently verified. It must not exist when a phase fails,
cleanup is incomplete, provenance is missing, or report validation fails.
Intermediate phase evidence is diagnostic material, never an overall pass.
A printed subprocess success or successful removal command alone is insufficient:
verify child exit, invocation-owned resource absence and full-run unmount before
setting `cleanup_verified`.

The bounded, mode-0600 success artifact records:

- Cairn and Waypoint commit IDs and dirty-source flags; SHA-256 of both executed
  binaries and all executed versioned fixture modules; an aggregate digest and
  file count of the built Cairn dashboard bundle.
- Immutable local image IDs by public role (course, checkout, Forgejo, and the
  full-run Keycloak, nginx and Hub images).
- Operating system, architecture, visible CPU count, host memory and runtime
  versions. These are context, not guaranteed container reservations.
- UTC start/end times, elapsed duration, required phase outcomes, public check
  identifiers and numeric metrics. Distinct-user observations describe this
  synthetic workload and machine, not a production concurrency promise.

It contains no environment dump, machine name, usernames, filesystem paths,
image registry references, raw URLs, request/response bodies or application
logs. Unknown report fields are rejected rather than copied through. The exact
binary hash identifies executed bytes; source revision reporting alone is not
build attestation. For release acceptance, keep the build log and reviewed clean
checkout alongside the bounded artifact. A dirty-source run is development
validation even if every functional phase passes.

Publish only reviewed `PASS.json` and explicitly selected synthetic screenshots.
Never upload the entire output directory or an unfiltered phase report. Fixture
passwords, keys, OAuth client secrets and simulated LMS tokens remain private.

## Failure and recovery

Stop at the first failed required assertion. Retain private diagnostics, inspect
the invocation identifiers and determine whether child processes, containers,
networks, volumes or loop mounts remain. Remove only resources identified as
belonging to this invocation; do not prune a shared Docker daemon. If cleanup
fails, do not manually create or rename an artifact to `PASS.json`. Correct the
cause and rerun with a new output directory.

The fixture uses a generated Docker runtime wrapper only for labelled acquisition
containers: they share the disposable Forgejo container's network namespace so
the fixture's loopback Git URL resolves on macOS and Linux. This is test routing;
grading execution retains its configured network isolation and the acquisition
resource limits remain enforced. It does not validate production DNS, TLS or
worker-to-forge routing. The generated wrapper and fixture credentials stay in
the private output directory.

The rehearsal must not requeue uncertain real jobs, write production grades or
restore over a live database. Hosted backup acceptance, off-host restore,
institutional identity/roster approval, real Brightspace registration and actual
student-feedback calibration remain separate gates.

## Verify the evidence gate itself

```sh
python3 -m unittest discover -s course-workspace/rehearsal -p 'test_*.py' -v
```

These unit tests exercise phase/cleanup refusal, scope separation, provenance
completeness, sensitive-field exclusion, bounded output, atomic publication and
no-overwrite behavior. Passing them proves the report gate's tested behavior; it
does not mean either end-to-end rehearsal has run successfully.

## Separate institutional recovery rehearsal

On the same disposable Linux runner, after the full course run has finished:

```sh
sudo /absolute/path/to/course-workspace/.venv/bin/python \
  course-workspace/rehearsal/verify_institutional_recovery.py \
  --output /tmp/educloud-institutional-recovery-unique-run
```

This tests an encrypted synthetic institutional workspace restore into a fresh
namespace and filesystem using current policy, including removed-user denial
and invalidation of old Hub sessions. It keeps the synthetic identity provider
running on the same worker. Its final artifact requires verified cleanup of both
workspace instances and mounts; it is separate from the full-course PASS schema.
Neither this run nor the local subset proves recovery from the institution's
actual off-host repository onto a replacement worker. Complete Stage C of the
worksheet for that acceptance.
