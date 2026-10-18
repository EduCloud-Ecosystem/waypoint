# Clean research runtime builds

Issue [27](https://github.com/EduCloud-Ecosystem/waypoint/issues/27) adds a source
build and qualification path. Ordinary service configuration still selects the
historical reviewed images. New source-built environments are candidates,
disabled for ordinary admission and replay until a separate review/promotion.

## Recorded construction inputs

`source-builds/inputs.json` pins native Linux ARM64 and AMD64 Debian base
manifests, a BuildKit image, Python 3.12.15 and R 4.6.1 source archives and
SHA-256 hashes, a dated Debian package snapshot, and finite build bounds.
`Dockerfile` and `build.sh` are the canonical construction recipe. Each attempt
records their hashes plus the complete four-file build context. APT verifies
signed release/package metadata from that fixed snapshot; archive expiry is
intentionally disabled. The base plus snapshot fixes package selection, and
`packages.tsv` records every installed OS/toolchain package and version.

The source archives come from the official
[Python release](https://www.python.org/downloads/release/python-31215/) and
[CRAN source directory](https://cran.r-project.org/src/base/R-4/). Python's
published SHA-256 matched the downloaded bytes. R's SHA-256 was computed from
the official HTTPS download; this is a retained integrity pin, not a claim of
independent signature verification. Build flags follow the
[Python configuration documentation](https://docs.python.org/3.12/using/configure.html)
and [R installation manual](https://cran.r-project.org/doc/manuals/r-release/R-admin.html).
The selected versions differ from historical Python 3.12.13 and R 4.5.1.
Historical recipe/image identities and exact archived R source remain intact.

Python's recipe needs only `csv`, `json`, and `sys`. R's recipe needs only
`base` and `utils`. No third-party language packages, pip/renv infrastructure,
or language-package lockfile are needed. Python is built without ensurepip;
R is built without recommended packages, GUI, external BLAS or LAPACK.
The qualification image retains the pinned build toolchain. It is not a
minimal production image or a general-purpose research environment.

These are clean **runtime source builds on a binary OS/toolchain base**.
Debian, compilers, libraries, and BuildKit are not themselves built from source.
The guarantee is pinned-input reconstruction and checked fixture behavior,
not bit-identical output images. Architecture, build timestamps, compiler
outputs, configuration IDs and manifest IDs can differ. Every image has its
own recorded identity; a different rebuild must never impersonate a prior pin.

## Finite local procedure

Use the accepted Outfitter revision
`5827c84e9952cc67b85e91ceff4282e0ead6923b` (merged in PR 7). Build its broker
outside the original checkout or reuse a verified binary. All fixtures are
public/synthetic; no cloud resource, credential, private data, or deployment
is involved.

From `research-workspace`, choose fresh output directories:

```sh
python3 build_sources.py --platform linux/arm64 --repetitions 2 --output /absolute/new/build-output
python3 qualify_sources.py --builds /absolute/new/build-output/builds.json \
  --broker /absolute/outfitter --output /absolute/new/qualification-output
```

Use `linux/amd64` only on a native AMD64 Docker daemon. Emulation is refused.
The existing research CI workflow offers an explicit `clean_source_builds`
dispatch input for one native AMD64 build pair; it does not run automatically
for every PR update. CI is automated additional-machine evidence, never an
independent human reproduction.

The runner checks at least 12 GiB free host storage and Docker capacity before
each build. Its uniquely named builder is limited to two CPUs and 3 GiB RAM;
compilation uses two jobs, each image has a 20-minute deadline and a 16 MiB log
ceiling, and at most two pairs are built. A fresh dedicated builder, four-file
context, `--no-cache`, and no cache imports prevent reuse of compiled runtime
layers. Compilation has networking disabled; only pinned base/tool downloads
and source acquisition use the network. Package managers are used to obtain
necessary compiler dependencies, not to manufacture a language lockfile.

Build-client deadlines terminate the complete client process group. Short image
probes have owned container names, finite CPU/memory limits, bounded scratch
space and verified owned cleanup.

The runner stops its own builder on success/failure, retaining its volume,
images, contexts, build logs, and failed attempts. It does not prune shared
Docker resources. Build interruption, unavailable capacity, failed downloads,
and failed qualification remain visible. Host/daemon loss can prevent stopping
the owned builder; inspect `builder_stopped` and reconcile before continuing.

A completed construction can be rechecked after a post-build probe defect:

```sh
python3 recheck_source_probes.py --builds /absolute/failed-attempt/builds.json \
  --output /absolute/new/probe-recheck
```

This verifies retained contexts, logs, manifests, local image identity and
runtime versions. It refuses incomplete construction or changed packaged build
inputs. It writes a separate report linked by hashes to the unchanged failed
report; it never performs or claims another build. Pass that report to
`qualify_sources.py`. A builder-start failure has no completed images to recheck.

## Qualification and export

`qualify_sources.py` verifies retained context/log/inventory hashes and creates
a disposable application copy with explicit candidate image pins. Only that
copy activates candidates. Each image pair runs Python and R with both
baseline and alternative conditions on each of two synthetic projects: eight
successful matrix runs, plus exact-label, budget, authorization and export
controls. Counts/conditions/group strings are exact; means use relative
`1e-12` and absolute `1e-9` tolerances. Labels `001`, `002`, `NA`, and `NULL`
are tested explicitly. The unchanged Outfitter mechanism supplies finite
admission, network-free execution, conservative charges and cleanup receipts.

Candidate IDs include a SHA-256 derived from the complete recipe/image pair; a
later rebuild cannot silently reuse the same ID. Early numeric candidate IDs
remain accepted only as inert historical snapshot metadata, not installed
execution identities.

New runs retain an environment `specification` snapshot in `runs.json`, with
version, dependency declarations, platform, input hashes and build evidence.
`comparison.json` adds separate input, recipe, condition/parameter, and
runtime-image lineage hashes for those runs. Both projects use the same
manifest/export code. Arbitrary parameters remain disabled; baseline versus
alternative is the supported condition parameter. Offline tests independently
change each lineage dimension, without pretending to execute unreviewed code.

Snapshot validation is structural and never reads or executes a bundled path.
It does not make an embedded catalog trustworthy. Replay still requires an
exact match against the installed trusted catalog and packaged recipe bytes;
candidates cannot replay through the ordinary CLI. Earlier bundles omit the
new optional snapshot and still use their original deterministic manifest.
Historical browser replay must remain passing with its externally retained
SHA-256 `888bbb96e611e6ba4a9b6181b2d8a1b314ac6167bf39d2642fb2e8c07ab20d87`.

A bare `sha256:` is the immutable local Docker image ID, not a portable registry
reference. Build evidence records OCI manifest and configuration digests
when supplied by the engine. The local containerd image ID was the manifest
digest; the CI Docker image ID was the configuration digest. These are different
object types and must not be compared as if interchangeable. Retain exact images with `docker image save`, and load them before
attempting exact replay; images are not pushed to a registry by these tools.
Rebuilding creates new evidence and may create a different image identity.

## Acceptance boundaries

See [the committed source-build acceptance record](evidence/2026-10-17/source-builds/README.md) for measured results and
failed controls. Source construction, fixture repeatability, exact image
replay, and independent reproduction are separate claims. Independent-human
and second-machine reproduction, service-owner promotion, production security
qualification, hosted identity, real-data admission, and institutional pilot
acceptance remain explicit gates. CI cannot substitute for the human gate.

The [supplemental Documents replay observation](evidence/2026-10-17/replay-cleanup-reconciliation/README.md)
records later verified container absence and a conservative 30 CPU-second
ledger charge. It resolves that later resource/accounting observation while
preserving the original failed workload, receipt, report and acceptance record
unchanged. It does not promote candidates or satisfy human/hosted admission gates.
