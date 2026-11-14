# Classroom MVP: connected synthetic testing candidate

The notebook can now obtain live tutoring through Belay-classroom and Portage,
then save and explicitly submit the selected file to Cairn. No-AI completion,
current roster/assignment checks and repository receipts remain independent.
This is a local synthetic test candidate, not a student deployment.

## What was actually demonstrated

The live browser run at Waypoint f6ee6cae926aea8f3a09077fd1e0b301344e99a6
used native Jupyter code selection, a signed roster-bound Belay job, the ordinary
planner/reasoner/self-evaluator, actual Portage HTTP transport and live OpenRouter.
Three requests completed:3276 tokens, USD0.0016332 reported by the provider,
zero SDK retries, zero new unresolved liabilities. Provider-internal retries are
unobservable. Belay separately settled3545 policy-priced micro-units; that is
not the provider invoice. The historical USD0.025 unknown liability remains.

The same run passed expired-credential and instructor-disabled refusal without
additional jobs/transports, no-AI continuation, keyboard Save, explicit selected-file
preview/confirmation, peer/CSRF/path/idempotency controls, lost-confirmation
recovery, byte-identical repository reopening and receipt recovery after restart.
All owned processes/resources closed. Neither the echo fixture nor the tutor's
self-evaluation establishes instructional quality, grading validity or scientific
acceptance. Human instructional review remains required.

A later HTML-only correction clearly discloses external inference. It was rendered
at1280px/390px without additional paid calls. The unchanged stub journey passed at6b2aad6 as a separate zero-model regression,
including fresh instructor OAuth login and protected access to the submitted repository.
The first instructor-read regression failed401 after restart; its evidence is retained.

## Visible desktop review, without model calls

From this checkout, use a fresh output path:

```sh
cd /Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint
.venv/bin/python course-workspace/rehearsal/roster_presenter.py \
  --cairn /Users/gregchism/dev/.educloud-worktrees/cairn-classroom-enrollment-export-v1 \
  --belay /Users/gregchism/dev/.educloud-worktrees/belay-classroom-enrollment-authority-v1 \
  --checkout-image sha256:bc9bc6929aafd69d37158bbd273a11818a2b222539242949d5d3c3066d02db8c \
  --grader-image sha256:726d304906fa5eb468e5531e366548d1903286352f5adb54ef508ac01fbd80c6 \
  --operator-review-seconds 120 --output output/classroom-operator-review-new
```

It first runs the automated controls, then opens
an authenticated synthetic learner notebook in a visible Chromium window for at
most two minutes. Only the supplied example should be used. Choose a cell and
**Ask Belay about this code**, review the local-stub hint, return to the notebook,
save, and use **Submit selected saved files** to inspect the explicit Cairn path.
Run the supplied cell with Shift+Enter and check that it prints `ok`. If a kernel
chooser appears, select **Python 3 (ipykernel)**. This trusted synthetic local
process is not the isolated learner worker; actual worker Python/R acceptance is
recorded separately. The fixture already contains one test submission; subsequent changes still need
explicit confirmation. Tutoring is separate from saving, submission and grading.

This mode refuses `--live-portage`, `--env-file` and `--execute-approved`. It
restores the original hints policy after the disabled-control check but does not
renew the credential or course authorization. Expiry can still refuse tutoring;
saved work remains available during the finite window. Closing the review page
or browser ends the window early. The runner then closes its own services.
Keep the terminal running. Use a fresh output directory for every invocation.

`operator-review.json` records the window state and its before/after job counts;
`result.json` records cleanup. Opening or using the window does not record human
acceptance, instructional quality, real-student readiness or physical-phone
acceptance. The exact tested source and current release holds are in the latest
readiness handoff. This is a local testing session, not an always-on classroom.

## Compatible branch map

| Component | Candidate/base | Checkout and responsibility |
|---|---|---|
| Waypoint | `codex/classroom-live-notebook-v1`, base `3ec286f5d41e857622d9b24f23b3c31e6f248605` | `/Users/gregchism/.codex/worktrees/classroom-live-notebook/waypoint`; notebook integration, launch, browser acceptance. PR66 targets main and includes PR59; merge/release state is recorded in the current handoff. |
| Portage | `codex/classroom-live-tutoring-v1`, `0653fd94fe86acf235a74f72a1ff4af6aa2fa1b9` | `/Users/gregchism/.codex/worktrees/classroom-live-tutoring/portage`; bounded provider route and ledger. PR17 merged as2c9031dcf3a016b355c861e0a3c6398b234022de; paid review remains separate. |
| Belay-classroom | `codex/system-one-classroom-consumer-v1`, `c5a27073e74b9eeb7ab697190915378b50132883` | `/Users/gregchism/dev/.educloud-worktrees/belay-classroom-system-one-consumer-v1`; unchanged authenticated jobs, governance, SDK, accounting. PR8 merged as979d7fbbd55021beeae768f151bfd80462243d82, including PR5–7. |
| Cairn | `codex/classroom-enrollment-export-v1`, `0cf3158fdf5bbbecc52d3cc9cd88ef33bdfb63fc` | `/Users/gregchism/dev/.educloud-worktrees/cairn-classroom-enrollment-export-v1`; unchanged roster exporter, repository publication and instructor authority. PR38 merged asfc038eb93cba16fe8265718e8472aca6f9ecf2d9. |

Original Belay is untouched. Outfitter is not required for this local MVP; its
native Jetstream2 qualification remains separate. The separate mobile IDE work was merged independently through PR67.
Its source is preserved when integrating current main; phone testing and mobile
incorporation remain deferred. Dependency source merges are not a deployment.

## Reproduce

Prepare the exact checkouts and hash-locked environments using
[ROSTER-PRESENTER.md](ROSTER-PRESENTER.md). The new adapter is standard-library-only;
it adds no dependencies. The tested tools are Python3.12.13, uv0.11.16,
Go1.26.4, Node26.7.0, Docker client29.5.3/server29.8.2, Chromium153.0.8010.12,
macOS arm64. Installed package inventories and lock hashes accompany private
evidence. Live cost/model observations are dated evidence, not future guarantees.

Before a paid window, run the offline seam check with the Belay environment:

```sh
"$BELAY_SOURCE/.venv/bin/python" course-workspace/rehearsal/verify_live_notebook_contract.py \
  --belay "$BELAY_SOURCE" --portage "$PORTAGE_SOURCE"
```

Expected: planner/reasoner/self_eval, `passed: true`, actual provider requests0.

One explicitly selected live synthetic window (at most7 requests/USD0.175 reserved;
never reuse an output directory, prior window, or failed attempt):

```sh
.venv/bin/python course-workspace/rehearsal/roster_presenter.py \
  --cairn "$CAIRN_SOURCE" --belay "$BELAY_SOURCE" \
  --live-portage "$PORTAGE_SOURCE" --env-file "$OPENROUTER_ENV_FILE" \
  --execute-approved --checkout-image "$CHECKOUT_IMAGE" \
  --grader-image "$GRADER_IMAGE" --output output/live-notebook-v1/new-window
```

Use the existing protected credential file; never copy it into source or logs.
This command drives the real browser automatically and closes the test resources.
Review the retained screenshots, tutor reply, result and receipt. It is not an
unattended always-on classroom service. Only the supplied echo cell and question
are allowed upstream; changed content is deliberately refused. The original
300-second course authorization cannot be renewed by this test. Every new window
requires a fresh deliberate invocation; it is not an automatic retry mechanism.

Expected success: exit0, `failed: []`, `cleanup: true`, 3–7 model transports,
no new unknown charges, one committed receipt whose saved bytes match. Failure
must retain the output directory. A receipt verifies publication, not a grade.

Tested image identities:
- Checkout `sha256:bc9bc6929aafd69d37158bbd273a11818a2b222539242949d5d3c3066d02db8c`.
- Grader `sha256:726d304906fa5eb468e5531e366548d1903286352f5adb54ef508ac01fbd80c6`.
- Forgejo `codeberg.org/forgejo/forgejo:16.0.5@sha256:cf5f5ae6acf2ababca0ee3d255705b83a47f35b25e07fc931d694d60664053fe`.

Historical image IDs were unavailable on the current Docker daemon. A new checkout
image was built from the pinned Cairn Dockerfile before execution. Its Alpine
package acquisition is mutable; a new build is not byte-identical reproduction.
Export/share that owned image archive or qualify a new image on another host.
No image-distribution closure or second-machine reproduction is claimed.

## Testing and release boundaries

Repair checks:744 Waypoint source tests passed before main integration;4 notebook snapshot tests,7 focused presenter
checks, offline actual orchestration; Portage580 tests passed/9 optional checks
skipped,44 focused adapter cases with3 optional SDK skips, three generated profiles
matched. Exact candidate CI and retained historical failures are recorded separately.

PR59's historical runtime failed at the peer Python kernel call with a ConnectionError;
its saved report was independently inspected and confirms cleanup. Basic authenticated
runtime passed, enrollment runtime failed. The cause remains unproven; do not call
it a flake or weaken its acceptance. Candidate6b2aad6 also failed the peer kernel transport and retirement HTTP probe,
and three multi-course checks exposed independently rebuilt image mismatches.
The follow-up shares a verified immutable image pair without weakening equality
checks. These are separate from live tutoring acceptance. Read the final
[checkpoint](../docs/classroom-live-notebook-checkpoint.md) for exact CI status;
source integration and controlled synthetic testing can proceed with explicit
limits. The historical R WebSocket503 remains unexplained and holds a real
classroom release; subsequent passes do not establish a causal repair.

Next decisions after source checks: Greg reviews the hint/help/submission experience;
engineering replaces the echo fixture with the accepted course package and evaluates
its pedagogy and provider policy before learner testing. Institutional login,
approved real-data routing, support operations, remote deployment, merge/release
review, and native Outfitter capacity remain separate gates. The mobile IDE and
colony research are not prerequisites for this synthetic notebook acceptance.
