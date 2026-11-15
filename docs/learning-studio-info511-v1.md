# INFO 511 Learning Studio: IDE-first implementation

## November 15 release-blocker fixes

The desktop UI audit reproduced lost focus and silent receipt transitions,
320 CSS-pixel overflow, and an instructor rehearsal that ended with the tutor
credential. Waypoint retains the native Jupyter controls and switches the tutor
to a separate narrow pane. The notebook and tutor state persist when the learner
returns to the editor. The browser review ends at its own bounded deadline
inside the original ten-minute maximum. Tutor credentials and the provider
reservation keep their existing deadlines.

Cairn's pilot branch adds focused preview and result headings, a persistent
receipt status region and keyed receipt rows. It makes the existing submission
form and instructor review reflow at 320 CSS pixels. Instructor publication
reports the saved result and moves focus to it. The assignment, receipt, grading
and instructor authority contracts are unchanged.

Use `course-workspace/launch-nhanes-instructor.command --verify-stub` to rerun
the narrow keyboard and post-expiry journey on the new Cairn pin. The dedicated
stub qualification uses a 90-second synthetic tutor credential and no provider.

Greg's November 14 direction replaces the busy studio mockup with a restrained reskin of the familiar Jupyter IDE. Keep code, files, outputs and native notebook controls. Add contextual tutoring in a collapsible Jupyter side panel; preserve the explicit saved-file preview, confirmation and receipt workflow. Inquiry is a later educational experience for research-driven courses, final projects and Capstones. Researcher POC/benchmarking is a different product, now in chat 01a1dcd5-6d15-7b82-aa9e-3eabdfb1e376, beginning from talks/education-compute.

## Ownership and baseline

Root owns this implementation in `/Users/gregchism/.codex/worktrees/waypoint-runtime-release/waypoint`, branch `codex/nhanes-live-tutoring`, base `3376e097240d56f822b9d9a64af52b804aabd9af`. The tree was clean. The Waypoint chat is idle and was notified of the boundary. Existing component checkouts and historical failures remain untouched. Cairn, Belay-classroom and Portage pins remain those in the instructor launcher. No original Belay or mobile work.

## Scope

- Existing Waypoint Jupyter extensions, roster-help assets, saved-file picker presentation, focused controls, NHANES test helper, launcher and testing documentation.
- Enable the integrated panel only from an explicit operator-selected INFO 511 profile with matching registered course binding. Other courses retain their existing popup path.
- Same-origin notebook embedding only; preserve signed identity, XSRF, exact message origin/window/nonce checks, selected-code-only capture, finite authority and provider guard. Never embed the separate Cairn authority or replace its confirmation/receipt.
- Keep the live allowance at seven requests / USD0.175 reservation, prepared public/synthetic question only, no SDK retries and no credential renewal. Display remaining hint time and make the manual path obvious.
- Simplify presentation through progressive disclosure; preserve checkpoint requirements, no-AI access, provider disclosure, saved-file warnings and explicit submission.

## Finite verification

Run relevant offline Python/Node controls, then one fresh full local NHANES stub journey (maximum 600 seconds). Preserve failure outputs and cleanup evidence; a correction run requires recording the concrete failure and fix here. Verify real kernel output, bad-output rejection, correct result, exact four-file receipt, stale-preview refusal and restart recovery alongside the embedded tutoring route. Inspect the actual browser layout. Use existing environments/cached images; no installation, cloud provisioning or student admission.

No new paid automated run is part of this stage. The previously qualified provider route stays available through a deliberately invoked finite instructor launcher and explicit Ask click. New embedded UI verification is synthetic/local-stub evidence, distinct from historical paid transport qualification and subsequent human acceptance.

## First verification and bounded visual correction

Source `a90995e`: 764 Python tests, five JavaScript handoff tests and six NHANES helper tests passed. Full `output/learning-studio-v1/stub-001` passed all eight integration stages in 119.88 seconds, including embedded stub tutoring, no-attempt refusal controls, native 192-row output, incorrect CSV 0/4, correct CSV 4/4, exact four-file receipt, stale-preview 409 and receipt readback after restart. Cleanup succeeded; actual provider requests were zero. Private raw output and screenshots are retained in that directory.

Visual inspection found the native Jupyter news solicitation obscuring the workspace. The live review helper also needed to scroll back to the tutor heading after preparing the question. Correct those presentation issues using isolated per-run Jupyter settings (news/update fetching off; error notifications unchanged) and an explicit initial scroll position. Capture the default collapsed context and the saved-file picker. Permit one correction run `stub-002`, maximum 600 seconds, with the same pins and local stub only. Do not erase or replace `stub-001`; no new paid run or student admission is authorized.

The stub launcher previously left the panel in the disabled/no-AI state from its refusal control. Before the instructor review, reload the restored original profile and show a fresh prepared question in a separate cell, preserving the completed solution. Use the same review helper as live mode but label local-stub evidence explicitly and retain the original expiry. The correction run includes a 15-second bounded stub review to exercise this otherwise manual presentation path; it records no human acceptance.

## Final instructor-test gate

Correction source `329faf7182b2862fb06fe3d33239802019a8c3d6`: `stub-002` passed the same eight stages and a 15-second prepared instructor-review window in 162.62 seconds. No failed controls, no paid provider requests, cleanup true. Six helper tests passed after the correction. The 764-test Python suite and five JavaScript controls passed against the application changes; subsequent changes were confined to review/rehearsal presentation and documentation.

Inspected actual browser screenshots: default collapsed sharing details, embedded hint, saved notebook, saved-file picker and prepared review preserving the completed work. The news prompt is absent. Both runs and raw controls are preserved under `output/learning-studio-v1`; this private evidence is not committed. The machine-readable companion records pins and result hashes.

Ready means a local synthetic instructor test with a public NHANES subset. Use the launcher and `course-workspace/NHANES-LIVE-TESTING.md`. `--stub` exercises the workflow without a paid provider; `--live` retains the previously qualified prepared-question guard and only asks after an explicit click. No new paid test was run for this UI, and no human instructional acceptance, hosted deployment, institutional identity, student admission or mobile qualification is claimed.
