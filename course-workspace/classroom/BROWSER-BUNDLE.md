# Reviewed learner bundles and browser readiness

This offline operator tool packages the existing `legacy` or `nhanes-v1` starter
materials without starting a notebook, model, service or network request. It is
the preparation slice for comparing suitable browser notebooks with the existing
hosted course workspace. It does not install or produce JupyterLite.

The package reuses `assignment_profiles.py` and the existing descriptor-relative,
no-symlink saved-file reader. `browser_bundle_sources.json` lists every reviewed
learner file and its SHA-256 digest. The archive has deterministic ordering,
timestamps and stored bytes. Unlisted files are never traversed or included.
`operator/`, solutions, hidden files and execution outputs are excluded. A changed
starter source blocks the build; review the change and update the specific
catalog entry deliberately, never regenerate digests from arbitrary learner work.
Digests establish byte identity with that local reviewed catalog, not authorship,
scientific validity, a digital signature or provenance against a malicious editor
who can change both code and catalog.

The newly added `browser_bundle.py` and `test_browser_bundle.py` use Apache-2.0;
the full text is in `browser_bundle.LICENSE`. This does not change the licensing
of existing dependencies, course materials or public source data. The operator
tool requires Python 3.12+ and uses only the standard library. A source release
is for operators; a learner receives only the archive created by `build` after
the actual fallback origin is selected. Source publication does not qualify a
browser kernel or a hosted classroom.

From the repository root, supply an existing absolute destination directory and
the actual course operator's approved HTTPS workspace origin. The example origin
below is a placeholder; the tool never contacts or validates a service there.

```sh
python3 course-workspace/classroom/browser_bundle.py build \
  --assignment-profile nhanes-v1 \
  --hosted-fallback https://workspace.example.edu \
  --archive /absolute/existing-directory/nhanes-starter.zip
python3 course-workspace/classroom/browser_bundle.py verify \
  --assignment-profile nhanes-v1 \
  --hosted-fallback https://workspace.example.edu \
  --archive /absolute/existing-directory/nhanes-starter.zip
```

Build refuses overwrite and symlink ancestors, writes a private mode-0600 file,
and prints only bounded source-bundle metadata. Verification rebuilds expected
bytes from the local reviewed source, the selected profile and the supplied
fallback. It never extracts or trusts the archive's own manifest. Different
content, ZIP metadata, profile or fallback fails verification. Keep the exact
code/catalog revision and the printed archive digest for review. Verification
requires that matching reviewed source checkout; there is no network lookup.
Source files are at most 256 KiB each, source total at most 1 MiB, and the complete
archive at most 1 MiB. Existing files and directories are never cleaned or replaced.

The manifest's import/native/server indicators are advisory starter-code hints.
Dynamic imports, notebook magics and future learner code can invalidate them.
The tool does not execute code or analyze every possible network/file operation.
All browser qualification gates remain **unqualified**. There is deliberately no
command-line switch to promote them to accepted.

Before a browser route can replace hosted time, qualify the exact runtime,
package versions, browser/device and course task with a bounded comparison:

| Gate | Required observation outside this tool |
| --- | --- |
| Execution and packages | Execute the real pandas workflow and independent checks in the target kernel; compare correct artifacts with the hosted baseline. Starter source alone is insufficient. |
| Storage and reopening | Save, close and reopen; exercise storage eviction/private mode, quota errors and recovery. Keep independent downloads. |
| Export and restore | Export all four required artifacts, import into a fresh browser/hosted environment and compare bytes. |
| Network and privacy | Inspect actual package/model downloads, telemetry, extensions and requests; authorize data destinations before using private data. |
| Accessibility | Test keyboard, screen reader, narrow screen, low bandwidth and an equally complete AI-off route with people. |
| Hosted fallback access | Confirm actual authorization, capacity, support and recovery; the supplied URL is not evidence. |
| Submission handoff | Use the existing authorized explicit selected-file preview/confirmation and receipt workflow after export. The server-side notebook bridges do not run inside this archive. |

The current hosted Python/R image remains necessary for native packages, R or
other unqualified browser workloads. Hosted availability itself still requires
the existing course acceptance gates. The same task and rubric must remain usable
without AI. Do not put operator answers, private tutoring, credentials or learner
data into the starter bundle to make a compatibility test pass.

Checks: `python3 -m unittest discover -s course-workspace/tests -p test_browser_bundle.py -v`.
Existing policy and selected-code tests remain required for application changes.

## Implementation checkpoint, 13 November 2026

Change record: this document; parent coordination remains the EduCloud classroom
full-stack spec and Wildcat resource implementation record. Scope is source bundle
preparation only. Reuse existing profiles and safe reading; add no registry,
authority, runtime, egress rule, provider or submission path.

Checkout: `/Users/gregchism/dev/.educloud-worktrees/waypoint-browser-notebook-bundle-v1`,
branch `codex/browser-notebook-bundle-v1`, base
`3ec286f5d41e857622d9b24f23b3c31e6f248605`. Changes are uncommitted and limited
to this guide, the bundle module/catalog, its tests, the course README link and
the diagnosed worker-runtime test correction described below.
Verification results are recorded below. No hosted CI, merge,
deployment, browser runtime qualification or institutional acceptance is claimed.

Next concrete action: operator review of the two fixed file/digest catalogs, then
a separately bounded actual browser-kernel comparison using public data, exported
artifacts and the maintained hosted fallback. No learner data or model calls are
needed to evaluate that next gate.

### Initial verification and retained failed gates

- New focused suite: 15/15 passed, including actual CLI build/verify, deterministic
  archives for both profiles, reviewed-source tampering, hidden/operator leakage,
  file/directory/ancestor symlinks, FIFO/size refusal, ZIP mutation/traversal and
  fixed unqualified states. Also passed within both full Python runs.
- Existing selected-code Node suite: 4/4 passed (Node 26.7.0).
- Full existing Python suite, using the already-prepared CPython 3.12.13 environment:
  **not green**. Original run: 757 tests in 343.626 seconds, 756 passed and one
  error in the unchanged composed-central-TLS three-second-budget control,
  `AuthorityError('authority_transport_unavailable')`.
- That single control passed when isolated (1 test, 3.201 seconds). One bounded
  full rerun then ran 757 tests in 250.403 seconds: 756 passed and a different
  failure in unchanged `test_stop_response_grace_never_extends_absolute_session_deadline`
  (`test_worker_runtime.py:221`, fake container still marked running). The original
  three-second-budget control passed in that rerun. The second failing control
  also passed when isolated (1 test, 0.740 seconds). Timing/load sensitivity is a
  plausible explanation, not an established diagnosis or a passing full gate.
  No runtime, transport or test-deadline code was changed; no further retries run.
- Python AST, catalog JSON, documentation whitespace and `git diff --check` pass.
  Focused new tests additionally passed on local CPython 3.14.5. No dependencies
  were installed or downloaded.

Full failed logs and isolated diagnostics are retained privately under ignored
`course-workspace/output/browser-bundle-validation-2026-11-13/`. Keep these failures
visible during review. The subsequent diagnosis and current local gate result
follow. The source-bundle tests do not establish acceptance of that unrelated
runtime. No browser or hosted CI gate is represented as passed.

### Session-expiry diagnosis and current local gate

Following authorization to continue toward commit and deployment, inspection
found a scheduling assumption in the existing 150 ms expiry test. WorkerServer's
absolute deadline begins before TLS, ACL, SQL and reverse authorization. Runtime
admission and `DockerBackend.remove` recheck authorization before Docker stop and
again before removal. Expiry before stop must therefore refuse with the container
still running; expiry during stop must retain the stopped container and refuse
removal. The old test assumed the second phase had been reached after only 150 ms
of wall time. Its running-container failure did not establish a production fault.

Only `tests/test_worker_runtime.py` changed: the existing after-stop control now
uses a module-scoped monotonic clock that advances after the fake stop effect.
A separate before-stop control advances that clock at the worker's dispatch
boundary and requires **zero stop/remove effects** with the container still
running. Both exercise actual TLS/SQLite and preserve all safety assertions.
The shared `time` module, real socket timeouts and all production code/deadlines
remain unchanged. The initial new fixture advanced time inside a client callback
and raced challenge-frame receipt (`worker_frame_invalid`); its failed log is
retained. Moving injection to the explicit worker boundary removed that ambiguous
test setup rather than changing protocol or accepting another result.

The composed central TLS/SQL three-second test remains byte-for-byte unchanged,
including all **25 × 100 ms real sleeps** and the actual three-second client
timeout. It passed focused in 2.864 seconds. Removing delays or extending its
deadline was explicitly rejected; the earlier transport error remains evidence
of performance sensitivity, not an established runtime defect or a new latency
guarantee.

Current verification: the after-stop and before-stop focused controls pass;
the single authorized post-diagnosis full run passes **758/758 tests in 94.183
seconds**, with zero skips/errors/failures, using the same existing CPython
3.12.13 executable at
`/Users/gregchism/dev/.educloud-worktrees/waypoint-classroom-roster-presenter-v1/.venv/bin/python`.
This includes all 15 bundle controls and the unchanged real three-second
composition control. Earlier Node selected-code checks remain 4/4 passed against
unchanged application source. Final diff checks pass. These results apply to the
selected source base above, not a later transplanted release branch.

All earlier failures, isolated diagnostic passes, the initial new fixture failure,
corrected focused results and final full-suite log are retained privately in
`course-workspace/output/browser-bundle-validation-2026-11-13/`. No further suite
retry, production edit, commit, push, release or deployment was performed here.
Next action: independent source review and parent-owned commit/CI/release work;
all actual browser qualification and institutional acceptance gates remain open.

### PR60 shared-runtime rehearsal correction

Exact-head CI at `30163640900ddacbbf7d701bd2ea3e14b2fa5315` found a separate
fixture fault: multiple-course, resource-amendment and cutover rehearsals each
built default notebook images twice, then asserted that the independently built
Hub/course IDs were identical. Both fixtures reached readiness, but the actual
image equality assertions failed. The three original CI logs remain retained in
ignored `course-workspace/output/browser-bundle-release-v1/`; they are not passes.

The rehearsal-only `Notebook.reuse_images_from` now takes the first successfully
started fixture's exact local Hub/course IDs, checks them against its frozen
manifest, and supplies them through the existing immutable no-build path for the
fresh second fixture. All three callers still construct both objects before
startup, preserving original DNS resolvers. The helper refuses incomplete/closed
sources, changed source manifests or image pins, attempted/configured recipients,
explicit image replacements and candidate-image mode. Only image pins are shared;
identity, credentials, storage, quotas, allocation and cleanup remain separate.
The second fixture still inspects its own actual image IDs and passes ordinary
startup preflight. Every existing real same-image assertion remains unchanged.
No production source, deadline, image build recipe or acceptance gate changed.

Focused hermetic checks: **8/8 passed**. Existing rehearsal unit suite: **333
tests, 325 passed and eight explicit skips**, zero failures/errors, in 36.842
seconds with the existing CPython 3.12.13 environment. Skips require Linux
process/root/mixed-ownership facilities or the optional Docker SDK. An initial
focused run refused legitimate normalized subject ordering during an overly broad
manifest comparison; that failure is retained. The corrected comparison binds
only the exact image pair against both source fields and the frozen manifest,
without copying identity state. Final AST/diff checks pass. No Docker, downloads,
commit or push ran here; local hermetic results do not replace a successful real
rehearsal. Next action is parent review, commit and exact-new-head CI for all
three original failures, retaining their evidence and remaining release gates.

### PR60 synthetic retention CLI correction

At `80303aff2da1a568c7ac7d911d4380cada515236`, the three corrected shared-image
rehearsals passed hosted CI. The remaining course-job failure was retirement's
synthetic `restic forget --keep-tag` command (job `132091766389`, installed Restic
0.18.1). The original failed log remains untouched at
`course-workspace/output/browser-bundle-release-v1/retirement-ci-failed.log`.
Restic's default `host,paths` grouping put the different-path unprotected control
in its own group; retaining only protected tags would remove that group's last
snapshot and is refused. See the [official grouping and retention semantics](https://github.com/restic/restic/blob/master/doc/060_forget.rst).

The fixture-only helper now explicitly uses `--group-by '' --keep-tag` against
only a local-test repository beneath this invocation's root. It refuses foreign,
nonlocal or ambiguous paths. No unsafe-remove flag is used. Protected snapshot
tag/ID checks, control-removal assertions, real integrity verification and closed
restore/export controls remain intact. Production `recovery.retention` and
`retirement.py` are byte-unchanged; their host/tag/count rules are unaffected.

A permitted new-temporary-repository probe with installed local Restic 0.19.1
reproduced the original exit 1, then verified corrected exit 0, protected snapshot
retention, control removal and `check --read-data` exit 0. The repository and
password were private and removed on context exit; no existing repository was
accessed. Regression checks then exercised the actual fixture helper with two
distinct snapshot paths, verified protected file bytes and integrity, and proved
that a missing protected tag still refuses whole-group deletion. Focused
retirement results: **9/9 passed in 10.822 seconds**, no skips. The existing
production-retention scoping/preview control also passed, **1/1**. AST and diff
checks pass. Local evidence is retained separately under ignored
`course-workspace/output/retirement-retention-fix-2026-11-13/`.

No Docker, download, production retention change, commit, push or release asset
change ran here. Local Restic 0.19.1 evidence is distinct from the still-pending
new-head hosted run with Restic 0.18.1. Parent review/commit and successful exact
new-head retirement CI remain required; the release candidate remains a draft.
