# NHANES desktop testing, synthetic no-AI route

This finite local session binds `nhanes-v1` to a new disposable Cairn assignment,
runs the authored public-data notebook in native Jupyter, saves its four files,
checks an intentionally incorrect CSV, and uses explicit preview/confirm with
an exact repository receipt. It starts no Belay/Portage/provider service. This
tests the no-AI path, not tutoring, instructional quality or student readiness.

Historical task metadata remains `SOURCE_BOUND_RUNTIME_PENDING`; its original
download, source notebook and hosted fallback are unchanged. The working copy
contains clearly labeled operator-authored answers for synthetic testing only.
Do not distribute this solution workspace as a learner template. CSV correctness
is four points only, not a full-rubric grade or an LMS publication.

## Try the INFO 511 learner sign-in on this Mac

Open [Try INFO 511 sign-in](try-info511-sign-in.command) in Finder, or run
`course-workspace/try-info511-sign-in.command` from this checkout. Wait for the
local Forgejo login page with **alice** and a generated password already filled.
Click **Sign in** within three minutes. Cairn checks course membership and the
launcher opens the blank NHANES notebook. Run and save your work, then choose
**Submit saved files to Cairn** to preview and confirm the four files.

The launcher creates a fresh local course and individual disposable learner
credentials every time. The entire session lasts at most ten minutes, including
setup. The notebook uses a separate local session token. If the sign-in form
closes or times out before submission, the report records an unstarted review
(exit 2). Errors after a submitted sign-in or during cleanup remain failures.

**To try hints with the blank starter**, open [Try INFO 511 with local hints](try-info511-local-hints.command)
instead. Select a code cell and choose **Ask tutor**. Review
the selected text and record the requested predictions before asking for a hint.
Belay returns a simulated hint locally. Tutor access lasts at most four minutes
from setup. Choose **Back to notebook** to resume editing. **Continue without AI**
turns off hints for that page. You can still save and submit after help expires. The original launcher keeps tutoring off.
The automated counterpart is `launch-nhanes-instructor.command --verify-learner-hints`.
To repeat it, reopen the launcher. Closing the notebook ends the
review. Keep the terminal open for cleanup.

## Fixed sources and environment

- Waypoint base: `96cc2b8c1922f06bb28933908d426a2efa849599`;
  isolated branch `codex/waypoint-nhanes-qualification`.
- Cairn repair: `2eb0a0415f791f7d53734d5909a3a3d7454e34f0` at
  `/Users/gregchism/.codex/worktrees/cairn-nhanes-handoff/cairn`.
- Belay checker source: `c5a27073e74b9eeb7ab697190915378b50132883` at
  `/Users/gregchism/dev/.educloud-worktrees/belay-classroom-system-one-consumer-v1`.
  Its checker hash must match the original package binding; no model runtime is used.
- Driver: existing hash-locked presenter environment at
  `/Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint/.venv/bin/python`.
- Separate kernel: Python3.12.13, pandas2.3.3, ipykernel7.4.0; full dependency
  hashes are in `rehearsal/nhanes-kernel-requirements.lock`. Each report also records
  the actual interpreter and every installed distribution/version.
- Existing grader image: `sha256:d8e7745e5644f4e4c4dd49902981a8953485cc188a2263d866ba042aa0e4fab7`.
  Its Python3.12.16/pandas2.3.3 differ from the native kernel and are recorded
  separately; successful correctness must be observed in this actual container.
- Existing checkout image: `sha256:bc9bc6929aafd69d37158bbd273a11818a2b222539242949d5d3c3066d02db8c`.
  Forgejo uses the existing fixture's digest-pinned16.0.5 image. No image is pulled.

## Prepare and launch a fresh finite review

The exact sources above must be clean and available, with Docker running and
the existing Cairn dashboard built (`npm ci --ignore-scripts` then `npm run build`
in its `web/` directory). Use a fresh output directory for every attempt; never
overwrite/resume a failed invocation.

```sh
cd /Users/gregchism/.codex/worktrees/waypoint-nhanes-qualification/waypoint
uv venv --python 3.12.13 output/nhanes-kernel-new
uv pip sync --python output/nhanes-kernel-new/bin/python \
  course-workspace/rehearsal/nhanes-kernel-requirements.lock
/Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint/.venv/bin/python \
  course-workspace/rehearsal/nhanes_desktop.py \
  --cairn /Users/gregchism/.codex/worktrees/cairn-nhanes-handoff/cairn \
  --belay /Users/gregchism/dev/.educloud-worktrees/belay-classroom-system-one-consumer-v1 \
  --kernel-python "$PWD/output/nhanes-kernel-new/bin/python" \
  --kernel-lock course-workspace/rehearsal/nhanes-kernel-requirements.lock \
  --grader-image sha256:d8e7745e5644f4e4c4dd49902981a8953485cc188a2263d866ba042aa0e4fab7 \
  --checkout-image sha256:bc9bc6929aafd69d37158bbd273a11818a2b222539242949d5d3c3066d02db8c \
  --output output/nhanes-instructor-review-new \
  --operator-review-seconds 120 --execute-synthetic
```

The visible browser first runs automated synthetic controls, then offers at most
two minutes for inspecting the supplied notebook/receipt. Keep the terminal
running. Closing the review window ends it early; the runner closes its own
browser, notebook process group and Cairn/Forgejo resources in `finally`.
The whole invocation is capped at10minutes. There is no persistent server or
published URL. Review `result.json`: any failed check or unverified cleanup is
a failed run. The optional human window is not itself human acceptance.

The local notebook is a trusted operator process, not the isolated worker
runtime. No campus identity, AWS host, real enrollment or paid inference is
configured. GitHub/Keycloak and later Shibboleth/SAML remain separate hosting
work; SSH is operator transport. Mobile stays deferred.

## Retained failures and targeted repairs

Private evidence is under `output/nhanes-qualification-v1/`:

1. `attempt-001`:151.84seconds, native output192 and save passed, then the
   submitted job failed before grading because Cairn's handoff producer emitted
   a legacy unpinned queue context. Cleanup passed. Cairn2eb0a04 fixes that producer
   using the existing `gradingjob.Capture` contract; queue checks remain strict.
2. `attempt-002`:73.12seconds, native output192, wrongCSV0/4 with the actual
   `source_value` diagnostic, correctCSV4/4, four-file native preview/confirm,
   changed idempotent preview payload409 and exact repository-byte receipt all
   completed. Supplemental restart readback failed401 because the evaluator
   reused the invalidated session. Cleanup passed and no owned kernels remained.
   The report remains FAIL. The helper now signs in again through synthetic OAuth
   before readback; two focused call-order/failure-cleanup tests pass.

The classroom coordinator explicitly amended the original two-attempt limit
for exactly one additional no-AI attempt, capped at10minutes, to test that
identified fresh-login correction. History/counts are not reset; unexpected
failure stops the extension. `attempt-003` passed in 72.39 seconds on clean
Waypoint `fa763d8bd4de950e59c16c3ecfe348df16b10c14` and Cairn `2eb0a04`:
native output192, incorrectCSV0/4, correctCSV4/4, exact four-file receipt,
changed-preview refusal and receipt readback after restart with fresh OAuth.
Cleanup passed; independent process/container inspection found no remaining
owned kernel or fixture service. Both notebook and receipt screenshots were
visually inspected. No provider transport occurred.

The three attempts consumed 297.35 seconds of runtime in total. The amended
budget is closed; the earlier failures remain retained. This is automated
synthetic qualification, not human acceptance or hosted deployment. The optional
human review window was not exercised. The classroom chat owns the later,
separately authorized live-tutoring qualification and its accounting limits.
