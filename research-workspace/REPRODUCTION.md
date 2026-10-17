# Bounded evidence repeatability

This tool checks whether selected results in a retained evidence bundle can be
computed again using the exact reviewed recipe and immutable image. It is
**automated repeatability**, not independent human reproduction, a clean image
rebuild, or evidence of scientific validity. It uses local Docker through
Outfitter; no cloud resources, image pulls, dependency installation, or paid
services are involved.

## Establish the source before planning

Obtain the expected whole-archive SHA-256 from a separately retained trusted
record. Internal ZIP checksums detect inconsistent bytes but cannot authenticate
an archive that has been replaced wholesale. Computing a hash of an unfamiliar
archive and immediately supplying that hash does not establish its origin.

From `research-workspace/`, inspect the plan first:

```sh
python3 -m research_workspace plan-reproduction evidence.zip \
  --trusted-sha256 EXPECTED_SHA256
```

Planning verifies the external hash, strict archive format, membership, checksums,
and input/result lineage. It then matches every run against the packaged
`environments.json` catalog and validates its input. It does not start a process,
inspect Docker, write files, or execute archive code. A plan's `executable` field
means that selected records match the trusted catalog and the maximum allowance;
local image availability is checked only when execution is requested.

By default, only original `succeeded` runs are selected. Every original record,
failure, cleanup/accounting assertion, and evidence gap remains in the plan and
report. Repeat `--run-id ID` to choose specific records, including an intended
failed control. Unknown IDs and duplicate selections are refused. Selecting an
original failure does not erase that failure or establish a successful comparison
when the original has no successful result.

## Execute a fresh, finite attempt

```sh
python3 -m research_workspace reproduce evidence.zip \
  --trusted-sha256 EXPECTED_SHA256 \
  --output .runtime/reproduction-attempt-1 \
  --broker /absolute/path/to/outfitter \
  --budget-cpu-seconds 60
```

The operator must supply a whole, finite budget from 1 to 300 CPU-seconds. Each
selected run requires a full 30 CPU-second ceiling up front, so a 60 CPU-second
budget permits two selected runs and the maximum permits ten. A smaller budget
does not dispatch a partial selection. Each run also has one CPU, 256 MiB memory,
64 processes, a 30-second execution limit, and 64 KiB output limit. Outfitter
supplies read-only recipe/input mounts, disabled networking, and its reviewed
local containment and supervised-watchdog behavior.

Every selected recipe **and** immutable image must exactly match a packaged
trusted entry. Packaged source bytes must equal the archived bytes and their
declared hash; only those checked packaged bytes are staged for execution. The
archived original R recipe remains a separate historical catalog entry and is
not silently replaced by corrected R. Historical recipe limitations may produce
failed checks, which are retained. Every selected input and locally available
image is checked before fresh broker authority is initialized. Images are never
pulled automatically.

Execution always requires a new output directory, fresh project and run IDs,
and fresh private broker authority. Existing directories, including empty ones,
files, and symlinks are refused. No archived budget, grant, reservation, session,
or credentials are imported. An original closed project can be replayed as a new
operator-authorized local attempt; its old authority is not reopened.

## Read and retain the artifacts

The output directory contains:

- `report.json`: public structured outcomes for every original record. Each row
  reports `PASS`, `FAIL`, or `NOT RUN`, selection, original status and gaps,
  new ID, hashes, metric comparison, verified cleanup and accounting.
- `evidence.zip`: a newly verified-format evidence bundle containing exact inputs,
  packaged recipe bytes, all selected new run records, and independently accepted
  results. It includes selected work that was not dispatched after an uncertain
  operation, so gaps remain explicit.
- `state.private/`: private broker authority, requests, bounded raw diagnostics,
  and receipts. Its permissions are private and a generated `.gitignore` excludes
  it. Do not share this directory as public evidence or delete its authority while
  work remains unresolved.

The API/CLI return includes the exact `report.json` object and SHA-256 values for
both public artifacts. The report also binds the new evidence archive's hash;
the report hash is outside the report itself to avoid a recursive self-hash.
Retain these hashes separately before transferring the artifacts.

The report records the original source bundle hash and maps original run IDs to
fresh IDs. The new bundle's project question records that source hash and its
success criterion records the bounded run mapping, preserving origin lineage
without extending the existing strict evidence format. Reproduced result bytes
may serialize numbers differently, so file hashes can differ while metrics
match. Counts, group names, conditions and structure must match; numeric means
use relative tolerance `1e-12` and absolute tolerance `1e-9`.

`PASS` requires every selected attempt to have a complete valid broker receipt,
verified cleanup, settled accounting, an independently validated output, and a
matching original successful result. If a new independently valid result differs
from the original, the new run can be `succeeded` in its evidence bundle while
the repeatability comparison is `FAIL`. A selected original control with no
successful reference cannot produce a repeatability `PASS`. An empty selection
produces an honest `NOT RUN` report and an empty new bundle.

## Interrupted or uncertain work

Before each dispatch, an atomic, synced checkpoint records an attempted run with
unknown amounts, unresolved liability, and unverified cleanup. The local observer
has a 70-second response deadline and bounds broker response memory. An invalid
receipt, process failure, output overflow, interruption, or deadline failure
retains uncertainty and stops later selected runs. There are no automatic retries.
An accepted receipt with a known reservation preserves that reservation; absent
authoritative amounts remain null rather than becoming a refund or invented zero.

Keep `state.private/` and reconcile its broker request IDs before intentionally
starting replacement work. The directory is not an automatic resume mechanism.
The detached watchdog remains responsible for the original bounded execution;
host, watchdog, or Docker daemon loss can prevent cleanup. A failed observer is
not proof that provider work ended. A crash between replacing `evidence.zip` and
`report.json` can leave a detectable hash mismatch; such a pair is incomplete and
must not be presented as a finished report.

## Python interface

```python
plan_reproduction(payload: bytes, trusted_sha256: str,
                  selected_run_ids: list[str] | None = None) -> dict

reproduce_bundle(bundle_path, *, trusted_sha256, output, broker,
                 budget_cpu_seconds, selected_run_ids=None) -> dict
```

The plan includes `format`, `mode`, `source`, `selection`, `limits`, `executable`,
`local_environment_preflight`, `runs`, and `warning`. Execution returns
`{"report": ..., "artifacts": {"report": {"name": ..., "sha256": ...},
"evidence": {"name": ..., "sha256": ...}}}`. The report adds overall `status`,
`phase`, fresh project identity, supplied budget, timestamps, and evidence hash.
Preflight rejection raises `ValueError` before provider effects; local operating
system failures can raise `OSError`. Once authority is prepared, observed broker
failures are retained as outcomes rather than silently retried.

## Verification evidence

`tests/test_reproduction.py` covers tampering, arbitrary code and image rejection,
changed packaged bytes, invalid inputs, finite budget admission, all-environment
preflight, output collisions, named-pipe refusal, fresh identity and authority,
pre-dispatch checkpoints, failed and malformed receipts, known/unknown liability,
independent result failure, differing originals, missing references, retained
failures, and bounded local process observation. Provider controls are simulated;
the process reader controls use real local subprocesses.

The retained [Docker verification](evidence/2026-10-17/reproduction/verification.json)
records a separate actual local replay of the committed browser fixture: Python
v1 and archived R v1 passed with independently checked matching metrics, verified
cleanup, fresh broker usage of 60 charged CPU-seconds, and independently observed
absence of both owned containers. The original failed R record remains visible
and unselected. [The report](evidence/2026-10-17/reproduction/report.json) and
[new evidence bundle](evidence/2026-10-17/reproduction/evidence.zip) are public;
private authority and raw diagnostics were not copied. This records local
linux/arm64 image execution, not live cloud or independent human reproduction.
