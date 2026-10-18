# Two-binary broker upgrade qualification

This is an executable, disposable qualification of Outfitter baseline
`5827c84e9952cc67b85e91ceff4282e0ead6923b` against PR #16 merge
`5446438985e31cb739f2048a2aa2176918831a7e` (reviewed source
`28d34437e61a7bfe50fbfafcabc656a7f09fceec`). Waypoint starts at accepted
PR #42 merge `66a48dfe29d61d3f6f48f016f2c474e1ac6af4f6`.
It does not adopt the candidate, change an active CI pin, edit accepted state,
or enable native execution, consumer commands, quota admission or cloud work.

## Interface and provenance

`python verify_broker_upgrade.py --baseline-broker ABSOLUTE_BINARY
--candidate-broker ABSOLUTE_BINARY --output NEW_DIRECTORY` requires separate
builds and adjacent provenance manifests from `build_upgrade_brokers.py
--repository OUTFITTER_GIT_REPOSITORY --output NEW_PRIVATE_DIRECTORY`. The helper
reads the pinned Git blobs, verifies every compiled source hash, and builds with
module/toolchain downloads disabled. Source snapshots, build logs, exact command
and environment overrides remain private. It does not depend on embedded VCS
stamps, which this host's Go toolchain omits even with `-buildvcs=true`.
Binary hashes, source manifests and Go build metadata bind the supplied build;
they do not independently authenticate an operator-supplied executable or prove
bit-identical reproducible builds.
Both binaries must exist and preflight must finish before the new private output
is created. Only existing reviewed Python/R images from images.lock.json are
inspected; this harness never pulls images. State and credential-containing
checkpoints remain private. Public reports contain checks, identities, hashes,
finite attempt counts and limitations, never keys, sessions or private ledgers.

## Finite effects

One invocation permits at most seven sequential Docker execution attempts:
legacy Python, guarded R, one candidate post-cutover run for each, two fresh
bounded-export-v2 runtime runs, and one deliberate diagnostic-persistence failure.
Each request retains the existing 1 CPU, 256 MiB, 64 PID, 30 second, 65536 output
byte profile. The total admitted execution ceiling is 210 CPU seconds; these
are conservative reserved CPU seconds, not measured CPU or dollars. An attempt
is retained before subprocess launch and never reused or refunded. No automatic
execution retry or resume is provided. A new directory is mandatory. Unknown
fixtures use synthetic state and do not dispatch. Queries and cleanup use the
existing bounded subprocess collector; any cleanup failure remains visible.
Cleanup targets only this invocation's exact broker states and request IDs.
Later filesystem errors, external watchdogs and provider observation can remain
uncertain; this is not a proof that every possible writer stopped.

## Required observations

1. Baseline-created local v2 authority is readable by the candidate. Exact
   authority hash, request/payload identity, usage and diagnostic bytes survive
   reads and idempotent initialization. No native or consumer state appears.
2. Baseline checkpoints of ordinary and guarded workspaces restore using the
   candidate into held replacements. Current-source cutover fences the source,
   retains completed data and charges, cancels queued work without replay,
   clears sessions/rotates old keys, preserves revocations, and admits exactly
   one new bounded replacement run. Stale snapshots refuse activation.
3. Separately labeled synthetic unknown records remain full reservations under
   both executable CLIs and held restore; activation refuses. Missing diagnostic
   files do not block accounting/status; incomplete checkpoints refuse. No
   synthetic unknown is sent to a provider or automatically reconciled.
4. Native v3 and consumer v4 fixture state is explicitly outside Waypoint's
   qualification contract. The old binary refuses it without rewriting. The
   candidate's status/recovery reads leave the entire fixture ledger bytes
   unchanged and report unresolved state. This is not field-level native or
   consumer execution/settlement acceptance. No downgrade, field stripping,
   native settlement or provider execution is attempted.
5. Actual run receipts pass Waypoint's strict identity/accounting validation.
   An owned output-directory obstruction produces retained diagnostic failure
   with full conservative charge and no independently validated metrics.
   A fresh bounded-export-v2 workspace runs both existing reviewed runtimes,
   exports a verified archive, and still refuses finite restore/cutover.

The v2 report requires every named control and seven retained attempts; empty or
partial checks cannot pass. Before each launch it records a separate expected
binding: the fixed scenario slot, request/project ID, request hash, reviewed
image, broker build hash, relative state role and canonical state-path hash.
The seven slots are ordinary baseline/replacement, guarded baseline/replacement,
then bounded-export Python/R/diagnostic failure. Duplicate request IDs or an
unexpected image, state or broker refuse before launch. Final status receipts
must match those bindings and assert actual admission/execution, success with
exit zero, verified cleanup, no unresolved liability, and exactly integer
30 reserved/30 charged CPU seconds with `estimated_upper_bound` provenance.
The report oracle independently requires all seven and exactly 210 charged CPU
seconds. Boolean, string, floating-point, missing or negative charges and any
failure marker refuse. A valid pre-admission 0/0 refusal is not evidence of a
previously observed successful attempt's final accounting. Invalid final
observations remain visible and cannot erase the prelaunch attempt binding.
These checks establish accounting consistency, not independent provider truth.

Failure exits nonzero and retains its report, logs
and synthetic state. Original failed reports are never overwritten. Unit tests
must exercise refusal and evidence-oracle sensitivity without Docker, including
optimized Python execution; production assertions must not depend on `assert`.

## Evidence and adoption gate

Retain exact source/binary hashes, raw bounded CLI observations privately,
public check outcomes, archive/checkpoint digests and every failed invocation.
Existing qualification archives, catalog/image pins, all prior evidence and
Outfitter PR #15/#16 red witnesses remain byte-identical. Historical pins remain
historical. Existing HTTP/replay/recovery acceptance can be rerun against the
candidate in a separate adoption PR; this qualification alone does not change
production defaults, authorize accepted-store cutover or prove physical storage,
all-writer finality, hosted identity, off-host recovery or live Jetstream2.
