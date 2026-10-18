# Source-build acceptance: issue 27

The clean source-runtime construction and synthetic result criteria pass on
native Linux ARM64 and AMD64. Four source-built environment entries remain
**candidates**, disabled for normal admission, queued dispatch and replay.
The three original catalog entries and historical recipe bytes are unchanged.

[acceptance.json](acceptance.json) contains artifact SHA-256 hashes, actual-run
lineage pairs, controls, limitations and the precise evidence references.
Retain its Git commit and whole-bundle hashes separately; embedded hashes are
integrity checks, not signatures or proof of authenticity.

| Criterion | Result | Evidence and boundary |
| --- | --- | --- |
| Pinned source, OS base, platform, compiler/package snapshot | PASS | `arm64/build` and `amd64/build`; SHA-256 source archives, four-file contexts and complete installed package versions |
| Runtime source construction | PASS | Python 3.12.15 and R 4.6.1 compiled with networking disabled; Debian/compiler binaries inherited, not rebuilt |
| Both conditions and runtimes in two projects | PASS | Each platform: 49 checks, eight matrix runs, exact groups/counts, relative 1e-12 and absolute 1e-9 mean tolerances |
| Same manifest and export mechanism | PASS | Two project bundles per platform; dependencies/build provenance retained in run specifications |
| Four independent lineage changes | PASS | Actual successful exported pairs; recipe-only control uses the exact archived R input/image/condition |
| Historical browser bundle replay | PASS | `historical-replay`; two successful rows reproduced and the original failed row retained |
| Independent human on a second machine | NOT RUN | Native AMD64 CI is automated additional-machine evidence |
| Bit-identical images or equivalence to opaque historical images | NOT RUN | Distinct identities and architectures; no equivalence claim |
| Promotion, hosted service, private/student data | NOT RUN | Source candidates remain disabled |
| Failed Documents-directory replay cleanup | FAIL | `failed-documents-replay`; owned container removal remains in progress and its 30 CPU-second reservation remains unresolved |

## What actually ran

The initial local native ARM64 builds completed in approximately 481 seconds
(Python) and 564 seconds (R). R's post-build version probe then failed because
its read-only container lacked temporary scratch space. The original failure
record and images remain. `arm64/build` is an explicitly labeled **post-build
probe recheck** of those same images; it is not a second construction. The
recheck verified the original context/log/manifest hashes, observed runtime
versions and OS inventories, then full qualification passed. A later attempt
to start another local builder hit its 180-second deadline before construction;
that attempt is retained under `failed-arm64-builder-start`.

Native AMD64 CI first completed both constructions but hit the same R probe
failure. With bounded scratch space supplied, the fresh build and qualification
passed at revision `7a84e1a0cb22f5074618e267c023f5cd7562b032` in
[run 39075522315](https://github.com/EduCloud-Ecosystem/waypoint/actions/runs/39075522315).
That run also passed ordinary Docker acceptance, historical replay and recovery.
Final local qualification uses the final hash-bound catalog IDs; earlier
numeric-ID qualification artifacts remain inert evidence snapshots.

The full matrix contains eight successful runs per architecture. The same
harness additionally covers two exact-label runs, one successful budget-control
run and one expected refusal. The latter remains a failed run, not a successful
computation. Original and supplemental attempts are never overwritten.

The local Docker/containerd image IDs equal their OCI manifest digests; that
engine did not supply separate configuration digests in its build metadata.
CI Docker IDs equal configuration digests, with manifest digests separately
recorded. Native bases and package architectures differ. Construction times
come from different hosts and are not a performance comparison. CI images
are ephemeral; their logs, manifests and digests are retained, not image tarballs.
Exact future replay of these candidates requires retaining/loading the exact
image or a separately reviewed distribution path. Rebuilding can create a new
identity; the source/recipe pair's hash prevents silent ID reuse.

## Retained failures and unresolved work

- `failed-arm64-probe` and `failed-amd64-probe`: construction completed, then the
  original R version probe failed. Paired probe outputs and their diagnostic
  interpretation are retained separately. The first control assertion expected
  a different R diagnostic; the recorded stdout identifies failure to create
  the `-e` temporary file. The corrected probe reports R 4.6.1.
- `failed-arm64-builder-start`: no new construction occurred. The finite startup
  deadline stopped the attempt. No unrelated images, caches or containers were
  removed.
- `lineage-initial-comparison.json`: the first recipe-only comparison also
  changed input, so it was rejected as an isolated control. The dedicated
  `recipe-lineage` run holds input, condition and image fixed and passes.
- `failed-documents-replay`: Python stalled in uninterruptible filesystem I/O
  while using a Documents-directory bind mount. Broker reconciliation and
  removal of the verified owned container could not confirm cleanup; Docker
  reports removal already in progress. Container
  `98c0b3f716beeb62997a7183cec8b19b2566ceb6b3e74deb4a99cd7b14434fae`
  remains an operator cleanup item. Its reservation is not refunded. The `/tmp`
  replay and qualification runs passed with verified cleanup. Restarting shared
  Docker or unrelated pilots is outside this change.

Independent-human reproduction, candidate promotion and any production/data
admission still require separate review. No subscription, paid cloud resource,
new hardware or production deployment was used. Belay and parent-owned pilot
worktrees were not modified.
