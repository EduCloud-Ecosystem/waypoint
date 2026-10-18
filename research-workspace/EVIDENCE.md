# Portable research evidence

`research_workspace.evidence` builds and verifies a bounded ZIP of one project
snapshot. It uses Python's standard library and performs no filesystem reads,
archive extraction, code execution, image downloads, network requests, or
publication. The service authorizes the export and supplies a consistent snapshot
of the project, all its runs, and the explicitly selected file bytes.

```python
from research_workspace.evidence import build_bundle, verify_bundle

payload = build_bundle(project, runs, files)
report = verify_bundle(payload)
assert report["valid"]
```

Failures raise `ValueError`. The successful report contains `project_id`,
`run_count`, `file_count`, `bundle_sha256`, `gaps`, and a warning that internal
integrity does not establish scientific validity, authenticity, or independent
reproduction. Preserve the archive hash in a separately trusted system when
provenance matters. A bundle can be internally valid while containing failed
runs, unverified cleanup, unresolved compute liabilities, or no results at all.

## Included records

| Member | Meaning |
| --- | --- |
| `project.json` | Project ID, name, question, success criterion, creation/end dates, compute budget, and status. |
| `runs.json` | Every supplied run, sorted by ID; includes condition, status, errors, immutable input/recipe hashes, environment, metrics, cleanup, and conservative compute accounting. |
| `comparison.json` | Descriptive per-run metrics, explicit missing-evidence conditions, and limitations. It does not rank methods or assert success against a scientific criterion. |
| `README.md`, `METHODS.md` | Interpretation, baseline/alternative methods, bounded rerun guidance, and limitations. |
| `ro-crate-metadata.json` | JSON-LD dataset and file descriptions with hashes and sizes for the project/run/comparison manifests and selected payload. |
| `checksums.json` | Exact SHA-256 and byte size of every other archive member, including the RO-Crate metadata. |
| `runs/<run-id>/input.csv` | Exact selected input bytes, required for every run. |
| `runs/<run-id>/recipe.py` or `recipe.R` | Exact selected recipe bytes, required for every run and matched to its declared recipe. |
| `runs/<run-id>/result.json` | Exact selected result bytes, required for succeeded runs and optional for other states. When present, it must agree with the run's metrics and condition. |

The checksum manifest deliberately excludes itself. The RO-Crate metadata names
`checksums.json` without hashing it, so there is no recursive checksum dependency.
The returned whole-archive hash covers all bytes, including the checksum manifest.
Hashes alone are not signatures: someone able to replace the full bundle can
rewrite all records and hashes consistently.

Private top-level project/run fields are never serialized. This excludes `owner`,
membership, submitter identities, keys, sessions, raw logs, and working paths.
Nested public metadata has an exact whitelist; unexpected fields fail closed.
The exported schemas are:

- `environment`: `image` (immutable SHA-256 image ID or digest), `runtime`
  (`python`/`r`), `runner` (`outfitter-docker-v1`). An incomplete non-successful
  run may use `{}`, producing an explicit environment gap.
- `parameters`: `{}` for the two reviewed recipes in this release.
- `metrics` and result JSON: `count`, finite `mean`, `condition`, and `groups`
  containing unique `group`, `count`, finite `mean` records. Runs without a result
  use `{}` metrics. No statistical interpretation is inferred from these values.
- `compute`: `reserved_cpu_seconds`, `charged_cpu_seconds`, `provenance`, and
  `unresolved_liability`. `estimated_upper_bound` requires finite nonnegative
  amounts. `not_admitted` requires zero amounts and no liability. `unknown`
  requires null amounts and unresolved liability; missing accounting never
  becomes a fabricated zero charge or refund. `reserved` retains a positive known
  reservation, zero charge, and unresolved liability until settlement is observed.

Metadata text rejects recognizable credential assignments, bearer credentials,
and absolute host paths. This is not a general secret scanner or a permission
check. Explicitly selected input and recipe bytes remain unchanged; the exporting
operator is responsible for their classification, licenses, and sharing approval.
Public or synthetic data is the scope of this local proof of concept.

## Verification boundary

Verification first checks archive safety and bounds, then exact checksum
membership, sizes and hashes, public metadata schemas, unique project/run
lineage, input/recipe hashes, and result/metric consistency. Derived comparisons,
methods, and JSON-LD must match the verified records. No undeclared files are
accepted. Metadata JSON uses deterministic sorted, compact UTF-8 encoding with a
final newline; duplicate keys and non-finite values are rejected. Selected result
JSON may use another serialization, but must still have unique keys and finite
values. This is a local deterministic encoding, not a claim of RFC 8785/JCS
certification.

Limits are deliberately small:

- At most 100 run records and 307 archive members.
- At most 64 KiB per input or recipe and 256 KiB per other member.
- At most 16 MiB of total uncompressed content and 16 MiB + 256 KiB of ZIP bytes.
- Deflate expansion is limited to 100:1. Exports use uncompressed ZIP members to
  avoid legitimate small repeated data exceeding this ratio.

The verifier refuses traversal, absolute and alternate path spellings, duplicate
names, symlinks and non-regular files, encrypted entries, unsupported compression,
extra fields/comments, overlapping entries, hidden prefixes/trailing bytes, and
inconsistent local versus central headers. Raw deflate decoding is bounded and
checked independently of declared sizes, including when an attacker lies about
the expanded size. ZIP64 and streamed/data-descriptor ZIPs are outside this narrow
format. No member is ever extracted to disk by the verifier.

## RO-Crate support

The JSON-LD uses the published [RO-Crate 1.2 metadata
structure](https://www.researchobject.org/ro-crate/specification/1.2/metadata.html)
and [root dataset/metadata descriptor
pattern](https://www.researchobject.org/ro-crate/specification/1.2/root-data-entity.html).
It includes an identifiable root dataset, a metadata descriptor, and file entities.
This private, unpublished snapshot does not invent a publication date, assign a
license to source material, or assert a formal profile conformance/certification.
It intentionally omits `conformsTo`; a future publication adapter must obtain the
necessary rights and publication metadata and validate its chosen profile.

## Checks run

```sh
PYTHONPATH=research-workspace python3 -m unittest discover \
  -s research-workspace/tests -p test_evidence.py -v
```

The 31 focused checks cover normal and incomplete Python/R exports, exact byte
preservation, deterministic ordering, secret-field removal, nested-schema refusal,
non-finite JSON, cross-project/duplicate records, rewritten checksums that attempt
to alter input/recipe lineage, missing/mismatched results, altered comparisons,
explicit unadmitted versus unknown accounting, unsafe ZIP names, symlinks,
encryption, expansion bombs including false size
declarations, member/total size limits, header disagreement, and hidden data.
These are archive and lineage checks. They do not stand in for actual Docker
execution, browser acceptance, an independent researcher's reproduction, or a
second study's scientific validation.

New run records may include `environment.specification`, a complete catalog
snapshot with declared dependencies and build provenance. Their comparison rows
include four separate lineage hashes (input, recipe, condition/parameters and
runtime image). The optional extension uses the same evidence writer/verifier;
older bundles retain their exact deterministic derived files and still verify.
`runs.json` is bounded to 2 MiB to accommodate up to 100 snapshots, within the
existing 16 MiB total archive bound. A snapshot grants no replay authority.
See [source-build qualification](SOURCE-BUILDS.md).
