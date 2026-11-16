# Runtime gates for a student release

Engineering closure plan, 2026-11-14. Owner: Waypoint runtime chat. The classroom
chat owns the overall release record, course/data decisions and human acceptance.
Starting point: clean main `946f11bf0e64b7230b92631607e88e7c2dc4d4db`, isolated
branch `codex/waypoint-runtime-release`. No deployment, provisioning, paid
inference, learner data or mobile work is authorized by this record.

## Current evidence and the concrete change

Main's post-merge worker run43554803093 passed17 phases, authenticated run43554803098
passed12 runtime and10 enrollment phases. These include actual Python/R and
cleanup on one Docker daemon with logical workers. They do not establish
independent-host recovery, classroom load, or the cause of the earlier503.

Historical run43548520258 failed the R WebSocket handshake after kernel creation
and before `execute_request`; Python had executed. This was the direct JupyterHub
proxy path, not institutional nginx or VS Code. The old attempt lacks the proxy
logs needed to identify the transport error. Post-failure running/no-OOM container
state is not an atomic observation of the failure. Keep the failed report; do not
claim the later passes fixed its cause.

Source review found a separate hosted-test defect: `smoke.kernel` replaced
`http://` with `ws://` but left `https://` unchanged. The probe now maps schemes
structurally, including HTTPS to WSS, preserving origin, IPv6, port and path.
It still makes one kernel POST and one handshake with the existing deadlines;
there is no mutation retry or authorization change. This fixes a test-client
defect, not the historical HTTP503 or a production R implementation.

Reuse: retain the existing smoke kernel driver, CHP5.1.0 and bounded diagnostic
category parser. Extend with a small loopback control, rather than replacing R,
Hub, proxy or worker ownership. No UI files changed.

The preregistered limit was3 targeted runtime attempts/45minutes total. Exactly
one control was run:0.354seconds, PASS, child and listener cleanup verified.
[Plan](evidence/2026-11-14-runtime-release/LOCAL-PLAN.json) and
[result](evidence/2026-11-14-runtime-release/PROXY-CONTROL.json) retain scope,
source revision/dirty status and exact code hashes. The experiment used Node26.7.0
on this Mac, not the hosted Linux Hub image. It is a transport diagnostic control,
not another successful R or learner-runtime run.

| Controlled case through actual CHP5.1.0 | Observation |
| --- | --- |
| TLS connection to synthetic WebSocket upstream | 101; message received; certificate and IP SAN verified |
| Closed upstream TCP port | 503; exactly one ECONNREFUSED diagnostic |
| Application explicitly returns503 | 503; no additional transport diagnostic |

Thus503 status alone does not identify its layer. The existing parser recognizes
the real pinned proxy log format. Counters are cumulative; absence of a counter
is inconclusive if logs are unavailable, truncated or outside the capture window.
This synthetic experiment cannot establish which case occurred historically.
Raw response bodies, URLs, certificate keys and logs are absent from the report.

Focused checks:3 kernel-transport tests passed; diagnostic/fixture tests ran46,
44 passed and2 skipped (optional Docker SDK absent; mixed root/learner ownership
requires Linux/root). The latter controls already have prior Linux CI evidence;
this local run does not relabel the skips. Node syntax validation passed. No
already-green full runtime suite was repeated. The original notebook download,
hosted fallback and mobile worktree/container are preserved.

## Reproduce only the targeted control

Use the existing test requirements and Node18+ in a disposable local directory.
Installation fetches the small proxy package/dependencies, not a course image.
The generated npm lock is part of private local evidence. Never point the fixture
at an existing service or publish its listener. It binds127.0.0.1 only, exposes
no control API, starts no kernel and terminates its exact child within120seconds.
Each invocation requires a fresh output directory; failures are retained as FAIL.

```sh
npm install --prefix output/proxy-control-deps --ignore-scripts --no-audit --no-fund configurable-http-proxy@5.1.0
python course-workspace/rehearsal/verify_websocket_diagnostics.py \
  --proxy-module output/proxy-control-deps/node_modules/configurable-http-proxy \
  --output output/proxy-control-001
python -m unittest discover -s course-workspace/tests -p test_smoke_transport.py -v
python -m unittest discover -s course-workspace/rehearsal -p test_remote_notebook_fixture.py -v
```

## Close the R transport release risk

The next useful runtime execution belongs on the selected hosted topology,
after its prerequisites below. Repeating the same Mac/one-daemon run without a
new hypothesis is not a closure strategy. Before that execution, record exact
source/images, actual per-host reservations, approved synthetic course fixture,
endpoints, expected outcomes and a finite attempt/time budget.

Required engineering evidence:

1. Capture a bounded attempt timeline: synthetic endpoint/kernel type, kernel
   creation result, handshake result, execute-request/reply/idle, saved-file hash,
   proxy error category and whether each log was available/truncated. Record
   container state and Guardian/authority events around that window; keep the
   existing post-failure snapshot explicitly non-atomic. Add windowed diagnostic
   capture before the next hosted run; current cumulative counters alone cannot
   correlate an earlier Guardian refusal with a particular handshake.
2. Through the real HTTPS origin, cold-start the approved Python and R examples,
   save/reopen exact bytes and execute again after a reviewed stop/start. Require
   valid TLS, current authorization, one POST per kernel creation, successful
   handshake and exact expected output, without hidden retries. Repeat the same
   matrix after the already-required rotation/recovery transitions, not as an
   unbounded stress loop.
3. Inject a controlled route/upstream loss and an authorization expiry in the
   disposable fixture. Require correctly classified refusal, no replay of an
   uncertain mutation, stopped owned learners within configured enforcement
   deadlines and retained files. Recovery requires current authorization and an
   explicit new operation. Do not increase deadlines to hide failure.
4. Any unexpected503 or unknown outcome fails that attempt. Retain its bounded
   evidence and identify the layer before another separately justified attempt.
   A successful later attempt does not overwrite it.

Closure can follow a demonstrated causal repair, or a documented residual-risk
decision after the prospective acceptance matrix and recovery procedures pass.
The latter must state that the historical cause is unproven. Engineering cannot
retroactively manufacture missing logs or sign institutional risk acceptance.
Neither closure path has been completed here; real-student admission stays held.

## Hosted and off-host recovery gates

Existing [host preparation](../course-workspace/INDEPENDENT-HOST-PILOT.md),
[worker channels](../course-workspace/INDEPENDENT-WORKERS.md),
[notebook protocol](../course-workspace/REMOTE-NOTEBOOKS.md) and
[recovery packaging](../course-workspace/RECOVERY.md) are the starting components.
Reuse the enrollment, epoch/quarantine, archive-validation and explicit-review
contracts. Do not attach the older single-course backup timers to worker channels.
Their local-copy fixture and Restic package acceptance are different capabilities.

| Gate and owner | Exact prerequisite / work remaining | Measurable acceptance |
| --- | --- | --- |
| Hosted inventory — operator plus runtime engineering | Selected dedicated provider/project/region, three failure domains (workerA, workerB, controller), verified host identities, private routes/CA names, per-host CPU/RAM/storage reservations, expiry/spend/teardown owner. No provider is selected by this plan. | Each role bound to its real host/kernel/daemon identity; controller/Hubs have no Docker socket; worker state/homes never shared; public access closed until TLS/identity acceptance. One-host smoke is recorded separately. |
| Role installation — runtime engineering | Build a role-specific installer/inventory from existing worker bootstrap and managed enrollment. Bind config, storage and certificate fingerprints, and install current-policy service lifecycle. | Fresh install from exact commit/image hashes; least-privilege mounts/ports; wrong peer, stale credential and wrong role refused; restart preserves journal/ownership. No invented host attestation from an operator label. |
| Host loss and incarnation — runtime engineering | Implement reviewed host-incarnation replacement and independently verified fencing. Current `worker_runtime._local` refuses a changed boot ID; `RemoteBackend._fencing` requires both worker channels. No existing unreachable-source promotion is supported. | Real reboot remains closed until reviewed re-enrollment/replacement; unreachable source cannot authorize promotion; provider/independent stop proof is bound to exact host/incarnation before receiver activation. Old source cannot resume after reconnection. Keep quarantine and reservations on uncertainty. |
| Encrypted transfer — runtime engineering plus backup custodian | Add an off-host transport for this worker-channel layout, with independently held key, approved remote repository, immutable image/config inventory, stopped empty receiver and validated metadata. No local volume copy as substitute. | Source inaccessible during restore; wrong key, corrupt archive, occupied receiver and interrupted transfer refuse safely; file bytes/modes, current roster/quotas and image identities verified; retained source/receiver copies and journals survive failure. No server starts automatically. |
| Independent-host acceptance — runtime engineering | Build a runner/evidence schema observing distinct hosts/daemons, network transfer and reviewed fencing/recovery. Approved RPO/RTO and load envelope supplied first. | One worker loss preserves peer service; authority/channel/Guardian failures enforce existing deadlines; old sessions/removed identities denied; explicit receiver activation executes Python/R and reopens exact saved bytes. Record observed RPO/RTO and compare with approved targets; no claim of zero data loss from saved-file fixtures. |
| Operational readiness — classroom owner/operator | Current institutional identity/data routing, course package, support/retention/incident owner, alert delivery and key recovery. No Brightspace grade integration assumed. | Allowed/denied login and active-session revocation through actual origin; quota exhaustion cannot damage peer; alert acknowledged by named operator; current backup restored independently and teardown inventory reviewed. Instructor/student acceptance remains separate. |

Until fencing exists, an unreachable source remains pending. Certificate revocation
or a failed network request is not proof the source stopped. Existing worker-agent
restart tests do not demonstrate host-reboot recovery. Do not reset journals,
change identity records by hand, clone the authority database or auto-promote.

Engineering can implement installers, windowed diagnostics, transport and evidence
schema locally before requesting infrastructure. The only eventual Greg/operator
inputs are the approved target/limits (including learner concurrency and RPO/RTO),
institutional identity/data/backup custodians and acceptance of the completed
release evidence. No new spending or deployment decision is needed for this patch.

## Rocker fallback and editor qualification

Greg requested Rocker if R is unsuitable in VS Code. Current `ide-pilot/Dockerfile`
installs Python/extensions only; no R-in-code-server result exists. The current
course image uses Debian-packaged R plus IRkernel behind Jupyter. The historical
Jupyter503 must not be relabeled as an R-in-VS-Code failure.

Prefer the official **rocker/r-ver4.6.2** as the smallest headless candidate for
an R runtime with the existing IDE. Official [image descriptions](https://rocker-project.org/images/)
show `rstudio` adds RStudio Server, `tidyverse` adds packages, and `verse` adds
TeX/publishing tools. RStudio is a separate desktop UI and launch/auth integration;
it is a useful fallback if R editing/debugging needs it, not a transparent image
swap. `verse` is justified only by the approved course's publishing requirements.

Upstream [recipe commit aabd57a](https://github.com/rocker-org/rocker-versioned2/blob/aabd57a293d4f740113bacfd2ecc431f53641ae3/dockerfiles/r-ver_4.6.2.Dockerfile)
was updated2026-11-13. Registry metadata was checked2026-11-14 without pulling
layers. Proposed Linux/amd64 reference:

`docker.io/rocker/r-ver:4.6.2@sha256:3adf31fa5c953bdee2709023366d1bfba306e6dcbe8eb34bf8a668119e37d2ca`

Its compressed layers total416,223,091bytes; this is registry metadata, not a
measured installation footprint. [Candidate metadata](evidence/2026-11-14-runtime-release/ROCKER-CANDIDATE.json)
also records the arm64 digest. Official [r-ver guidance](https://rocker-project.org/images/versioned/r-ver.html)
calls arm64 experimental. Use native amd64 for the initial hosted candidate.
The current recipe uses Ubuntu26.04 and an unpinned `latest` package mirror:
pin course packages/snapshot separately. A digest pins the downloaded image,
not future package installations or compatibility with Waypoint.

Before substitution, build a separate candidate recipe and require:

| R conformance check | Required evidence |
| --- | --- |
| Plain R independent of editor | Exact R/package inventory; deterministic course calculation and plot match approved outputs; nonzero exit and stderr observable for an intentional R error. |
| Existing desktop IDE | Pinned R language/debug extensions; edit/run, breakpoint/step/local-variable inspection, interrupt/error recovery, Git commit/diff and laptop reopening of the same saved workspace. Measure actual student task usability. R executable success alone is not IDE acceptance. |
| Jupyter compatibility if retained | IRkernel registered and selected; trusted HTTPS kernel execution and file reopen; compatible Python/Jupyter runtime. Ubuntu base/Python paths differ from the current3.12 image and require a reviewed recipe, not a `FROM`-only replacement. |
| Workspace isolation | Explicit nonroot learner UID1000 and `/home/learner` mapping; read-only root, dropped capabilities, PID/CPU/RAM limits, restricted network, hard home quotas, peer denial and no Docker socket. `r-ver` has no default nonroot user. |
| Lifecycle/recovery | Same current identity/enrollment/expiry/Guardian stop controls, immutable image recovery, byte-identical retained work and verified owned cleanup. No second writable workspace copy or simultaneous unsupervised IDE service. |

If R itself passes but code-server editing/debugging fails that matrix, qualify
`rocker/rstudio` behind the same managed workspace lifecycle; prefer `tidyverse`
or `verse` only when the course needs their additions. Record its separate UI,
authentication, persistence and licensing review before admission. No Rocker
runtime, RStudio service, new editor or image-base replacement was executed here.

## Checkpoint and next action

Branch `codex/waypoint-runtime-release`; base946f11b. New source is limited to
`smoke.py`, its3 regression tests, and the synthetic proxy fixture/runner. New
documentation/evidence lives in this plan and its sibling evidence directory.
The proxy report truthfully records a dirty working tree and source hashes;
compare those hashes with the committed files before relying on it.

Ready for source review; not merged or deployed by this checkpoint. Next engineering
step: windowed attempt diagnostics plus role-specific host inventory/installation
contract, then worker-channel encrypted-transfer/fencing design. The classroom
chat chooses sequencing against its overall release gates. Current evidence
does not justify another full runtime run or any student admission.
