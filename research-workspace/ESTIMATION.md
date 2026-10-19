# Deterministic synthetic estimation

`assessment-estimate` calculates a provisional estimate from already retained,
explicitly synthetic observation metadata. It validates every receipt through
Outfitter's pure command and exposes the selected inputs, exclusions, exact
coefficients, held-out errors and limits. It does not run an experiment, accept
an upload, create a workspace or infer that measurements or cleanup are trusted.

This implements the initial calculation slice of the
[canonical contract](https://github.com/EduCloud-Ecosystem/educloud/blob/1184ab2c2a0c82aa437b2d146f22dbae2b98efa7/docs/specs/research-assessment.md#deterministic-estimation-and-comparison).
The existing benchmark-manifest and receipt interfaces remain unchanged.
Use a separately built Outfitter executable from merged revision
`de65bdab9303395226ebfb35e0e0740ab19b4d8e` for the pure validator. Existing
execution broker/image/profile defaults are unchanged.

## Run the synthetic example

From `research-workspace`:

```sh
python3 -m research_workspace assessment-estimate \
  assessment/fixtures/estimation/scenario.json \
  --series assessment/fixtures/estimation/python-series.json \
  --series assessment/fixtures/estimation/r-series.json \
  --outfitter /absolute/path/to/separate/outfitter
```

One series is sufficient. A second series adds a comparison using the same
scenario; the command selects no winner and makes no resource recommendation.
It writes canonical JSON to stdout only after all inputs pass validation and
calculation. It creates no files, state, Store, worker, ledger or study export.
An operator may deliberately redirect the resulting content-free draft.

The example counters are invented by
`assessment/fixtures/generate_estimation_fixtures.py`; they are not timing,
memory or cost observations from Python, R, a Mac or a target resource. For the
first series, CPU nanoseconds equal `1000 + 50 * rows` plus symmetric repeat
variation. Execution duration equals `500 + 20 * rows` with separate variation.
The second series multiplies those invented counters by two. Neither series
is evidence that one language or runtime is faster.

At 25 rows and six proposed executions, the first series predicts 13,500 CPU
nanoseconds of modeled work. Its explicitly declared additional shared setup
adds 100 nanoseconds once, giving 13,600. Serial busy duration is separately
6,000 plus 200 nanoseconds. These deliberately tiny numbers verify arithmetic;
they are not resource forecasts.

## Closed inputs and bounds

`waypoint.assessment-series/v1` contains an opaque `series_id`, an existing
`educloud.benchmark-manifest/v1` and at most twelve existing receipt pairs.
Only synthetic-classified manifests are accepted. At most two series from the
same project may be supplied. Duplicate attempt or cleanup-receipt identifiers
are refused. Every pair must match a unique declared manifest slot; a partial
series never implies complete reservations or sample coverage.

`waypoint.assessment-scenario/v1` contains an opaque ID, target rows, stage
execution/experiment/repetition counts, retries per execution, setup/transfer/
idle assumptions and declared held-out model-error tolerances. These tolerances
are distinct from the recipe's numerical-result tolerances. They are declared
assumptions, not authenticated pre-registration or scientific acceptance.

Each overhead explicitly declares `once` or `per_attempt` and the basis
`additional_outside_measured_execution`. A null value means unknown, while zero
means a declared zero assumption. Only additional work outside the modeled
execution belongs here; the tool cannot independently verify that assumption.
Shared setup is counted once when that policy is selected. It is never silently
multiplied by experiment count or silently assumed to be zero.

Scenario files retain the 64 KiB assessment file limit. Each series file is a
bounded regular file up to 256 KiB; the combined maximum is two series plus one
scenario. All existing strict JSON, duplicate-key, ASCII, depth, numeric and
file restrictions apply. There are at most 24 pure validator calls, with a
30-second aggregate acceptance budget, per-call five-second cap, and up to
five seconds of teardown after a failed call. There are no retries or fallback
providers. Output is capped at 256 KiB.

Target rows are bounded to one billion; each stage/experiment/repetition count
to one million; retries to ten per execution. Derived attempt and instructional
storage counts must fit `2^53 - 1`. Exact rational numerators and denominators
are bounded to 256 bits. Overflow is refused instead of truncating or wrapping.
All reasons, methods, scopes and source labels use closed identifiers. Arbitrary
text, filenames, raw data, tool logs and provider conversion rules are excluded.

## Models, held-out checks and uncertainty

The first method is `affine_size_means_ols_v1`, for the existing `analysis` stage.
It fits CPU-consumed nanoseconds with `process` scope, and execution-duration
nanoseconds with `whole_attempt` scope, separately. Other scopes cannot be
silently added to extra overhead assumptions. Mixed fitting scopes make that
metric unavailable; incompatible held-out scopes are excluded from evaluation.

An eligible observation declares `synthetic_fixture`, successful admitted and
started execution, no cancellation, `reused: false`, a passed quality claim and
an available metric. These remain untrusted synthetic declarations. Failed,
OOM, unknown/reused, unknown-quality and unavailable observations stay in the
output with exclusion reasons. Conservative reservation charges never enter a
model. Cleanup liabilities survive calculation regardless of fitting success.

For each fitting size, take the exact mean of its eligible repetitions. Fit
`y = intercept + slope * rows` using equal weight per size. The selected receipt
digests, size means, coefficient units and exact numerator/denominator pairs
are retained. There is no automatic model selection, outlier deletion, floating
point fitting or silent negative-value clipping. Fewer than two valid fitting
sizes, a negative slope or a negative prediction prevents that estimate.

Held-out observations never affect coefficients. Each reports absolute and
relative error against the fixed model. A zero observed value has unavailable
relative error; the absolute tolerance still applies. An incomplete held-out
set cannot receive `within_declared_tolerance`, even when its remaining values
agree. The combined limit is the larger of the declared absolute tolerance and
the declared relative tolerance times the observed value. Mean absolute error
is reported separately.

The default coverage is three fitting sizes with three repetitions each and
three held-out repetitions. Missing coverage remains insufficient. An explicitly
approved smaller design can have a mathematical fit but remains provisional.
Targets outside the fitting or observed range carry distinct warnings. A target
at or above an observed OOM size has no prediction; that boundary is retained.

Repeat count, min/median/max and range describe observed variability only.
Confidence intervals and prediction uncertainty are not estimated. The explicit
scenario controls assumption sensitivity; changing it does not turn repeat
spread into an uncertainty interval. Every output remains provisional and has
`readiness_pass: false`, even when the synthetic held-out check agrees.

## Totals, capacity and comparison

Demand combines the modeled value with explicit execution, experiment,
repetition and retry counts. Research counts apply to the whole proposal;
instructional counts apply per assignment per enrolled student. Model work and
additional overhead are shown separately. Any unknown required overhead makes
the total unknown while preserving the known modeled portion.

CPU demand and serial busy duration remain distinct; elapsed completion time is
unknown. Conversion to their respective hours divides exact nanoseconds by
3,600,000,000,000. Coefficients and totals retain exact fractions; a separately
labeled ceiling in base units supplies the conservative rounded demand. There
are no conversions to GPU-hours, node-hours, service units, credits or dollars.

Memory, scratch and output peaks are observed byte envelopes, never multiplied
by runtime. Target capacity remains unknown. Instructional persistent-storage
assumptions use enrollment times per-student storage, while simultaneous and
deadline-peak sessions remain separate fields. Concurrent working memory and
storage stay unknown because concurrency was not observed. Missing setup,
transfer and idle measurements remain visible even when synthetic overhead
assumptions are supplied.

Two-series comparison preserves each software, hardware, metric-scope and source
identity. Differences require compatible units, scopes, pathway and instructional
assumptions, and known totals; incompatible/unavailable dimensions remain in
the result. Different hardware still supplies only synthetic comparative
assumptions, never actual target qualification. Every actual-resource, execution,
upload, cleanup-trust and retention-finalization gate remains false.

## Verification

`tests/test_assessment_estimation.py` covers known coefficients and exact rounding,
held-out isolation, failed-held-out non-promotion, incomplete/smaller designs,
zero versus missing, OOM/extrapolation limits, scope conflicts, unknown overhead,
overflow, deterministic input ordering, instructional dimensions, comparison,
bounded actual CLI transport and no new state. Set `ASSESSMENT_OUTFITTER` to the
pinned pure validator binary to run the real cross-component CLI check.

No test here qualifies resource measurements, independent-human reproduction,
source candidates, hosted identity, temporary-upload cleanup or private/student
data. The selected operator/machine and Greg Chism's later promotion review
remain separate gates; no existing allowance is renewed by this calculation.
