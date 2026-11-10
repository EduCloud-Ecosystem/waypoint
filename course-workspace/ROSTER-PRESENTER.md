# Roster-bound classroom presenter (synthetic integration)

This opt-in presenter connects an isolated notebook to a specific current Cairn
roster grant and Belay assignment. The learner reviews one selected code cell,
asks an optional question, and can continue without AI. Saving, explicit selected
file publication, repository receipts and human help remain separate actions.

It extends the existing cookie/XSRF notebook proxy and reuses Belay's job recovery
client and Cairn's saved-file bridge. It does not introduce a roster service,
identity issuer, model provider, grading authority or automatic submission.
Historical `legacy` and `nhanes-v1` presenter profiles retain their behavior.

## Current boundary

This is explicitly **loopback synthetic integration**, enabled only with
`WORKSPACE_BELAY_LOCAL_TEST=1`. The rehearsal uses real local Forgejo OAuth/Cairn
rosters, the finite Cairn exporter, RSA-signed synthetic identity, native
JupyterLab, Belay HTTP/jobs/worker and real repository publication/receipt APIs.
The response is Belay's deterministic `local_stub`: no model endpoint is called.
The notebook uses an isolated local token login, not institutional single sign-on.

Portage transport acceptance is separately pinned in the Belay enrollment
evaluator. This journey does not exercise Portage, Outfitter, live GenAI, native
cloud placement, pedagogical quality or real students. It neither merges nor
releases the dependent draft interfaces.

## Operator profile

Set `WORKSPACE_BELAY_ROSTER_PROFILE` to an absolute private regular JSON file,
and set `WORKSPACE_BELAY_LEARNER_ID` to its exact existing roster learner label.
The original proxy API/asset/workspace origin settings are still required.
Use the precise keys produced by the rehearsal as a **synthetic example**:

| Fields | Authority and meaning |
| --- | --- |
| `version` | Integer `1`; reject unrecognized fields. |
| `issuer`, `audience`, `subject` | Explicit operator-reviewed identity binding. Never inferred from a username or browser selection. |
| `institution_id`, `class_id`, `learner_id` | Exact current Cairn export membership; learner ID is the existing 32-hex roster label. |
| `authority_id`, `grant_generation` | Exact exporter authority and grant generation; not minted by Waypoint. |
| `authorization_file` | Absolute private path to the current finite Cairn export. |
| `credential_file` | Absolute private path to a signed, at most 300-second identity credential. It stays on the notebook server. |
| `expires_at` | Original finite profile deadline, at most 900 seconds ahead when bound. It cannot be extended in an existing notebook binding. |
| `exercise_id`, `exercise_version`, `assignment_title` | Operator-reviewed Cairn assignment to Belay pack mapping and displayed title. |
| `assistance` | `hints` or `disabled`, the operator's local presenter restriction. It is not a replacement for Belay-wide instructor policy. |

Profile, snapshot and credential files must deny group/other access, be bounded
regular files, and have no symlink traversal. Keep them outside the learner's
contents root. The profile is reread before dispatch. Only `assistance` may
change in-place; identity, grant, assignment or deadline changes require a fresh
operator binding. A rotated credential is reread for each request. Belay still
verifies signatures, current scope and the durable rollback watermark at its
HTTP and worker boundaries. A presenter observation is never authority to bypass
those checks. Disabling this presenter does not revoke other clients' grants.

Expired/refused tutoring leaves the draft and saved work intact. The learner can
return to the notebook, submit selected saved files, or use the instructor/TA
contact in course materials. Tutoring does not save or submit anything. A Cairn
receipt proves publication of the selected bytes, not scientific or grading
acceptance. Optional conversation persistence remains disabled in this fixture.

## Reproduce from fresh environments

Required dependency checkouts, including unchanged tracked source:

- Cairn `0cf3158fdf5bbbecc52d3cc9cd88ef33bdfb63fc` (draft PR38).
- Belay-classroom `656d6ec1e4be5f9ef7267f5a2bb5145a90238dfe` (draft PR7, stacked).
- This Waypoint branch; use the exact reviewed commit from the handoff.

Tools used locally: CPython 3.12.13, uv 0.11.16, Go 1.26.4, Node 26.7.0,
Docker 29.8.2 on macOS arm64. The complete presenter dependency lock is adjacent
to the rehearsal. Belay's complete dependency lock is in its pinned checkout.
Cairn uses its pinned Go modules and dashboard npm lock.

From a clean Waypoint checkout, with `CAIRN_SOURCE` and `BELAY_SOURCE` pointing
to those checkouts, prepare ordinary local dependencies:

```sh
uv venv --python 3.12.13 .venv
uv pip sync --python .venv/bin/python --require-hashes course-workspace/rehearsal/roster_presenter-requirements.lock
uv venv --python 3.12.13 "$BELAY_SOURCE/.venv"
uv pip sync --python "$BELAY_SOURCE/.venv/bin/python" --require-hashes "$BELAY_SOURCE/deployment/portage/requirements.lock"
npm --prefix "$CAIRN_SOURCE/web" ci
npm --prefix "$CAIRN_SOURCE/web" run build
.venv/bin/python -m playwright install chromium
docker pull codeberg.org/forgejo/forgejo:16.0.5@sha256:cf5f5ae6acf2ababca0ee3d255705b83a47f35b25e07fc931d694d60664053fe
```

Supply preloaded immutable `CHECKOUT_IMAGE` and `GRADER_IMAGE` Docker image IDs.
The tested IDs are recorded in the acceptance result. No implicit pull/build of
these images occurs during the journey. The checkout image can be built from
Cairn's pinned `Dockerfile.checkout`, but its Alpine package acquisition is not
byte-reproducible from source alone. The previously built grader image also needs
an operator-provided image archive/registry reference for another machine.
These are explicit image-distribution gaps; do not substitute a tag and claim
the identical environment. The grader is not a tutoring provider.

One bounded **start/check/stop** command (choose a never-used output directory):

```sh
.venv/bin/python course-workspace/rehearsal/roster_presenter.py \
  --cairn "$CAIRN_SOURCE" --belay "$BELAY_SOURCE" \
  --checkout-image "$CHECKOUT_IMAGE" --grader-image "$GRADER_IMAGE" \
  --output output/presenter-new-run
```

The command builds the two pinned Cairn binaries, starts disposable local fixture
resources, runs browser checks, and stops its processes and labelled Docker
resources in `finally`. The deadline is 900 seconds; interruption should be Ctrl-C,
not SIGKILL. Existing outputs are refused. Inspect `result.json` for passed/failed
checks and `cleanup: true`; exit zero requires no failures. A crash or forced kill
is not verified cleanup. Never use broad Docker prune to recover a fixture.

The positive path must include actual notebook code selection, signed Belay job
completion using the stub, explicit saved-file preview/confirmation, a matching
repository receipt, byte-identical file reopening and receipt persistence after
Cairn restart. Controls require expired and disabled help to add no job/attempt,
preserve code, and allow no-AI submission. Existing submission controls also
exercise peer denial, origin denial, unsafe paths, conflicting idempotency and a
lost confirmation response. Keyboard, axe and narrow-screen checks are regression
evidence, not accessibility certification or human usability acceptance.

Run component checks separately:

```sh
.venv/bin/python -m unittest discover -s course-workspace/tests -v
node --test course-workspace/tests/test_belay_snapshot.cjs
```

## Evidence and privacy

Every invocation owns a fresh private output directory. It contains synthetic
credentials, synthetic course databases, private raw logs and screenshots: do not
publish it wholesale. Keep original failed runs. Export only reviewed result
metadata, hashes and sanitized screenshots. No production or learner/IRB data is
an input to this evaluator. Source tests and synthetic success do not authorize
institutional login, a live model endpoint, cloud provisioning or a classroom run.
