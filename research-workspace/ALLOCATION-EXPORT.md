# Local allocation drafts

This partial A5 implementation produces a digest-approved **DRAFT** from retained
synthetic estimation metadata and explicit operator narrative. It does not submit
an application, request resources, upload data, execute a workload, or establish
institutional authority. Every current-requirements, readiness, actual-resource,
execution, upload, cleanup and retention gate remains false.

The supported profiles are:

| Profile | Output and limits |
| --- | --- |
| `access_explore` | Summary/project-field planning worksheet by default, without PDF or font. An explicit preview option adds a planning PDF with a two-page internal cap; this is not an official narrative or form limit. |
| `access_accelerate` | Separate three-page description draft with objectives, resource estimates, computational plan, software/specialized needs and team preparedness. |
| `access_discover` | Discover project-description draft, one US Letter page, one-inch margins, 11-point minimum text. Other application documents remain on the checklist. |
| `nairr_research_draft` | Separate research planning layout. Current official instructions are unverified; eight pages is an internal bound, not an official requirement. |
| `nairr_classroom_draft` | Separate instructional layout; requires an instructional manifest. Current official Classroom instructions are unverified; eight pages is an internal bound. |

Maximize remains refused until its separate main, performance/cost, renewal and
external-document requirements have a proper multi-document profile. There is no
generic tier fallback. The ACCESS requirements snapshot was reviewed on 2026-10-19 against the [official preparation instructions](https://allocations.access-ci.org/prepare-requests).
A retrieved page does not establish eligibility, current resource availability,
exchange rules, deadlines or correspondence to the current application form.
NAIRR sources were robots-blocked during the requirements preflight. They remain
unverified; this tool does not retry them through another client.

## Prepare and review

Use the existing [assessment preparation](ASSESSMENT.md) and
[synthetic estimator](ESTIMATION.md) first. No manifest or receipt interface has
changed. The three accepted initial A5 preview records still validate unchanged;
new optional-render and null-font semantics apply only to Explore. Accelerate
does not infer eligibility from research or instructional manifest classification.
Supply the exact bound manifest for every retained series (maximum two).
Only defined metadata is accepted; unknown nested fields, raw upload hashes,
paths and logs are rejected. Software, manifest, receipt, font and official-source
digests have explicit allowed roles. These digests establish integrity, not
source authenticity. The export rechecks derived model arithmetic and totals;
it does not authenticate the original claims or turn invented counters into
measured performance.

The narrative is a UTF-8 JSON document with `schema` set to
`waypoint.allocation-narrative/v1` and a closed `fields` object. Use the appropriate
[fixture](assessment/fixtures/export/) as a starting point. Every narrative field
is shown in the preview, Markdown and worksheet, and in the PDF when included. Values must be NFC text,
without control characters other than newline. Title limit: 160 characters;
other fields: 2,048 characters each; whole narrative: 32 KiB. Enter only material
that you intend to export; manual narrative is operator-authored and unverified.

PDF support is optional. Explore worksheet-only commands need Python 3.12+
and its standard library; they do not load PDF code or read a font. PDF-producing
commands need a Python 3.12+ interpreter that has the versions in [requirements-pdf.txt](requirements-pdf.txt). Existing commands
retain their standard-library path; importing these modules does not load PDF
libraries or create the application store. The tool does not install dependencies.

For a PDF-producing preview, select an operator-vetted regular TrueType font file whose provenance and license
you have reviewed. Supported families are Arial, Courier New, Palatino, Palatino
Linotype, Times New Roman and Computer Modern (including CM Roman and `cmr10`).
Family metadata is a claim, not authentication; approval binds the exact font
bytes and size into the preview. Do not use untrusted font files. Missing glyphs,
unsupported fonts and font changes refuse export; there is no fallback or font
shrink. No font binary or local font path is included in the package.

```sh
python -m research_workspace assessment-export-preview retained-estimate.json \
  --manifest retained-manifest.json \
  --narrative narrative.json --profile access_discover \
  --font /absolute/approved-font.ttf > preview.json
```

Inspect **all** of `preview.json`: manual text, retained metadata, visible fields,
font proof, requirements checklist, warnings, exclusions, unknowns, scope and
cleanup liabilities. Its `preview_sha256` is the SHA-256 of canonical `record`
bytes (sorted keys, ASCII JSON escapes, integer tokens, compact separators).
The digest covers the complete approved record, not just a narrative excerpt.

An optional `--refresh-requirements` on the preview command performs a bounded,
read-only HTTPS GET of the fixed ACCESS public page. It sends no assessment input,
follows no redirects, uses no proxy, accepts at most 512 KiB and has a 12-second
acceptance deadline plus up to five seconds for child teardown. Only URL, UTC
retrieval time, status and content digest are retained. Failure remains a draft;
success is `retrieved_unreviewed`, never current-requirements readiness. Export
itself performs no refresh or network request. Changed source observations or
inputs require a new preview and approval digest.

## Explore and Accelerate inputs

Explore requires explicit `title`, `planned_work_summary`, `keywords`,
`field_of_science`, `supporting_grant_details`, `preferred_resource` and
`alternative_resource`. These are operator-entered planning fields. The current
public instructions establish a summary and project fields; they do not establish
a separate required narrative PDF or a summary page/character limit. Our text
bounds are internal safety limits, not portal constraints. Do not invent grant
support, eligibility, resource availability or scientific claims to fill a field.
An explicit statement of unknown or not applicable remains visible as written.

```sh
python -m research_workspace assessment-export-preview retained-estimate.json \
  --manifest retained-manifest.json --profile access_explore \
  --narrative explore-narrative.json > explore-preview.json
python -m research_workspace assessment-export \
  --preview explore-preview.json --approved-preview-sha256 THE_REVIEWED_DIGEST \
  --output /absolute/physical/new-explore-package
```

For an optional **internal planning copy**, add `--include-planning-pdf` and
`--font /absolute/approved-font.ttf` to the Explore **preview** command. Its two-page
cap is internal, not an official limit. The preview records `include_planning_pdf`
and its font proof. A worksheet-only preview records false and null respectively;
it refuses a supplied font without reading that file. Export has no render-mode
option and requires a font exactly when the approved preview includes a PDF.
Changing that choice requires a new preview and approval digest. No other profile
accepts the planning-PDF flag or a null font.

Accelerate requires the same project fields plus `research_objectives`,
`resource_estimates`, `computational_plan`, `software_special_needs` and
`team_preparedness`. Select `--profile access_accelerate` and provide an approved
font. All entered prose appears in the preview, Markdown, PDF and matching
worksheet; the PDF refuses above three pages. The five description sections are
distinct, and the tool does not fabricate performance evidence, team capability,
scientific prose or resource-unit conversions from the retained synthetic model.

Each tier has its own missing-document/review checklist. PI/co-PI CVs remain
uncollected. Explore retains the conditional graduate-student advisor/NSF GRFP
letter gate; Accelerate retains an applicant-specific supporting-letter review
without asserting that a letter is universally required. No personal documents
are ingested or generated. All current-requirements and authority gates stay false.

## Export the exact approved record

```sh
python -m research_workspace assessment-export \
  --preview preview.json --approved-preview-sha256 THE_REVIEWED_DIGEST \
  --font /absolute/approved-font.ttf --output /absolute/physical/new-package
```

The operator's explicit digest is consent for this local export only, not PI,
service-owner, institutional or provider approval. Preview and export do not
change source-environment defaults; all four source candidates remain disabled.

The output directory must be new, absolute, and have existing physical parents;
symlinked parents, `..`, and existing destinations are refused. On macOS resolve
aliases such as `/tmp` before selecting the output path. The directory is mode
0700 and files are mode 0600. PDF layout and contents are prepared before creating
the directory. A failed write preserves owned partial files and retains or restores `INCOMPLETE`
best-effort; do not erase that evidence to conceal a failure. Marker recreation
can itself fail. In that case the command explicitly reports publication
uncertainty; **marker absence and directory existence never establish success**.
No file or existing directory is overwritten, and there is no cleanup or
automatic retry.

`package.json` always says `PREPARED_DRAFT`: it describes byte inventory only.
Acknowledged completion additionally requires the successful JSON receipt printed
to stdout, with `status: COMPLETE_DRAFT`, the approved preview digest and the
SHA-256 of `package.json`. The operator must retain that complete receipt alongside
their review evidence. The tool writes only inside the requested directory; it
does not silently save the receipt to another path. Use a caller-controlled,
fresh evidence file or capture the complete terminal output. If using shell
redirection, enable no-clobber first and select a new receipt filename. If the
receipt is lost or truncated, completion has not been acknowledged; preserve the
prepared directory for review. A usable draft requires the receipt, its matching
manifest/hash inventory, and no `INCOMPLETE` marker. Completed sync calls are
recorded; crash/power-loss durability has not been demonstrated. A successful
receipt grants no scientific, institutional or operational authority.

Each package contains:

- `narrative.md`: editable literal-text narrative and visible summary fields.
- `source.json` and `worksheet.json`: identical canonical approved records,
  including detailed assumptions, coefficients, held-out errors, warnings,
  comparison, manifest/receipt hashes and unresolved cleanup claims.
- `worksheet.csv`: lossless JSON-pointer/scalar projection of that same record.
  Every cell is prefixed with one apostrophe to force spreadsheet text. Remove
  exactly that first character before JSON-decoding `json_value`; original
  leading apostrophes, equals signs, signs, whitespace and Unicode are preserved.
- `draft.pdf` when included: profile-specific PDF with all visible fields. Page overflow or a
  section too large for one page refuses; nothing is shrunk or truncated.
- `retained-preview.json`, `requirements.json`, `README.txt` and `package.json`:
  approved preview, missing documents/reviews, interpretation and file hashes.

Inputs are bounded to 256 KiB per estimate, 64 KiB per manifest, 32 KiB narrative,
1 MiB preview and 8 MiB font. The PDF is capped at 4 MiB and package payloads at
8 MiB. These are document-generation limits, not machine capacity qualification.

## Evidence and remaining gates

The checked-in export fixtures were generated using the pure Outfitter validator
at `de65bdab9303395226ebfb35e0e0740ab19b4d8e`. Counters are deliberately invented
arithmetic fixtures, not observed performance. Reproduce with:

```sh
python assessment/fixtures/generate_export_fixtures.py \
  --outfitter /absolute/pinned-outfitter --output /fresh/fixture-directory
```

The optional PDF test suite requires `ASSESSMENT_EXPORT_FONT`; when invoked it
fails if the font or dependencies are missing. Existing CI adds this suite after
its unchanged standard-library controls, using pinned dependencies and a
hash-verified [Computer Modern font from Matplotlib](https://github.com/matplotlib/matplotlib/blob/4aeb773422464799998d900198b35cb80e94b3e1/lib/matplotlib/mpl-data/fonts/ttf/cmr10.ttf).
Font SHA-256: `4dd9761b058c009db9b2145f55ee6617d093c273dd3d6c5b3e43ff6110fa9bf6`.
No font binary is committed. Tests exercise all supported PDF profiles and isolated standard-library Explore
preview/export without optional dependencies,
worksheet/PDF parity, metadata, leakage refusal, formula handling, page overflow,
font failure, digest approval, no overwrite and interrupted writes. Exact accepted
PR #51 preview digests are regression controls; a new mode cannot alter those v1
records. Maximize still refuses before reading inputs. Render and
inspect all pages as an additional local acceptance step.

Independent human/second-machine reproduction, real target measurements,
scientific/resource review, current program/form review and institutional
submission approval remain outstanding. Independent-operator selection and
machine selection are separate from this metadata export. The exhausted v4
exercise cannot be resumed. Original failed workload evidence and subsequent
cleanup reconciliation remain distinct; this export does not clear any new
cleanup liability or authorize retention/deletion.
