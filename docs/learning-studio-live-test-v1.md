# Learning Studio live completion test

Greg explicitly requested “Run through the test yourself” after launching the live instructor window. This authorizes the bounded live check here; the earlier UI implementation scope did not itself authorize paid execution.

## Existing window and observed limitations

Preserve `output/nhanes-instructor-20261115T043359Z-62761` at source `751b87e1a29751566a0a42c23894b4732a1ff552`. Through the actual visible browser, the agent confirmed the prediction checkpoint, requested the prepared hint and observed its returned text. The model asked about SEQN coverage and bpxo_match rather than giving the supplied full solution. This is a narrow qualitative observation, not instructional acceptance.

The ordinary preflight workflow passed five stages, including native 192-row output, wrong CSV rejection, correct CSV, byte-exact four-file receipt, stale-preview refusal and restart recovery. The subsequent manual post-hint submission was **incomplete** when the original test window closed. The first clipboard paste left assistance.md empty; this was observed before selecting/submitting files and corrected by typing and saving a 202-byte ai_assisted declaration. This is retained as an interaction failure, not counted as a successful submission.

The closed window recorded three completed OpenRouter/OpenAI transports, USD0.0035544 reported usage, zero SDK retries, zero newly unknown liabilities and zero active/unresolved Portage reservations. Cleanup succeeded. Provider-internal retries remain unobservable; the historical USD0.025 unknown liability remains separate and unresolved.

## Finite completion scope

One fresh invocation of the existing automated NHANES browser journey is allowed, maximum 600 seconds, using the same Cairn/Belay-classroom/Portage/environment/image/task pins. A small committed qualification wrapper narrows the transport guard to four remaining requests and USD0.100 reserved before the browser can submit a hint. Combined with the three slots already used, the ceiling remains seven requests and USD0.175 reservation. No retry if new usage is unknown. No renewal of course credentials or deadline; no private/student inputs, model changes, cloud resources or production changes.

Use `course-workspace/rehearsal/studio_live_check.py` with the same NHANES arguments, `--tutor-mode live --execute-approved --execute-synthetic`, and no manual review window. The existing acceptance routine requests the live hint before notebook execution, writes the explicit ai_assisted declaration, saves and submits four files, verifies exact repository bytes and independent correctness, exercises refusal controls, restarts Cairn for receipt readback and closes owned resources. Preserve failures and raw accounting at `output/learning-studio-live-check-v1`.

No application source or component pin changes are part of this test. The observed short manual window is a usability limitation to report, not a reason to extend model authority.

## Verified completion

At tested source `d19cda90ab41d5306b78b47acd7b8e200238c685`, the fresh run passed all eight acceptance stages in 152.37 seconds. A live hint preceded the declared AI-assisted saved-file submission; the real kernel produced 192 rows, the wrong numeric output scored 0/4, correct output scored 4/4, the receipt matched all four saved files byte for byte, a changed preview was rejected, and the receipt survived a Cairn restart. Expired/disabled help caused no further attempts. Owned-resource cleanup succeeded.

The completion run used 3 actual provider transports, USD0.0018184 reported cost, zero SDK retries, zero new unknown usage, and no active/unresolved Portage reservations. Across the manual and completion windows: six transports, USD0.0053728 reported cost, within the combined seven-slot/USD0.175 reservation ceiling. Provider-internal retries remain unobservable. The historical USD0.025 liability remains unresolved and separate.

Result: `output/learning-studio-live-check-v1/result.json`; SHA-256 `a61911985fb2b259f4369f3b32b29dc746fb220b0ba04a84ee31af4ead51a177`. Raw outputs, screenshots, hint, source/environment pins and negative controls remain in the private output directory. Technical synthetic live workflow passes; human instructional/student acceptance and deployment are not claimed. Manual UX finding: the finite instructor window is too short for an unhurried review and shuts the workspace down when help access ends. A future refinement should keep model expiry separate from a bounded save/submit grace period.
