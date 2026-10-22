# CW1 existing-host CPU recipe

Prepared source, not an execution grant. Root is the sole remote operator.
No credentials, host address, student data or model keys belong in this bundle.
Use only root's freshly verified existing host and an isolated private directory;
do not connect to an old historical address or change existing services.

Root reported a 4 GiB existing Linux Docker host with approximately 3.2 GiB
available and 70 GB free on 21 October 2026. That is a point-in-time inventory,
not a reservation, tested capacity or billing guarantee. Root must recheck it,
existing workloads, installed tools/images and account coverage. No new resource,
purchase, public listener, institutional resource or image pull is requested.

## Smallest check first

The source bundle contains a self-contained Waypoint package check under
`overlay/waypoint/course-workspace`. It requires Python 3.10+ standard library
only. No pandas, browser, Docker, network, service or model is needed. From
`overlay/waypoint`, the exact check is:

```sh
python3 -B -m unittest discover -s course-workspace/tests -p test_info511_package.py -v
```

Request a fresh 15-second outer window, one CPU and 128 MiB enforced memory
ceiling. These are prospective controls, not measured consumption. Root's earlier
original-fixture check used about 24 MiB RSS and 0.078 seconds wall time. The
changed fixtures need this new run. Have the existing supervisor deny networking,
record stdout/stderr, exit/signal and peak memory, and terminate descendants at
deadline. Do not install a supervisor or claim a soft interpreter setting is a
hard limit. If no enforcement mechanism exists, root must resolve that first.

## Next serial slice: CSV source and service controls

Apply `overlay/cairn` only to a clean disposable copy of Cairn commit
`089911ece3783037d4f96ff0c9ca37e6907da320`. Verify all manifest hashes first;
never overlay the live checkout. Existing Go 1.26.9 and cached go.mod/go.sum
dependencies are required. No toolchain/dependency download or database service
is needed; the tests use disposable SQLite files. Commands are separate grants:

```sh
gofmt -l internal/assessment/extract.go internal/assessment/csv_test.go
env GOTOOLCHAIN=local GOPROXY=off GOSUMDB=off GOMAXPROCS=1 GOMEMLIMIT=768MiB \
  go test -p 1 -parallel 1 ./internal/assessment -run '^TestCSV' -count=1 -timeout=20s
```

The formatting command is inspection only. Formatting corrections change hashes
and require a new source freeze. The test slice requests a 60-second absolute
outer limit including compilation, one CPU and 1 GiB enforced memory ceiling;
Go's 768 MiB setting is only a soft runtime target. No race build in this first
slice. Keep failure/timeout raw evidence; do not increase limits, fetch missing
dependencies or rerun automatically. Four authored functions cover quoting/raw
citations, limits/path refusals, all-four capture/proposal/review and altered or
stale evidence. They do not replace required build/vet/race/Postgres/UI gates.

## Notebook/checker slice without Docker

Only after source review and a separate grant, the trusted instructor-authored
notebooks can run serially in a disposable non-root CPU workspace with no network,
the pinned Python 3.12/pandas 3.0.6 environment, and the exact B1 checker manifest
hash. Request one CPU, 512 MiB enforced memory and 60 seconds per notebook.
These bounds are proposed, not measured. Missing preinstalled dependencies stop
the slice. Never execute arbitrary submitted notebook cells on the host.

Copy the synthetic input into a fresh `data/` directory and the selected operator
notebook beside it. Run Alice; run Bob's fault cells only and preserve their
six-row CSV plus failing B1 check; then run the labeled repair and preserve its
nine-row CSV plus repeated B1 check. Record actual interpreter/package versions,
cell outputs, source/checker/data hashes and peak memory. Preserve all missing
values and exact input bytes. The authored CSVs are comparisons, not executed
results. Keep these operator examples out of student distribution/tutor context.

## Connected slice needs Docker

The current connected runner cannot perform authentic Cairn repository capture
and grading without Docker. It reuses real Forgejo OAuth, isolated acquisition
and the restricted grader. Do not replace them with mocks or a new native host
adapter to claim full-flow completion. Dockerless checks above are useful
independent evidence; they are not preview/confirm/review acceptance.

Requirements: reviewed Linux Cairn binary plus build evidence, matching built
web assets, existing Playwright 1.63.0/Chromium and rehearsal Python dependencies,
existing pinned Forgejo image, inspected `cairn-checkout:local` image and immutable
CPU course image ID. Preserve Dockerfile and dependency-lock hashes, architecture,
image IDs and binary SHA. No image/build identity is prefilled or fabricated.
Use a real approved synthetic workspace URL. The device-transfer slice does not
need a simultaneous Hub/notebook service and does not qualify the Jupyter bridge.

Run the command in the package README only after root admits a finite interactive
window and ensures an instructor can review within it. No fixed duration is
claimed for human review. One run only; no concurrent notebook/build/model task.
Plan for at least 2.5 GiB freshly available memory for the browser/Cairn/Forgejo
and one bounded checkout/grader, leaving existing workload/OS headroom. This is
a planning estimate, not measured capacity. Existing per-container limits are
512 MiB Forgejo and 256 MiB grader; root must inspect acquisition limits and
enforce parent browser/Cairn limits separately. A process cgroup alone does not
cap Docker-daemon-created containers. Disk need depends on already present image
sizes; record free space before/after and refuse uncontrolled build/cache growth.

Loopback listeners only, private synthetic roster, default AI/provider disabled.
No port publication or shared-service changes beyond the existing disposable
fixture. The runner requires actual four-point checker results, exact receipts,
all-four capture, human-authored judgments, explicit approval, grade export and
reopened submitted originals. No fake grades or invented successful receipts.

## Evidence and teardown

The runner's existing Course.close stops its owned Cairn process, closes relay,
uses Cairn recovery for only this work directory, removes labeled invocation-owned
containers/volume/network and verifies absence. Root must retain failures and
inspect remaining owned resources if interrupted; never use host-wide prune,
restart Docker, remove shared images or touch other chats' jobs. Killing the parent
alone is not container cleanup. A failed or expired run is not accepted.

Retain private synthetic evidence under root's explicit retention decision:
actual receipts/revisions, source captures, reviewed judgments, four-file exports,
grades CSV, stdout/stderr and cleanup inventory. Do not share raw service logs,
credentials, operator solution notebooks or private tutoring/monitoring data.
Cleanup is not independent physical deletion, backup expiry or classroom-service
retirement. Those gates remain separate.

Root owns host/account access. P1 owns covered OpenAI/Modal route, all-stage
attempt limits and usage reconciliation; B1 owns trusted tutoring/monitoring.
CW1 introduces no model route, consumer identity or provider accounting store.
There is no current CW1 operational DO credential/host record to add beyond
root's reported inventory; no further credential search is needed here.
