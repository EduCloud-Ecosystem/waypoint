# Assessment manifest preparation and receipt preview

The operator CLI prepares and validates a content-free benchmark manifest for
local public/synthetic assessment. It does not accept a dataset, create scratch
or a workspace, initialize a budget, call a provider, or grant execution. The
research-study bundle format remains separate because it deliberately retains
input and recipe bytes.

This implements the preparation and read-only preview boundary of the
[canonical assessment contract](https://github.com/EduCloud-Ecosystem/educloud/blob/1184ab2c2a0c82aa437b2d146f22dbae2b98efa7/docs/specs/research-assessment.md).
Encryption, independent expiry/cleanup, actual-resource qualification, hosted
identity, independent-human reproduction and real-data admission remain open.
Greg Chism remains the service-owner approver for local public/synthetic use.

The separate [synthetic estimator](ESTIMATION.md) consumes these existing
manifest/receipt shapes through the pure validator. It adds deterministic
calculation and comparison without adding execution or trust authority.

## Prepare or inspect a manifest

From `research-workspace`, inspect the committed synthetic example:

```sh
python3 -m research_workspace assessment-validate assessment/fixtures/research-default.json
```

This returns the canonical manifest SHA-256, structural validity, current expiry
and whether the design is explicitly smaller/provisional. Execution authority,
upload admission, cleanup trust and finalized retention remain false. An expired
manifest can be inspected without reviving it.

Prepare an operator-supplied manifest with a future absolute expiry:

```sh
python3 -m research_workspace assessment-prepare /absolute/manifest.json
```

Preparation writes only canonical JSON to stdout. The caller may deliberately
redirect it to a chosen file. The command does not create an output directory or
private state. Expired preparation is refused. The fixed example expires on
October 20, 2026; retain it unchanged as a shared digest vector and use a separate
manifest for a later proposal.

## Closed version 1 shape

The wire schema is `educloud.benchmark-manifest/v1`. The committed fixture is the
complete shape: unknown fields and free-form content are refused at every level.
Research manifests require `instruction: null`; instructional manifests require
bounded enrollment, simultaneous sessions, assignment count, deadline-peak
sessions and per-student storage. These are declared assumptions, not observed
capacity or Cairn-issued course authority.

Only the packaged active Python/R recipes and their exact reviewed software
hash/image/identity bindings are accepted. Source candidates and archived entries
cannot be enabled by a manifest. Software hashes identify reviewed code; hashes
of uploaded data, filenames, logs, raw output and narrative fields are not part
of this version. Third-party dependencies, arbitrary stages/commands, GPU work
and model acquisition are outside this initial preparation path.

The default design has three distinct fitting sizes and one different held-out
size, each with three repetitions. Sizes use rows and remain within the existing
1,000-row recipe bound. Repetition seeds and the attempt denominator must agree.
A smaller design requires an explicit user-approval declaration and a bounded
reason, and remains provisional. That declaration is not an authorization token.
Measurement receipts must additionally keep fitting and held-out observations
distinct; choosing different sizes alone cannot prove independent observations.

Quality uses the reviewed independent summary check and its existing tolerances:
one part per trillion relative and one nanounit absolute. Failure allowance is
declared separately. Finite attempt, CPU-second, memory, scratch, output and
per-attempt deadline ceilings are required. These are proposals, not resource
reservations or whole-store quota enforcement. Hardware descriptors are
structurally checked; no external authority or hardware is queried.

`reservations` is an array of per-attempt declarations. Each entry binds an
attempt ID, reservation ID, stage, fitting/held-out role, size and repetition.
The slot tuple and both IDs must each be unique. Empty and partial arrays are
valid preparation: the summary reports `unreserved`, `partial` or `complete`
coverage with the declared/planned counts. Even complete coverage has
`references_verified: false`; it is never ledger-backed admission. The fixture
declares only one of twelve slots. A receipt must match that exact entry.

## Canonical bytes and shared vector

Use ASCII JSON with recursively sorted object keys, no whitespace and no trailing
newline. Text is a closed ASCII vocabulary or bounded opaque identity. Numbers
are integer tokens within the interoperable `2^53 - 1` range; floats, exponents,
NaN, Infinity and booleans in numeric fields are rejected. Ordinary JSON string
escaping applies. The digest is bare 64-character lowercase hexadecimal SHA-256
of the manifest bytes, with no prefix and no embedded self-digest.

The unmodified `research-default.json` canonical digest is:

```text
506a3587d73e70c1d545631ea2dd455056db92570e394230d5f56d81a7738420
```

Whitespace and input-key order do not affect the canonical digest. Duplicate
keys are rejected before canonicalization, including nested duplicates.

## Input and trust boundary

Assessment inputs are bounded regular files up to 64 KiB. Final symlinks,
hardlinks, directories, named pipes, excessive nesting and changing file sizes
or timestamps are refused. Input errors contain fixed messages rather than
caller-supplied filenames, fields or content. The CLI uses the operator's local
file access; it is not a hosted authorization boundary.

Receipt structure and actual trust remain separate. A claimed cleanup outcome,
local quarantine or reuse denial cannot prove external termination, remote
credential revocation or removal of all copies. No preparation command upgrades
these claims into approval, releases liabilities, or clears an execution gate.

Existing project records, study exports, raw private state and failed evidence
remain unchanged. Assessment files cannot be imported as study evidence ZIPs,
and retained-study ZIPs cannot be interpreted as assessment manifests.

## Preview the retained record

Use an explicitly selected, trusted local Outfitter executable that implements
the pure `assessment-receipts` command. The older execution broker pinned by
the existing Docker workflow does not provide this new command.

```sh
python3 -m research_workspace assessment-preview \
  assessment/fixtures/research-default.json \
  assessment/fixtures/legacy-receipts.json \
  --outfitter /absolute/path/to/outfitter
```

Waypoint validates its manifest and sends one receipt pair to
`outfitter assessment-receipts` as bounded stdin JSON with
`operation: validate`. Outfitter owns receipt semantics and cross-field
validation. Waypoint checks the returned wrapper, canonical digests, exact
manifest/attempt binding and the closed display vocabulary. The helper receives
no state-directory argument and this path does not initialize Store, Broker,
worker, ledger or an evidence bundle. It creates no scratch files and makes no
provider or network calls. The executable itself is trusted operator-installed
code; this is not a sandbox for an untrusted executable.

The preview prints the exact draft `retained_record` plus binding digests,
expiry, partial reservation coverage and explicit false execution, upload,
cleanup-trust, actual-resource and retention-finalization flags. It can inspect
an expired proposal without reviving it. There is no report-finalization or
upload route in this slice, and no narrative text is retained automatically.

Measurements retain explicit units, availability, scope and unverified source.
Reserved CPU seconds, conservative accounting charges and provider charges
remain separate from measurements. Unknown/unsupported metrics keep null
values. Legacy wall timestamps do not become CPU consumption or phase timings.
Unknown reuse remains null instead of becoming evidence of a fresh dispatch.

Execution, quality and cleanup are labeled as untrusted claims. Arbitrary
receipt/verifier labels and helper diagnostics are omitted. The initial preview
refuses embedded `outcome: verified`. The frozen legacy fixture keeps cleanup
unresolved, termination and access revocation unknown, and local quarantine and
reuse denial true. Its execution-resource deletion is only a provider's
`reported_complete` claim; temporary-data and recovery-copy deletion remain
unknown. A settled conservative charge cannot clear these cleanup liabilities.

Each input file and accepted result is capped at 64 KiB. The helper transport
caps stdout at 256 KiB and stderr at 64 KiB, rejects nonzero exits, and uses a
five-second acceptance deadline with up to five seconds of process teardown.
Malformed, oversized or failed helper output produces only fixed local errors.
There is no fallback to another provider, retry, new allowance or resource run.

## Verification scope

`tests/test_assessment.py` checks the manifest and actual operator CLI, including
reservation uniqueness/coverage, canonicalization, candidate refusal, closed
types and no state/export initialization. `tests/test_assessment_preview.py`
uses the shared synthetic receipts and a bounded local stub executable to test
the CLI transport and retained projection. Those tests do not validate the Go
implementation or establish measured resource/cleanup evidence. Cross-component
integration must separately run the real pure Outfitter validator against the
same frozen vectors and adversarial cases before integration acceptance.

No assessment workloads, engines, source builds, model acquisition, independent
operator exercise, source promotion or hosted/private-data admission are part
of these checks. Existing allowances and disabled source candidates remain as
recorded.
