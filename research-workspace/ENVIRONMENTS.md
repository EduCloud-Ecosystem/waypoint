# Reviewed research environments

`environments.json` is the packaged allowlist for new runs and historical bundle
reproduction. Its entries identify exact recipe bytes and immutable runtime
images. Inspecting this catalog does not pull images, execute code, or establish
research validity.

| Environment | Use | Packaged recipe |
| --- | --- | --- |
| `summary-python-v1` | Active Python group summary | `recipes/recipe.py` |
| `summary-r-v2` | Active R summary with exact string group labels | `recipes/recipe.R` |
| `summary-r-v1` | Archived historical reproduction only | `recipes/archive/recipe-r-v1.R` |

The archived R bytes are copied exactly from Waypoint commit
`373f494ca0f5b2ac06a62906024f41212dbb8862`, with SHA-256
`91db760044f6879d8171d34db57d5138e9255ce4d16bf489c919990dc65da3fd`.
This version used R CSV type inference, which can change labels such as `001`
or `NA`. A historical replay may fail independent validation. The replayer must
retain that failure and must never substitute corrected code. New runs use R
version 2, whose recipe hash is
`a3c06effab96558d8a09068adbfaf2650a3d9ab82b608ea63437dc9076fe3957`.

## Contract and trust boundary

The catalog has schema version 1. Each entry declares a versioned ID, stable
recipe identifier, active/archive status, immutable image reference, packaged
source path and SHA-256, runtime and dependency details, platform evidence,
baseline/alternative methods, resource limits, result tolerances, provenance,
and known limitations. The JSON file contains no credentials or local private
state paths.

The Python API is:

```python
list_environments(configured_images=None) -> list[dict]
resolve_environment(recipe, recipe_sha256, image) -> dict
recipe_source(entry) -> bytes
```

`list_environments` returns independent copies and adds `available`. This is
true only for an active entry whose image exactly matches the configured image
for its runtime. Omitting configuration returns metadata with every entry
unavailable. An archived entry is always unavailable for new runs. Availability
does not attest Docker health, platform compatibility, budget, or completed
execution.

`resolve_environment` requires an exact recipe/hash/image triple, including for
archived versions. An unknown or ambiguous identity raises `ValueError`.
`recipe_source` accepts an unmodified resolved or listed entry and returns only
the packaged source. It re-resolves the metadata and checks the source hash
again. Bundle-provided paths and executable bytes are never authoritative.

Every lookup validates the full catalog, including archived sources. It rejects:

- Unknown fields, duplicate JSON keys, invalid versions or duplicate identities.
- Mutable image tags, mismatched runtime/recipe pairs, or missing active versions.
- Absolute, traversal, malformed, or out-of-directory source paths; symlinked
  path components; nonregular, empty, or oversized files; changed source hashes.
- Resource limits or mean tolerances that differ from the execution contract.
- Added package dependencies or a clean-rebuild success claim that this catalog
  version has no evidence to support.

The catalog is bounded to 64 KiB, at most 16 entries, and at most 64 KiB per
packaged source. Source files are opened through directory descriptors with
symlink following disabled. This is an installed-code trust boundary, not an
authenticity signature: an attacker able to replace the trusted application and
catalog can also replace its verifier. Retain trusted release revisions and
whole-bundle digests separately.

`GET /api/environments` returns `{"environments": [...]}` after authentication.
`GET /api/recipes` retains its existing response contract. New-run admission
must resolve the current packaged recipe hash and configured image to an active
catalog entry. Historical reproduction may resolve archived entries, but it
must preflight every selected pair before creating any new compute work.

## Evidence and remaining gaps

Both pinned local image records were inspected on 2026-10-17 as **linux/arm64**.
Their image IDs matched the digests in this catalog. The local image
configuration declares Python **3.12.13** and R **4.5.1**; these version labels
come from image metadata, not a newly executed version command. No AMD64 or
other-platform support is claimed by these pins.

Python uses `csv`, `json`, and `sys`; R uses `base` and `utils`. There are no
additional language packages. This is the recipe dependency declaration, not
an inventory or rebuild lock for every operating-system package in the images.

The catalog references the retained local synthetic evidence by source version:
the original browser record covers Python and archived R; the later 41-check
HTTP/Docker record includes the corrected R hash and exact group-label controls.
Those records are not new catalog acceptance runs. The earlier failed R run is
retained in the original browser evidence.

| Criterion | Result | Boundary |
| --- | --- | --- |
| Strict catalog and packaged-source trust checks | PASS | 17 offline unit tests; no provider calls |
| Focused environment browser review | PASS | 17 checks on disposable local service; catalog503 and disabled-fieldset controls explicitly simulated; no compute submitted |
| Local image platform/declared-version inspection | PASS | Existing linux/arm64 image records only |
| Existing Python / corrected R execution | PASS | Referenced local synthetic evidence; exact recorded hashes |
| Clean image rebuild from source/build inputs | NOT RUN | Upstream build inputs and base-image construction are not captured |
| Independent operator or second-machine reproduction | NOT RUN | No such acceptance is recorded |
| Other architectures | NOT RUN | No support inferred from unrelated CI jobs |
| Real research data or institutional pilot acceptance | NOT RUN | Public/synthetic proof of concept only |

Run the offline catalog checks from `research-workspace` with:

```sh
python3 -m unittest discover -s tests -p test_catalog.py -v
```

The browser's **Environment & dependencies** disclosure shows the selected
version and lets viewers inspect the other active and archived entries. It
shows exact image/source identities, limits, tolerances, dependency declarations,
and the clean-rebuild gap. Failed catalog loading disables new browser
submissions until a successful refresh. Browser bundle upload or replay execution
is not part of this increment.

The focused record is
`evidence/2026-10-17/catalog-browser-verification.json`, with
`catalog-desktop.png` and `catalog-mobile.png` beside it. These are new catalog
records; the original 23-check browser record and its failed R run remain
unchanged. The new checks do not repeat role authorization, recipe execution,
manual screen-reader review, or independent-machine acceptance.
