# Reviewed course image upgrades

`upgrade.py` prepares and applies a stopped institutional course's Hub/course
image change. It requires compatible preloaded immutable images, a successful
full Linux canary and a fresh encrypted backup. It changes only the image pins
in the private environment; roster, identity, origin, quotas and saved work stay
under the existing course policy. It never starts, pulls, builds or automatically
rolls back a course.

## Test the candidate images

Build and review both images outside the learner deployment. Preserve their
immutable registry digests or local image IDs and architecture. Preload them on
the disposable native Linux canary runner and target worker. Use the existing
[full rehearsal setup](rehearsal/README.md) to prepare Cairn, the local LMS
simulator, checkout image, browser dependencies, XFS tools and the synthetic
fixture. Run ordinary Python without `-O`:

```sh
sudo --preserve-env=PLAYWRIGHT_BROWSERS_PATH python3 rehearsal/verify_course.py \
  --cairn-binary /secure/build/cairn --lti-binary /secure/build/lti-simulator \
  --cairn-source /secure/build/cairn-source --output /secure/course-canary \
  --hub-image sha256:FULL_HUB_IMAGE_ID --course-image sha256:FULL_COURSE_IMAGE_ID
```

The paired image arguments skip image builds and candidate pulls. The fixture
still runs independent synthetic identity, proxy and classroom services. It
tests real browser login, Python/R, file persistence, isolation, revocation,
bounded load, explicit saved-file submission, grading and simulated passback.
Its `PASS.json` is published only after the required phases and cleanup succeed.
Optimized Python is refused because it would disable acceptance assertions.

Keep the reviewed report and candidate images together. Upgrade preview requires
full-linux evidence version 4, the current fixture file hashes, clean Cairn and
Waypoint sources, exact candidate image IDs, all required phases, verified
cleanup and zero fixture HTTP errors/model calls. A local subset, changed
fixture, mismatched image or partial report is refused. This is a trusted
operator artifact, not a signed remote attestation or live-host certification.

CI's `--synthetic-candidate-images` mode derives two distinct images with a
synthetic label from the current built images. It verifies the workflow with
distinct runtime identities; it does not claim a dependency upgrade or evaluate
an institution's future image changes. Operator-provided images must pass their
own current canary and image vulnerability/update review.

## Stop, preview and review

Use a private canonical deployment directory and separate owner-only recovery
configuration/password files. Establish the repository and escrow its password
as described in [RECOVERY.md](RECOVERY.md). Plan and evidence paths must be
canonical; the new plan's parent must be private. Plans cannot overwrite
transaction files or live homes/Hub data, backup storage or secret/configuration
references.

Save edits and announce the maintenance window. Stop the Hub and every writer
of this course's volumes. Preview refuses an active Hub/learner or an unrelated
running container using the Hub database or a retained home.

```sh
sudo python3 deployment.py stop --directory /srv/educloud/course
sudo python3 upgrade.py preview --directory /srv/educloud/course \
  --hub-image sha256:FULL_HUB_IMAGE_ID --course-image sha256:FULL_COURSE_IMAGE_ID \
  --canary /secure/course-canary/PASS.json \
  --recovery-config /srv/educloud/course/recovery.json \
  --plan /secure/course-upgrade-plan.json
```

Preview holds the deployment and home-operation locks while checking native
worker/image compatibility, quota policy and saved-file state. It takes a fresh
encrypted backup of the stopped source and verifies the exact snapshot's course
ownership. It refuses changes during backup. The private plan binds image IDs,
source configuration, retained files/Hub data, mounted storage, original manifest,
canary bytes, backend configuration/password and tool dependencies.

Review the private plan, image changes, snapshot and canary before applying.
The CLI summary contains image IDs, counts, plan digest and snapshot ID, not
private paths, subjects, credentials or configuration/password fingerprints.
No backup is deleted by this workflow. Keep the prior images and independently
escrowed key available until the retention owner approves their retirement.

## Apply and explicitly start

```sh
sudo python3 upgrade.py apply --directory /srv/educloud/course \
  --canary /secure/course-canary/PASS.json \
  --recovery-config /srv/educloud/course/recovery.json \
  --plan /secure/course-upgrade-plan.json
sudo python3 deployment.py start --directory /srv/educloud/course
```

Apply checks the exact frozen inputs and stopped state before creating a private
interruption journal and atomically writing the new pins. It validates the
candidate environment and retained state before publishing
`.upgrade-receipt.json` and removing the pending marker. `APPLIED` means stopped
configuration was updated; it does not mean a runtime was started or accepted.
Startup uses preloaded images with no pull or build. Verify actual login,
saved files, kernels, routing and image identities before reopening the course.

The stopped old Hub container remains until explicit startup recreates it.
Taking another backup while its actual image differs from the configured Hub pin
is refused, preventing an archive with inconsistent runtime/configuration. The
verified pre-upgrade snapshot remains available throughout this interval.
The original setup manifest remains historical context; the receipt records the
new runtime pins. Roster/quotas still use stopped-course maintenance separately.

## Interrupted or uncertain outcomes

An unfinished `.upgrade-pending.json` blocks startup, backup and roster/quota
maintenance. Emergency stop/status remain available. Resume with the same plan,
canary and recovery configuration:

```sh
sudo python3 upgrade.py resume --directory /srv/educloud/course \
  --canary /secure/course-canary/PASS.json \
  --recovery-config /srv/educloud/course/recovery.json \
  --plan /secure/course-upgrade-plan.json
```

Resume accepts only the recorded old or new environment and unchanged saved
state. Changed files, roster, quota policy, mounted storage, image identity,
canary, password, configuration or tool bytes require operator investigation;
the tool does not clear a marker to conceal drift. Do not manually erase journals
or run direct Docker/storage mutations during the transaction. If the response
was lost after completion, inspect the durable receipt and actual environment;
reapplying a completed stale plan is refused.

## Recover the prior runtime

Use the receipt/plan's exact snapshot and [institutional restore](RECOVERY.md#recover-an-institutional-course)
into a fresh deployment directory and prepared XFS destination. Independently
supply current authorization and credentials. Recovery loads preserved prior
Hub/course images and saved homes, starts no service itself, and discards prior
Hub sessions/database state. The archived old Hub database is retained encrypted
for investigation; it is not reactivated.

A data/schema change cannot be reversed by simply swapping an older image pin.
Keep the failed/current worker stopped during cutover. Files saved after the
pre-upgrade snapshot require deliberate export/reconciliation before returning
to that snapshot. Verify files, Python/R, authorization and TLS before routing
learners to the replacement. Support, off-host credentials, capacity and actual
replacement-host acceptance remain institution-specific.

## Repeatable component acceptance

After a passing full candidate canary, the disposable Linux upgrade rehearsal
uses the same public CLI implementation:

```sh
sudo --preserve-env=PLAYWRIGHT_BROWSERS_PATH python3 rehearsal/verify_upgrade.py \
  --canary /secure/course-canary/PASS.json --output /secure/upgrade-rehearsal
```

It checks changed inputs/files, interrupted durable apply/resume, stopped-state
preservation, real candidate images, saved files and Python/R. It then restores
the exact prior snapshot to a fresh namespace and verifies prior images, fresh
sessions and saved work. `UPGRADE-PASS.json` requires all 20 observed controls
and verified cleanup. Its selected screenshots and digests are safe synthetic
evidence; environments, manifests, journals and backup keys remain private.
This same-worker rehearsal does not establish institutional off-host recovery.
