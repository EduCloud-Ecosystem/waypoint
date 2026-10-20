# Disabled streaming summary candidates

This is a separate, source-only scientific component for the larger assessment
profile. It consumes **one exact sample**, produces a bounded ephemeral summary,
and requires a separately authored checker. It does not admit an upload or a
compute attempt. The ordinary Python/R recipes, environment catalog, study
Worker, AS1, old manifest validators, receipts and previews remain unchanged.

The independently authored checker is integrated and pinned to its actual
source bytes. Final isolated local verification passed 55 methods with no
skips, including actual Python, R 4.6.0 and independent checker commands.
Hosted CI remains pending. Runtime image qualification and capability
readiness are unavailable; every readiness flag is false.

## Identity and interface

The new input profile is `summary-fixed6-csv`, version 1. Manifest recipe
families remain `summary-python` and `summary-r`, as required by the accepted
v2 codec. The candidate environment IDs are
`summary-python-fixed6-stream-v1` and `summary-r-fixed6-stream-v1`.
Their actual packaged code hashes distinguish them from the old recipes.

The separate `assessment-environments.json` registry cannot enable ordinary
dispatch or production assessment. Its reader validates exact field sets,
types, source paths and hashes without importing or running a recipe. Unknown
runtime/image identities are null, never fabricated. The checker was authored
separately from this workload source, reviewed by the coordinator and integrated
from commit `1bc3f41fbae007bfda9498bf1957a984339efd7c`. Its source SHA-256 is
`d8e55a522ce5e0d268d724824c4f918374f613705b09e40bea5690d6f285f073`.

Both workload CLIs have exactly three positional arguments:

```text
INPUT CONDITION EXPECTED_ROWS
```

`INPUT` is a trusted caller's file path or `-` for binary standard input.
`CONDITION` is exactly `baseline` or `alternative`. `EXPECTED_ROWS` is a
canonical positive decimal integer from 1 through 1,000,000. The entire input
must contain exactly that many rows and reach EOF. An extra tail is an error.
Python uses isolated, no-bytecode mode; R uses `Rscript --vanilla`.

The checker uses the same three arguments, requires a trusted file path rather
than `-`, and receives bounded workload JSON on standard input. Future owned
mount/descriptor framing and argument derivation from a permanent slot require
their own reviewed integration. A file path supplied to these offline CLIs is
not proof of owned-file identity.

Each language has separate proposed capability mapping IDs for its fixed
condition, for example `cpu-summary-fixed6-python-baseline-v1` and
`cpu-summary-fixed6-python-alternative-v1`. These identifiers are candidate
metadata, not constructed or enabled runtime capabilities.

## Explicitly narrower input

The first line is exactly `group,value` followed by LF or CRLF. Every data
record, including the last one, also ends in LF or CRLF. Empty records, quoting,
escapes, extra columns, embedded line endings, BOM, NUL and non-ASCII bytes
are rejected. Only the following fields are accepted:

| Field or bound | Contract |
| --- | --- |
| Group | Exact `[A-Za-z0-9 _.-]{1,64}` bytes; no trimming or normalization |
| Numeric spelling | `[+-]?(?:0\|[1-9][0-9]{0,12})(?:\.[0-9]{1,6})?` |
| Numeric magnitude | At most 1,000,000,000,000, checked exactly |
| Decimal precision | At most six fractional digits |
| Numeric field | At most 21 bytes |
| Record | At most 88 bytes including CRLF |
| Groups | At most 128 distinct labels |
| Rows | 1 through 1,000,000 |
| Input bytes | At most 88,000,013 |
| Read block | At most 65,536 bytes |
| Partial record | At most 88 bytes |

The numeric grammar is equivalently an optional sign, a canonical integer
part, and an optional decimal point followed by one to six digits. It excludes
exponents, numeric whitespace, leading-zero padding, `.5`, `1.`, NaN, infinity,
hexadecimal, underscores and locale-dependent commas. Signed zero is allowed
as input and normalized in output. Groups such as `001`, `NA`, `NULL` and
labels containing spaces remain exact strings.

The byte ceiling is derived from 64 group bytes + 1 comma + 21 numeric bytes
+ 2 CRLF bytes, times one million rows, plus a 13-byte header. A LF record is
shorter. **This candidate does not support a 5 GB scientific input.** The
general v2 metadata ceiling remains 5,000,000,000 bytes, but a future mapping
for this candidate must refuse incompatible declared bounds rather than clamp
them. The old 64 KiB/1,000-row recipe grammar remains available on its old path.

## Exact arithmetic and bounded state

An input value is an exact decimal multiple of one millionth. Its signed
scaled coefficient `X = value × 1,000,000` has magnitude at most `10^18`.
Each group retains only its count, exact scaled sum, minimum and maximum.
The full input's scaled sum has magnitude at most `10^24`.

Baseline uses every value. For alternative, a group containing at least five
values removes **one occurrence** of the minimum and **one occurrence** of
the maximum, even when extrema are tied. Smaller groups remain unchanged.
Its used count is `n − 2` and its used sum is `S − minimum − maximum`.
The overall mean is weighted by the used value counts, not an average of
group means. No value list, value sorting, spill file or row-sized reservoir
is retained.

Python uses bounded integers. R never converts a complete 19-digit value
coefficient or 25-digit sum into a double. It uses signed-magnitude vectors
with five radix-1,000,000 limbs. Each limb is an exact double integer from
0 through 999,999; carry and borrow normalize after each operation.
Five limbs cover magnitudes below `10^30`, exceeding the `10^24` sum bound.
Each addition/carry intermediate is below `2,000,002`.

Means are strings with exactly 12 fractional digits, rounded to nearest with
ties to even. To calculate them, divide `abs(used_sum) × 1,000,000` by the
used count and round the integer quotient. R multiplies using a limb shift,
with six limbs covering the numerator through `10^30`. Long division keeps
the remainder below the count, so each intermediate is below `10^12`, safely
below `2^53`. Quotient/remainder ranges and the exact reconstruction are
checked; a failed invariant refuses output. No large floating-point sum or
floating-point mean formatter is used.

This representation has at most `5 × 10^-13` absolute mean rounding error.
It is stricter than the unchanged v2 `reviewed_summary` tolerances. The checker
must also verify the exact canonical rounded representation. Extreme
cancellation within the accepted decimal domain is handled exactly; malformed,
nonfinite, excessive-precision and out-of-domain inputs fail.

State grows with at most 128 groups, not with row count. R's newline-index
vector is bounded by one read block. Output is bounded before publication.
These logical bounds are not measured process RSS, interpreter memory,
throughput, a 32 MiB memory guarantee, or host storage capacity.

## Ephemeral result and checker

The workload emits one LF-terminated `educloud.summary-stream-result/v1`
object with exactly these fields:

```text
schema, input_profile, condition, input_rows, input_sum_scaled,
count, sum_scaled, mean, groups
```

Each ordered group object contains exactly:

```text
group, input_count, input_sum_scaled, min_scaled, max_scaled,
count, sum_scaled, mean
```

`input_*` fields describe original rows; `count`, `sum_scaled` and `mean`
describe values retained by the selected condition. Counts are canonical
positive JSON integers, never booleans or floating/exponent tokens. Scaled
values are canonical signed decimal strings: no plus, leading zeros or
negative zero. Means are fixed-12 decimal strings with no exponent or
negative zero. Groups are unique and sorted by unsigned ASCII bytes.

The independent checker must read the original sample with its own bounded
byte lexer, accumulate with Decimal anchored sums, and verify rounding with
an independent Fraction/interval oracle. It imports none of the workload
parser, accumulator or formatter. Self-consistency between result fields is
insufficient: every value must agree with the independently read sample.

The checker report contains only `schema`, `status` and a closed `reason`.
Completed malformed-input/result or mathematical mismatch is failed.
Unavailable input, internal errors or interrupted execution is unknown.
Neither an exit-zero process alone nor a self-reported result establishes a
passed quality claim.

| Output | Maximum bytes |
| --- | ---: |
| Workload stdout | 61,440 |
| Workload stderr | 256 |
| Checker stdout | 1,024 |
| Checker stderr | 256 |
| Combined two phases | 62,976 |
| Required manifest output partition | 65,536 |

A conservative maximum-field-width calculation gives 40,496 bytes for a
workload result. Some simultaneous field maxima are scientifically impossible;
this is a serialization bound, not a claimed executed dataset. Verification
must also include a valid large result and actual bounded CLI collection.

Errors contain only fixed codes, never input excerpts, labels, filenames,
tracebacks, interpreter diagnostics or receipt text. The outer collector must
count and discard stderr even when an interpreter fails before the script can
handle an error. A partial response or failed write is not successful output.

Raw summaries and input remain ephemeral. None of these values, labels, raw
output, diagnostics or their content hashes belongs in durable measurement
receipts, study exports, A4 estimates or A5 packets. Durable quality remains
an unverified checker claim with no authorization or cleanup trust.

## Verification and mandatory follow-through

The literal fixtures describe synthetic data. Controls are designed to cover
fragmented reads, EOF, exact sample size, parser and group bounds, extrema
ties, cancellation, carry/borrow, half-even rounding, output mutations, source
pinning and actual Python/R/checker processes. Counter-injection controls for
large scalar limits must be labeled logical boundary checks, not million-row
runtime evidence.

R acceptance requires actual R execution against the independent checker.
An absent or unusable local R interpreter is explicitly NOT RUN/SKIP, never
replaced by Python results or inferred from source. No local dependency
installation or image build is part of this component.

The existing hosted controls job installs only the distro binary
`r-base-core` package with `--no-install-recommends`, then requires a usable
`Rscript` before complete test discovery. Noninteractive package operations
have finite timeouts and one acquisition retry, within the unchanged
ten-minute job limit. The artifact records the distro, apt candidate and
installed dpkg version, followed by the interpreter frontend hash, version
command and bounded result. This is package-version provenance, not immutable
runtime attestation. Installation or interpreter failure fails the job;
skipped R is never accepted. Docker/helper pins, optional PDF checks and
source-build gates are unchanged.

The first integrated local run at `e6dcc5a` ran 25 methods: 24 passed and one
failed because the negative fixture appended LF to a trailing CR, making a
valid CRLF record ending. Its log is retained unchanged with SHA-256
`af0632004dfe871e95822f81c03b64f96af8fc56eb423ec4877dc51d94b4be40`.
This was a test-fixture error, not a workload defect. The test-only correction
at `db63306` added explicit valid-CRLF/invalid-internal-CR CLI controls and
both-language independent checks for cancellation, rounding and large forms.
All 28 methods passed with no skips; log SHA-256
`384812dd43a2d18b531d6e8091d3b3d9b2dafb193f2a62295c452c41660303df`.
Both runs preserved complete unchanged source inventories and used small
synthetic inputs. Their parent unittest process used the recorded minimal
environment and `PYTHONPATH=tests`, not isolated mode; Python workload/checker
children used `-I -B` and R children used `--vanilla`. The two earlier runs
must not be relabeled.

The final combined selection at `bd4848453e60ee01e6b894d34e907bb5df5149c5`
passed 55 methods with no skips in 6.948 seconds. Its parent used
`-I -B -W error::ResourceWarning` and an explicit trusted-root bootstrap;
complete before/after source inventories were identical and the bounded
process completed and was reaped. Log SHA-256:
`8f45d8346940bcb5d59d3923ccfdac3b0b2cbb954062aaa1703de47c26587537`.
These 55 methods include the independently authored checker controls and
overlap the earlier selections; counts must not be added across runs.
The [verification record](evidence/2026-10-19/streaming-summary/verification.json)
binds sources, raw logs, actual commands, interpreter frontends and the
protected full inventories. Native frontend identities do not qualify a
container image, the full transitive runtime or a deployment host.

The first hosted controls attempt, run `39370145454` at `f03cc67`, failed
with `RSCRIPT_UNAVAILABLE`; no candidate tests or helper build ran in that
job. Its original failed job log and source binding are retained in the
[CI dependency correction record](evidence/2026-10-19/streaming-summary/ci-dependency-repair.json).
The subsequent change supplies the distro dependency inside the existing
hosted job. Workload, checker, catalog and test bytes remain those of the
final local 55-method pass; hosted success remains pending.

The first candidate performs no sampling. Before multi-size execution,
an immutable selection/input-view policy must bind the actual sample and
seed semantics. A deterministic prefix policy is convenience sampling with
order bias; it cannot claim random or stratified sampling or pretend to use a
seed. A genuinely seeded policy requires separate exact algorithm review and
bounded selection overhead. Never pass a larger file and silently ignore
its tail.

Required subsequent work remains the same-authority two-phase execution path,
current authorization and permanent slot binding, separately measured
workload/checker/sampler overhead, actual aggregate writer/storage enforcement,
versioned A4/A5 integration, independently observed cleanup and qualified
researcher/instructor pilot journeys. No host, allowance, private-data approval
or operational readiness follows from this source component.
