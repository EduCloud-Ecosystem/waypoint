# Browser bundle operator source release

The release builder packages the operator tool and the 20 source files
identified by the [original rc.1 source manifest](https://github.com/EduCloud-Ecosystem/waypoint/releases/download/browser-bundle-v1.0.0-rc.1/SOURCE-MANIFEST.json).
It uses committed Git bytes from a fixed allowlist and checks all learner
files against the existing reviewed digest catalog. The
[learner bundle guide](BROWSER-BUNDLE.md) documents the separate command that
builds and verifies a starter ZIP for a selected assignment profile.

From a checkout of the intended source commit, using Git and Python 3.12+:

```sh
python3 course-workspace/classroom/release_browser_bundle.py \
  --version v0.0.0-rc.1 --out /absolute/existing-parent/operator-release
```

The example version is for local verification. Use a new output directory.
The builder refuses dirty changes to its own file or to the packaged files.
It produces four files:

- The operator ZIP, reproducible for a given commit with the same Git archive
  implementation. It contains both reviewed starter profiles.
- `SOURCE-MANIFEST.json`, with the source commit and SHA-256 for the ZIP and
  every packaged file.
- An [SPDX 2.3 JSON](https://spdx.github.io/spdx-spec/v2.3.1/) sidecar, with
  the same ZIP and file SHA-256 values, the required per-file SHA-1, and the
  package verification code. The UTC document creation time can differ
  between builds.
- `SHA256SUMS`, covering the ZIP and both JSON sidecars.

From the output directory, verify the checksums, validate the SPDX document
with [SPDX tools-python](https://pypi.org/project/spdx-tools/0.8.5/), and run the
learner bundle tests from the extracted operator tool:

```sh
shasum -a 256 -c SHA256SUMS
python3 -m venv /absolute/path/to/spdx-validator
/absolute/path/to/spdx-validator/bin/python -m pip install spdx-tools==0.8.5
/absolute/path/to/spdx-validator/bin/pyspdxtools -i waypoint-browser-bundle-v0.0.0-rc.1.spdx.json
unzip waypoint-browser-bundle-v0.0.0-rc.1.zip
python3 -m unittest discover \
  -s waypoint-browser-bundle/course-workspace/tests -p test_browser_bundle.py -v
```

The SBOM records explicit Apache-2.0 headers as file evidence. Aggregate
licensing, file license conclusions and files without reviewed in-file license
evidence use `NOASSERTION`. Its CC0-1.0 data license covers the inventory
metadata. The ZIP includes notebook and CSV files whose paths and bytes are
verified. Their redistribution terms have not been assessed by this builder.
Python 3.12+ and its standard library are external requirements. The browser
kernel, installed packages, and hosted fallback remain outside the archive
inventory and require their own qualification evidence.

This change uses the fixed rc.1 file list and the existing catalog on a clean
checkout from Waypoint main `f60c9489b843d68a42288ee4d9cf01cfa16686f8`.
The release tests compare every ZIP file against Git, the manifest, and the
SPDX inventory. They also exercise dirty source, a mismatched catalog,
a committed symlink, and output refusal. The course-workspace workflow builds
a real candidate, checks its digests, tests the extracted tool, and invokes
the SPDX validator. Review those results and the distribution terms before
using the builder for a future publication.
