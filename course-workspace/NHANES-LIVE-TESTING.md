# Test NHANES with notebook tutoring

This is a local instructor test using synthetic identities and a small public
NHANES extract. It uses native Jupyter, Belay-classroom hints through Portage,
and Cairn's explicit saved-file preview, confirmation, receipt and checker.
The notebook includes an operator-authored solution fixture. It is not a learner
template, an AWS deployment, or approval to admit students.

## Start as a first-time learner

On the prepared Mac, open `course-workspace/start-info511.command` and click **Sign in** in the prefilled disposable learner account. To work entirely offline with hints disabled, run the same launcher with `--offline`. Both modes open the distributed blank starter.

Dismiss the welcome or take the quick tour. Open **Lecture notes** on the right for a reading companion based on the assignment README. **Guide → Workspace tour** replays the introduction. Run the first cell and write both predictions. For optional live help, open **Tutor → Review course example**, confirm your predictions, and choose **Ask for a hint**. Only the prepared code and question are used. Opening the notes, tour or tutor never dispatches a model request. The tutor has its own shorter deadline.

Save the notebook, CSV, explanation and assistance declaration. Choose **Submit work**, preview those four files and explicitly confirm in Cairn. Keep the receipt. The local workspace closes after at most nine minutes of review within a ten-minute total session. Reopening starts a fresh disposable workspace. The reading companion has no recording or time links.

To reproduce the automated browser journeys on the same prepared host, use `course-workspace/launch-nhanes-instructor.command --verify-first-use-offline` or `--verify-first-use`. The second command requests one live hint with the usual seven-transport and USD 0.175 ceiling. Both write a private result under `output/`.

## Review the learner assignment

Open the [learner instructions](classroom/info511-nhanes-v1/learner/README.md) and the notebook beside them. Copy the entire `learner/` folder into a fresh workspace to keep the two relative data paths intact. For a portable review copy, run this from the Waypoint repository root:

```sh
git archive --format=zip --prefix=INFO511-NHANES/ --output=INFO511-NHANES-starter.zip HEAD:course-workspace/classroom/info511-nhanes-v1/learner
```

For a hands-on review on the prepared Mac, run `course-workspace/launch-nhanes-instructor.command --learner`. It opens the untouched starter in Jupyter as a synthetic learner, with the Cairn submission button available. You have up to nine minutes to inspect, edit, save and optionally submit. The terminal shows the private output folder. Your saved files remain in its `saved-work/` subfolder after the disposable services close. A timed or closed window records no human acceptance. For a longer content review, open the portable copy in your own Jupyter environment.

On the prepared Mac, run `course-workspace/launch-nhanes-instructor.command --verify-learner` to verify the blank package through receipt recovery. The rehearsal opens the actual starter, then supplies authored test answers using synthetic accounts. It leaves the original learner files untouched. At application revision `169fed8` all six controls passed with zero model requests and cleanup confirmed. The correct CSV earned 4/4 and the changed value earned 0/4. The private result SHA-256 is `f3103e41094756791335d125b89179b039acfa1ffdcd525f9d3263abd292bb41`. Two earlier checks selected ambiguous or hidden text in Jupyter. The passing check selects the rendered recall heading by its accessible role. Instructor approval of the rubric and human usability review are pending.

## Start on this Mac

Docker must already be running. The launcher uses the qualified local environments
and cached images; it does not install software, pull images or create cloud resources.

```sh
./course-workspace/launch-nhanes-instructor.command --check
./course-workspace/launch-nhanes-instructor.command --live
```

The current launcher uses merged Portage `4f7ac0d` through the existing private
key file. OpenBao is inactive: no address, CA or service token is supplied.
Use `--verify-live` to run one paid, automated walkthrough of the supplied question and four-file submission. The live modes use Belay `fc3e0cc`, which asks for one participant lookup in the original BPXO table. The offline modes retain their original source pins.

Use `--stub` for a no-cost automatic hint and a two-minute
notebook-and-tutor review. The completed solution is preserved, a separate
prepared question is selected, and the restored help page uses the original
expiry. Every invocation creates a fresh private evidence directory.
Do not rerun after an uncertain paid outcome until its liability is resolved.

Use `--verify-stub` for automated keyboard and 320 CSS-pixel checks. The fixture
issues one shorter-lived tutor credential and waits for it to expire. It then
saves in Jupyter, verifies a four-file Cairn receipt and confirms the jobs and
attempts counters stay unchanged after expiry.

In live mode, the browser first runs execution, grading and receipt controls.
Then a new code cell is added for the prepared question, preserving the completed solution cell, and the **Tutor** sidebar opens inside Jupyter. No model
request is sent automatically in this mode. The remaining help window is at most
four minutes and is shortened by the original credential expiry. The terminal
prints the tutor time and the separate workspace review time. The tutor panel
shows the hint countdown. After hint access ends, work may still be edited,
saved and submitted while the workspace and assignment are open. The whole
invocation is limited to ten minutes.

1. Expand **Selected cell & sharing** to review the exact selected code and provider disclosure, then review the question. The code includes a predicted output
   schema and observational unit. Confirm both boxes and click **Record prediction
   checkpoint**. This records presence only, not the notebook contents, consent or a grade.
2. Click **Ask for a hint**. Only the supplied example is accepted by the provider
   guard. Do not enter personal, student or private research information. The route
   uses OpenRouter/OpenAI `gpt-4.1-mini`, with a maximum of seven transport requests,
   800 output tokens each, USD0.025 reservation each and USD0.175 total. SDK/HTTP
   retries are disabled. A hint can require several planner/reasoner/evaluator calls.
3. Use **Back to notebook** to inspect/edit/run and explicitly save. Help does
   not save the notebook. If submitting after AI use, edit `assistance.md` to
   `Declaration: ai_assisted` with a brief summary; omit the private conversation.
   The test's previously saved no-AI submission remains a separate historical receipt.
4. Use **Submit work** to review the four selected files, preview
   their changes, then explicitly confirm. Inspect the exact repository receipt
   and the separate correctness result. Four CSV correctness points are not a full
   course grade or an LMS publication.
5. Close the browser or press Ctrl-C to stop. Cleanup also runs at the deadline.
   Retained output includes result/cleanup/accounting, local screenshots and failures;
   it must remain private. No persistent service is left for students.

Review hint accuracy yourself, especially missing measurements versus unmatched
participants, joins and unsupported population/causal claims. The model's own
self-evaluation is not instructional acceptance. No-AI completion remains available.

## Reproduction and limits

`NHANES-DESKTOP-TESTING.md` records the fixed Cairn/Belay sources, driver and
hash-locked kernel environment, grader/checkout images, dataset/checker binding,
and prior failed no-AI attempts. The live launcher additionally checks Portage
`4f7ac0d3e839012664d4b8272c8446376efc8f1b`. Runtime records the exact Waypoint source,
images, actual kernel packages and file hashes. The provider catalog is rechecked
before live admission, so a changed price/endpoint can refuse a fresh window.

The existing private key file is used in place; its contents never enter source
or reports. An older USD0.025 unknown liability remains recorded separately; a
new successful run does not resolve it. The owner-authorized merged-source rerun passed 13 synthetic UI controls and
eight live controls, with three requests and USD 0.0036024 provider-reported cost.
See [the exact verification record](../docs/merged-classroom-verification.json).
This record does not authorize repeating automated paid work. Human use requires
deliberately invoking this launcher and clicking Ask. Nothing resumes a stopped paid window automatically.

The Cairn export remains preserved separately from the explicit operator-owned
NHANES teaching-policy overlay. Membership, original deadlines, submission and
grading authority are unchanged. The isolated presence-checkpoint database is
synthetic only; optional monitoring is not enabled. Retain it privately with the
failure packet, and do not treat its test retention policy as a student policy.

Hosted sign-in, student data destinations, free-form tutoring acceptance, actual
course enrollment, host capacity/backup recovery and instructor approval remain
student-release gates. Mobile is deferred. Outfitter is not needed to provision
this local test; its native cloud acceptance is a separate gate.

## Learning Studio interface

The INFO 511 skin retains native Jupyter menus, file browser, cells, execution and
save controls. **Ask tutor** captures only the currently selected code cell; it
does not send a model request. The optional, resizable Tutor sidebar stays open
while you work and can be collapsed using Jupyter's right sidebar tab. The
question and selected-cell review precede a separate **Ask for a hint** action.

**Submit work** opens the existing saved-file picker. Check the four required
files, choose **Prepare saved files**, then **Continue in Cairn** to inspect
changes and explicitly confirm. A hint, saved notebook, repository receipt and
correctness result are distinct. There is no new course dashboard or AI chat
landing page. Inquiry and researcher benchmarking are separate development lines.
