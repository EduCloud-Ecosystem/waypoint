# Classroom live notebook integration checkpoint

## Outcome and scope

The actual Jupyter-to-Belay-classroom-to-Portage-to-OpenRouter tutoring route
completed, followed by native Save and explicit Cairn submission, byte-identical
repository reopening and receipt recovery. This is a fixed synthetic technical
candidate. It is not a general learner service or an accepted instructional pilot.
The notebook kernel was not executed in that joined run; actual Python/R execution
has separate Waypoint runtime evidence below.

Greg owns instructional acceptance. Engineering retains integration, model/policy
qualification, packaging and operations; the next engineering blockers are not
being transferred to Greg. The active mobile-IDE chat remains on its own branch.

## Frozen sources and review state

| Component | Branch and exact source | Review |
|---|---|---|
| Waypoint | codex/classroom-live-notebook-v1; runtime candidate137c38bb0b880d5b873d970a69bf647201b85e74; base3ec286f5d41e857622d9b24f23b3c31e6f248605 | [Draft PR66](https://github.com/EduCloud-Ecosystem/waypoint/pull/66), stacked on PR59, not main |
| Portage | codex/classroom-live-tutoring-v1;0653fd94fe86acf235a74f72a1ff4af6aa2fa1b9; base9298030d2aef6f011ae2b2720bca2e961bb1c866 | [Draft PR17](https://github.com/EduCloud-Ecosystem/portage/pull/17); gate and controls green; paid review skipped |
| Belay-classroom | codex/system-one-classroom-consumer-v1;c5a27073e74b9eeb7ab697190915378b50132883 | [Draft PR8](https://github.com/EduCloud-Ecosystem/belay-classroom/pull/8), stacked on PR7; SQLite/Postgres, quality, enrollment and integration checks green |
| Cairn | codex/classroom-enrollment-export-v1;0cf3158fdf5bbbecc52d3cc9cd88ef33bdfb63fc | [Draft PR38](https://github.com/EduCloud-Ecosystem/cairn/pull/38); eight checks green; no source changes here |

Waypoint worktree: /Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint.
Portage worktree: /Users/gregchism/.codex/worktrees/classroom-live-tutoring/portage.
Belay-classroom and Cairn remain in their existing /Users/gregchism/dev/.educloud-worktrees
checkouts named by branch. No original Belay, Outfitter, research or mobile-IDE source
was changed. No merges, production/cloud deployment or real learners were introduced.

## Independent evidence and limitations

- Live browser acceptance source:f6ee6cae926aea8f3a09077fd1e0b301344e99a6.
  Three actual transports,3276 tokens,USD0.0016332 provider-reported cost,
  zero SDK retries/new unknown liabilities. HistoricalUSD0.025 unknown remains.
  Provider-internal retries cannot be observed. Belay policy accounting is separate.
- Native Jupyter selection, signed roster/assignment authorization, real Belay
  planner/reasoner/self-evaluator, visible hint, expired/disabled refusals without
  new jobs/transports, no-AI route, explicit preview/confirmation, CSRF/peer/path
  controls, idempotent lost-response recovery and byte-identical receipt all passed.
- Clean6b2aad6fe56dfb757f9dba358d038de14edd627a zero-model regression passed,
  adding fresh instructor OAuth login and protected submission-repository access.
  The first instructor-read regression failed401 after restart; retained, not erased.
- Later changes clearly disclose external inference, add the instructor check and
  reuse immutable images between independent rehearsal peers. They do not change
  live model orchestration/provider source. No paid repeat was made.
- Local Waypoint source suite742 passed; notebook snapshot4 passed; presenter7
  passed. Portage580 passed/9 optional skips; focused adapter41 passed/3 skips.
  Peer image correction62 focused checks passed. Broader rehearsal suite328:
  319 passed,8 skipped,1 failed at concurrent pending-reconciliation loser-code
  assertion. Its existing assertion did not retain the unexpected code; unresolved. One bounded
  diagnostic reproduction passed with stale_reconciliation_approval and cleanup;
  that later pass does not resolve the first race failure.
- Hash-locked installed environments: Waypoint118 and Belay56 packages exactly
  match their locks. Tools and immutable image IDs are in the testing guide.
  Cairn checkout image was rebuilt because the previous local image was absent;
  mutable Alpine packages mean image distribution or second-host requalification
  remains necessary. No second-operator reproduction is claimed.
- Private raw evidence, ledgers, screenshots, all failures and hashes remain in
  output/live-notebook-v1. Publish only allowlisted metadata; no credential files.

## CI evidence and unresolved gates

Candidate6b2aad6's authenticated runtime and enrollment reports both passed,
including cleanup and source cleanliness. CI tested merge39f8f919e9b0b709ee5fdc26a58a19fe604117fb;
its tree2b9cf14391bebca7fcfd3e7b56009f3e7e62a6ce exactly matches6b2aad6.
[Run43528577074](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/43528577074).
This does not erase historical PR59's enrollment ConnectionError.

At6b2aad6, separate worker-channel evidence failed peer Python kernel POST after
source Python/R passed; services and learner containers remained running, cleanup
passed, root cause unproven. Three multi-course evaluators failed same-image
assertions after rebuilding peers independently. The correction pins the first
fixture's frozen image pair before starting its peer and preserves equality,
identity/storage separation and cleanup checks. Retired-course old-session HTTP
probe timed out instead of returning expected502/503. These distinct failures
remain retained under their original revisions.

Follow-up CI at137c38b independently passed multi-course allocation, resource
amendments and replacement cutover. Each retained JSON report is PASS with verified
cleanup and source16a5af6fe8f95abd7035b173e5bdace223180c85. That CI merge tree
8fa17732f0da3edc5911c78bac516326fe759db6 exactly matches137c38b. Equality checks
were preserved. These three image-selection failures are fixed and CI-verified.

The current course job132248877848 failed later in retirement at
verify_retirement.py:225 / recovery.py:85: Restic forget --keep-tag returned1.
This invocation progressed beyond the prior old-session timeout through export
and malformed-export controls. The wrapper discards private backend stderr; the
cause is unproven. Preserve the earlier timeout separately. Next diagnostic:
retain bounded private Restic version/stderr for this owned synthetic repository,
then verify the intended protected-snapshot policy; do not accept a failed
retention command or publish a retirement PASS. Final CI:13 successes,1 failure (course retirement),1 manual-only continuity skip.
Authenticated runtime and enrollment both independently passed again. The actual
worker-channel report also independently passed all17 phases, cleanup and source
verification on the same candidate tree. It uses one Docker daemon, not two-host
or production acceptance. The previous connection failures remain retained;
a later pass does not establish their root cause. Detailed check links and evidence
hashes are frozen in [classroom-live-notebook-ci.json](classroom-live-notebook-ci.json).

This checkpoint is a later documentation-only commit; it does not move the runtime
candidate pin or claim a new execution. All implementation worktrees are clean;
other checkouts and other-chat resources were preserved.
Manual-only continuity CI is skipped, not passed.

## Next executable work

1. Close any remaining runtime/retirement/reconciliation failures with bounded
   diagnostics and regression evidence; never retry an uncertain kernel POST or
   treat timeout as successful retirement. Preserve the completed tutoring proof.
2. Review the synthetic hint, disclosure, no-AI path and explicit receipt flow.
   Use [CLASSROOM-MVP-TESTING.md](../course-workspace/CLASSROOM-MVP-TESTING.md)
   for exact setup and the separate zero-model/live finite commands.
3. Replace the echo-only allowlist with the accepted course package, evaluate
   tutoring/pedagogy and provider policy, and package a usable test session before
   any real learners. Campus identity, approved data routing, support and release
   acceptance remain distinct. Outfitter/native Jetstream2 and the mobile IDE are
   not prerequisites for this local synthetic notebook path.

All owned local test services were stopped. Existing other-chat resources remain
untouched. A new live window is deliberate and finite; the completed window is
not resumed. No additional model calls are part of CI follow-up.

## Zero-model reproduction uses its own Belay pin

The live profile uses Belayc5a2707. The historical stub evaluator intentionally
uses656d6ec; do not switch or dirty the active live checkout to run it. Its existing
clean checkout is /Users/gregchism/dev/.educloud-worktrees/belay-classroom-enrollment-authority-v1.
From the Waypoint checkout with the documented environment and image pins:

```sh
.venv/bin/python course-workspace/rehearsal/roster_presenter.py \
  --cairn /Users/gregchism/dev/.educloud-worktrees/cairn-classroom-enrollment-export-v1 \
  --belay /Users/gregchism/dev/.educloud-worktrees/belay-classroom-enrollment-authority-v1 \
  --checkout-image sha256:bc9bc6929aafd69d37158bbd273a11818a2b222539242949d5d3c3066d02db8c \
  --grader-image python@sha256:726d304906fa5eb468e5531e366548d1903286352f5adb54ef508ac01fbd80c6 \
  --output output/classroom-stub-review-new
```

Expected: failed=[],cleanup=true,model_calls=0. The output directory must be fresh.
This is an automated browser rehearsal; it closes its own resources. The finite
live command in the testing guide uses the new Belay and Portage pins and actual
provider calls. Neither command is a persistent classroom deployment.
