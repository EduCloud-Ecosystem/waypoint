# Broker upgrade qualification evidence

This packet qualifies a candidate on disposable synthetic state. It does not
adopt a broker pin or change an accepted workspace. Baseline Outfitter is
`5827c84e9952cc67b85e91ceff4282e0ead6923b`; candidate is accepted PR #16 merge
`5446438985e31cb739f2048a2aa2176918831a7e`, tree
`b65c2c381a282f221e0cb8acfa2a47bf8b3fca4e` (the same tree as reviewed
`28d34437e61a7bfe50fbfafcabc656a7f09fceec`).

[verification.json](verification.json) records **45 passing named controls and
seven execution attempts**, all with verified broker cleanup and a conservative
charge of 30 CPU seconds each: **210 charged CPU seconds total**. These are
reserved upper bounds, not measured CPU time or monetary costs. Two attempts used
the baseline; five used the candidate. No execution retry occurred. The existing
reviewed local Python/R images were inspected and used; none were pulled.

The actual run used Waypoint implementation commit
`89084a70f7341fc11054b9d620d858c214787ced`. The verifier
records exact runtime and harness hashes and both source/binary identities.
Both brokers were compiled from the complete pinned production Go file sets
with module/toolchain downloads disabled. Native Go build metadata on this host
omits VCS fields even when requested; the preparation helper therefore retains
explicit Git-source and binary manifests. Those hashes are integrity evidence,
not independent executable authenticity or bit-identical build proof.

The following public synthetic bundles independently verify:

- [Ordinary replacement](ordinary-after.zip): original completed Python run,
  queued cancellation, candidate replacement run and retained charges.
- [Guarded replacement](guarded-after.zip): original completed R run, queued
  cancellation and candidate replacement run.
- [Fresh bounded-export v2](bounded-export.zip): actual Python and R results
  through the production Worker/Store/export paths. This is not HTTP/browser,
  concurrency, hosted identity or class-size acceptance.
- [Diagnostic failure retained](bounded-export-with-diagnostic-failure.zip):
  an owned output-directory obstruction caused the seventh run's diagnostics
  to be unavailable. Its full charge and verified cleanup remain; Waypoint
  records failed independent validation and publishes no result metrics.

Synthetic unknown/v3-native/v4-consumer ledgers were seeded explicitly after
real CLI initialization. These fixtures prove decoder/status/held-restore and
refusal behavior, not provider creation, settlement or consumer execution.
Unknown local reservations and one native-unit liability remain in those private
fixtures; the seven-attempt cleanup result does not erase them or claim their
provider cleanup. Missing diagnostics refuse complete checkpoint publication
while retaining accounting visibility. Native and consumer state remains outside
Waypoint's adoption contract; no state was stripped or downgraded.

## Subsequent harness-only correction

An additional negative test found that resolving an entire output path followed
a dangling symlink and created its target. The retained
[red log](pre-review/dangling-output-failed.log) contains two assertion failures
in one of eight tests. The original
[verifier](pre-review/verifier-before-fix.py.txt),
[builder](pre-review/builder-before-fix.py.txt) and
[negative test source](pre-review/test-before-fix.py.txt) are preserved.
Both entry points now resolve only the existing parent and exclusively create
the final component, refusing both existing and dangling symlinks.

An additional interruption control found that a KeyboardInterrupt after all
checks but before final artifact publication could leave a PASS report while
the CLI exited unsuccessfully. Its [red log](pre-review/interruption-failed.log),
[verifier](pre-review/verifier-before-interruption-fix.py.txt) and
[test](pre-review/test-before-interruption-fix.py.txt) are retained. The outer
qualification boundary now records interrupts as failures before bounded cleanup.

The corrected harness passes [nine focused controls](focused-tests.log) and
the [same nine controls under optimized Python](focused-optimized.log). Counts
overlap; do not add them. No Docker run was repeated after this destination-path
and failure-reporting corrections. The 45-control execution report remains attributed to its original
source; final preparation and interruption behavior is established by the focused delta
tests, not silently relabeled as a later real-broker run.
The final preparation helper was also executed again with downloads disabled;
[preparation-verification.json](preparation-verification.json) binds those two
new build outputs and records zero Docker attempts. Their hashes differ from
the workload-tested binaries because the source/build paths differ; no
bit-identical rebuild claim is made.

## Limits and preservation

Private state, build directories, credentials, raw CLI observations and
checkpoint contents are retained locally and excluded from Git. Public artifacts
contain only synthetic study results and bounded evidence. All prior evidence,
catalog/image files, frozen capacity/#41 qualification archives and active CI
broker refs remain unchanged. The focused tests are automatically discovered by
the existing controls job; this PR adds no workflow or broker-pin change.

Finite restoration still refuses. No physical storage quota, all-writer finality,
deadline under full-scale ledger pressure, external-host recovery, native/cloud
execution or real-user acceptance is established. Later I/O orphans and watchdog
limitations remain. Any active-pin adoption is a separate reviewed change.
