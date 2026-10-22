# CW1: INFO511 synthetic classroom demonstration

**Source candidate only.** No new runtime window was used for this revision.
Root's revised v2 package passed 13 controls after fixing permissions only in the
isolated test copy; the earlier import failure is retained. The new opt-in
unattended harness below is not covered by that result and has not run.
The assigned `classroom-integration-handoff.txt` remains the change record.

## Selected source and scope

Waypoint base: `256f83480113bd4f5ad669df5b72e6c974d8db43`, isolated branch
`codex/cw1-info511-classroom`. Cairn base:
`089911ece3783037d4f96ff0c9ca37e6907da320`, isolated branch
`codex/cw1-csv-evidence`. Both are uncommitted source overlays. The selected
Cairn baseline matches Waypoint's existing course-rehearsal pin. Local newer
Cairn main `912b9e1d035a83fa801096748627ad006dbba1d4` has identical assessment
and handoff sources; unrelated server additions are not pulled into this slice.
After review/commit authorization, update the specific candidate pin/build;
the unpatched old binary cannot qualify CSV extraction.

The learner task is `info511-lab3-q4-synthetic-v1`, policy `info511-hints-v1`.
The input is B1's exact instructor-created 3-country x 3-year fixture with
3 missing cells. It is not the original 44-country dataset. Original CSV rights
remain unresolved and no original bytes are distributed. Use the same rubric
and explicit assistance declaration for both routes; `no_ai` is valid.

Reuse the existing CPU course Dockerfile and hashed Python dependencies, Cairn
assignment/roster/pinned-policy APIs, separate sign-ins, saved-file selection,
preview/confirmation and immutable receipts. No new transport, identity store,
score service, automatic submission, private-monitoring feed or provider exists
here. The course image's executed ID is still null: source pins do not prove a
built image or apt package identity. J2b/J2c proceeds in a separate chat; this
package does not use the paused research helper/science graph.

## Distribution and instructor attempts

Upload only `learner/` to each synthetic learner's fresh notebook directory.
Never distribute `operator/` or its complete notebooks to learners or tutor
retrieval. Learner cells retain placeholders and no assessed pipeline.

Operator-only attempts:

- `attempts/alice`: complete authored correct notebook with independent scalar
  source-cell checks, input-preservation assertion, duplicate/row/missingness
  checks and explicit CSV export. No notebook cell has executed output.
- `attempts/bob-missingness-remediation`: deliberately drops missing rows,
  exports six rows, then contains a separately labeled repair cell. Pause before
  repair for the B1 `row_count` failure; preserve failed bytes, run repair, and
  rerun the same checker. This is an academic error, not misconduct.
- `attempts/bob`: complete corrected end-state notebook and authored CSV for
  transfer preparation. Do not claim these prepared CSVs came from notebook
  execution until the notebook actually runs and the output is compared.

These are instructor-created examples, not student or model output. Both
assistance declarations truthfully say `no_ai`. Keep all cells unexecuted in
source control. For a later admitted notebook window, explicitly upload the
operator attempt plus synthetic data to a separate instructor-controlled CPU
notebook directory, run cells in order and retain the actual executed copy
privately. Do not execute an arbitrary learner notebook on the host.

## Deterministic checker and pinned grading policy

`operator/checker-binding.json` pins the unchanged `check_tidy` source in
`EduCloud-Ecosystem/belay-classroom`, revision
`f53b8f19e0e445d9a0caedc734c048c383664134`. Use that fork for all future tutor
and checker inputs; original B1/Belay is frozen. The existing `--belay-source`
option name remains compatible. The demo
stages its exact bytes from `--belay-source`; it refuses drift. No second checker
or independent claim of B1 acceptance is introduced. Preserve its AGPL-3.0-only
header when staged in the synthetic instructor template.

`operator/grade_csv.py` is a small wrapper used in Cairn's existing restricted
container. It reads only the bounded submitted CSV plus pinned instructor source,
loads the exact B1 checker, and reports its fixed messages. It never executes
the submitted notebook, scores narrative or evaluates spreadsheet formulas.
The demo creates an actual instructor-pinned `grading.json` using the provided
immutable CPU image, network none, 20-second step, 256 MiB and one CPU. A
meaningful cell-preservation check awards **correctness only: 0 or 4 out of 4**.
This is an intermediate check, not the final ten-point grade. The remaining
criteria and final publication require instructor review. No pass-all grader
or toy `answer()==5` policy is used.

The template contains only instructions, synthetic input, this wrapper and the
unchanged B1 checker/binding. It contains no complete notebook solution. The
future run uploads these files to the existing disposable Forgejo template,
reads its actual commit, creates the assignment and pins `template_commit`
through `PATCH /assignments/{id}/grading-policy`. The published revision and
assignment ID are recorded from responses, not invented in source files.
Reference JSON files are examples only; no null template ref is sent by the demo.

## CSV evidence and instructor approval

The Cairn patch enables only instructor-selected `.csv` rubric paths. It validates
UTF-8/CSV quoting and retains original bytes, SHA-256 and physical `line:N`
segments as `csv-source-v1`. Multiline quoted fields remain physical source
lines; formulas are inert text. Scientific errors and variable row widths stay
visible for feedback. Blank CSV, malformed quoting, unsafe paths, symlinks,
encoding errors or existing byte/line limits still block assessment.

All four rubric artifacts remain required: notebook, CSV, explanation and
assistance declaration. No `Artifact.Issue` is ignored. The prior proposal to
inspect CSV outside the capture does **not** complete the existing review path
and is superseded by this source extension. The extension is not yet tested or
accepted. Existing original-byte, citation, input-digest, review, stale-revision,
role and roster gates remain unchanged.

The demo captures the same immutable revision as the authenticated receipt and
compares every artifact digest/size. It saves a private capture and an unscored
operator judgment draft. An actual operator inspects the source, fills points,
feedback, uncertainty and exact citations, then explicitly requests import.
The proposal uses `source: instructor-import`, model `none-human-instructor`,
and the actual input digest. A second explicit `approve <assessment-id>` action
publishes through the existing review API. No automatic/default scores or
fabricated model evidence are supplied. The operator may edit/reject rather than
approve; stopping leaves the run incomplete, never a success report.

## Future admitted launch

### Explicit unattended engineering fixture

Use `--device-only` to omit the Hub/workspace mapping, and `--reviewer-fixture`
to select authored automated review inputs. Both are opt-in. Without the reviewer
fixture, the original interactive operator review still requires a terminal;
without device-only, supply the original `--workspace` argument.

`operator/reviewer-fixture.json` binds both packaged attempts' four exact saved
file hashes and exact captured citation text. It supplies a deliberately distinct
5-point proposal and 6-point review as API transition test inputs, never as human
academic judgments. The actual separate CSV checker must still pass 4/4 before
review. Import must leave grade history unchanged; a separate authenticated
approval request must publish exactly one matching persisted grade. The result
states `review_actor_kind: automated-synthetic-fixture`,
`human_review_performed: false`, and `assessment_quality_evaluated: false`.
No learner, model or monitoring signal generates these test scores.

The command below uses the packaged instructor-authored saved files to exercise
real submission and review mechanics. It does not claim those saved notebook
files were produced by a Jupyter kernel. If root chooses different actual saved
bytes, explicitly author/review a new fixture manifest with their real hashes and
citations; do not regenerate expectations silently or relax the checks.

From the Waypoint source root, after root has prepared the named paths/images and
granted a finite window, this is one unattended command:

```sh
python3 -B course-workspace/rehearsal/info511_demo.py \
  --execute-synthetic --device-only \
  --cairn-binary "${CW1_CAIRN_BINARY:?reviewed candidate binary}" \
  --cairn-source "${CW1_CAIRN_SOURCE:?matching candidate source and web assets}" \
  --belay-source "${CW1_BELAY_CLASSROOM_SOURCE:?belay-classroom checkout}" \
  --image "${CW1_CORRECTNESS_IMAGE:?immutable local sha256 image ID}" \
  --forgejo-image "${CW1_FORGEJO_IMAGE:?existing local ID or unique fixture tag}" \
  --checkout-image "${CW1_CHECKOUT_IMAGE:?existing local ID or unique fixture tag}" \
  --alice-saved course-workspace/classroom/info511-lab03-q4-v1/operator/attempts/alice \
  --bob-saved course-workspace/classroom/info511-lab03-q4-v1/operator/attempts/bob \
  --reviewer-fixture course-workspace/classroom/info511-lab03-q4-v1/operator/reviewer-fixture.json \
  --output "${CW1_NEW_OUTPUT:?new private output directory}"
```

Selectors only inspect existing images and resolve to immutable IDs. The harness
does not build, pull, retag or overwrite shared tags. Course's original default
image selectors remain unchanged for existing callers. Synthetic grade export
is named `automated-synthetic-reviewed-grades.csv`; captured originals and real
receipts retain their exact provenance. Human UI review is a separate later gate.

The six authored `test_info511_review_fixture.py` controls are NOT RUN. They cover
explicit labels, original/receipt/fixture drift, incomplete/stale evidence,
incorrect citations, invalid point inputs and wrong actor/task/rubric. They are
offline refusal tests, not connected API or human-review acceptance.

### Interactive instructor walkthrough

**Do not run now.** First obtain fresh headroom and a serial runtime window from
the classroom coordinator. Use existing local images, prepared binaries/browser
and approved synthetic workspace. Do not install, build, pull, provision or
start services to bypass a missing prerequisite. Read
[the existing runbook](../../rehearsal/RUNBOOK.md) for prerequisites and ownership.
Its toy whole-course command is a regression, not this demonstration.

1. Review immutable source hashes and build the changed Cairn candidate only
   in a separately admitted build window. Retain the binary SHA/build evidence.
   The demo checks three source hashes but that is not binary attestation.
2. Execute the authored notebooks in the approved CPU notebook environment in
   a separate admitted window; establish the wrong-output then repaired B1
   checker results. Keep failed and corrected source/output copies separately.
   Use the actually saved four files for the demo arguments below.
3. Run the following only with an interactive instructor and fresh finite outer
   supervision. The program itself does not grant runtime permission:

```sh
python3 -B course-workspace/rehearsal/info511_demo.py \
  --cairn-binary /absolute/path/to/reviewed-cairn-binary \
  --cairn-source /absolute/path/to/cairn-cw1-csv-evidence \
  --belay-source /Users/gregchism/dev/belay-classroom \
  --image sha256:EXACT_EXISTING_CPU_IMAGE_ID \
  --workspace https://EXISTING_APPROVED_SYNTHETIC_WORKSPACE \
  --alice-saved /private/actually-executed-alice \
  --bob-saved /private/actually-executed-bob-corrected \
  --output /private/NEW_EMPTY_PARENT/cw1-run \
  --execute-synthetic
```

The synthetic fixture creates only its owned local Forgejo/Cairn resources using
the existing Course implementation, and refuses missing local images before
startup. It makes no image-pull request. The classroom is roster-restricted;
Alice and Bob sign in independently. The local demo uses explicit device upload
of saved notebook files to the existing Cairn page. This does not establish the
Jupyter popup bridge or actual Hub runtime in the same invocation; use the
existing full course/browser controls for that separate acceptance leg.

Prepare/receive/preview are never successful submission. The explicit Confirm
flow must return one committed receipt with all four saved-file hashes and the
same submission ID, including original-response loss/recovery. Peer access,
wrong origin, changed idempotency payload and unsafe path probes are retained.
Use the existing authenticated readback, never a locally manufactured receipt.

After actual instructor review, export `grades.csv` through the existing
operator API, compare both scores/maxima to the approved results, reopen the
private file and hash it. The runner also exports only the four captured
originals, verifies their receipt hashes and reopens them byte-for-byte. The
operator solution tree, review notes and private events are not part of that
work export. Download does not call `confirm-export` or start its
retention clock. No LMS passback. The script writes `CW1-OBSERVATIONS.json` only
after its checks and existing owned cleanup succeed; it is not `PASS.json` or
full-platform/model/notebook acceptance. Failure leaves private diagnostics and
requires operator investigation; no automatic retries or cleanup of unrelated
resources. Human review time must fit Greg's shared four-hour weekly ceiling.

## Acceptance matrix

| Evidence layer | Current state | Required later evidence |
|---|---|---|
| Initial package fixtures | 13 PASS, original source only | Rerun affected controls for new fixtures |
| Cairn CSV extractor/service | Source + four test functions authored; NOT RUN | Quoting, encoding, limits, symlink, original/citation tampering, all-four capture/import/review, stale refusal |
| Notebook and checker | Authored correct/fault/repair; NOT RUN | Actual CPU execution, initial failure preserved, repair verified with unchanged B1 checker |
| Application/API/browser | Demo source prepared; NOT RUN | Two independent sessions, explicit confirmation/receipts, actual source capture, instructor review and matching export |
| Full notebook bridge/accessibility | NOT RUN | Jupyter selection/popup, keyboard/focus/errors, screen reader, 200% zoom, peer notebook denial/revocation |
| Optional AI/monitoring | Disabled, B1/P1 source dependencies | Trusted policy, attempt accounting, covered route, actual bounded suitability and permission/pause/revoke |
| Real students/production | NOT AUTHORIZED | Offering policy, rights, complete data path, covered host, support and separate approval |

No local syntactic/source check substitutes for any row above. P1 still identifies
trusted consumer transport, single qualified route, finite attempt authority and
unknown-outcome gaps. No API credentials, entitlement, paid fallback or inferred
coverage enter this package. The non-AI route remains usable independently.

For root's existing-host execution plan, read
[the CPU-only recipe](operator/OFFHOST-CPU.md). It separates the smallest
standard-library check, offline Go controls, notebook execution and actual Docker
integration. The 4 GiB host is a reported inventory, not an execution grant.

## Deferred commands and retirement

```sh
# Each needs fresh serial admission; all new/revised checks are NOT RUN.
python3 -B -m unittest discover -s course-workspace/tests -p 'test_info511_package.py' -v
python3 -B -m unittest discover -s course-workspace/tests -p 'test_submission_bridge.py' -v
python3 -B -m unittest discover -s course-workspace/tests -p 'test_submission_bridge_server.py' -v
# In the selected Cairn checkout with its existing Go 1.26.9 toolchain/deps:
go test ./internal/assessment -run '^TestCSV' -count=1
gofmt -l internal/assessment/extract.go internal/assessment/csv_test.go
```

Formatting, focused Go tests and required whole-repo build/vet/race/database/UI
checks remain due; no gates are waived. Full CI and real demo execution require
separate source/binary review and explicit admission. No commits or CI runs here.

After any later notebook rehearsal, follow
[course retirement](../../COURSE-RETIREMENT.md): stop owned writers, revoke
synthetic access and verify old sessions denied, hide the Cairn binding,
preview/apply retirement and export/verify retained coursework. The local demo's
owned cleanup is not institutional retirement or independent physical deletion.
Retention owner/date, encrypted backup expiry, no-resurrection restoration,
second-operator reproduction and phone usability remain gates. Keep private
captures, review files, credentials and raw logs out of ordinary exports.
