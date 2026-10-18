# Bounded export acceptance evidence

The fresh opt-in export profile closes database readers before artifact I/O and
ZIP construction, then compares current authority and the exact private manifest
before returning bytes. HTTP streaming holds no export reader or guard lock.
The [implemented contract](../../../BOUNDED-EXPORT.md) states the authorization
point, version bindings and limits. This is disposable local qualification, not
a live provider, accepted-store, physical-capacity or real-data acceptance.

## Accepted checks

- [Full suite](suite.log): 334 tests passed, including the 16 new
  [export tests](export-tests.log). JavaScript syntax and whitespace checks pass.
- [Independent review](independent-review.json): no remaining actionable code
  findings; reviewed source hashes are recorded. It reran the 16 export tests,
  21 finite v1 tests and both frozen bridges, plus
  [13 supplementary controls](independent-controls.json).
- [Negative controls](independent-negatives.json), with
  [raw output](independent-negatives.log), detected a leaked capture reader,
  omitted final authority check and omitted private `cancel_requested` comparison.
  The tests fail when these defects are injected; those expected failures are
  evidence of control sensitivity, not candidate failures.
- [Supplemental checks](supplemental.json) verified fresh CLI initialization,
  reopen/local sign-in, required-runtime refusal before creation, and changing
  artifact refusal during a descriptor read. These were executed inline; no
  exact supplemental-script replay claim is made.
- The [frozen #41 bridge](frozen-bridge.log) passed. All 39 archived files match
  base `1e4b04c2d07dd4730d904fe93acb1795ad1a4d1c`; all 209 pre-existing evidence
  files, catalogs and image pins are unchanged.

Counts overlap: the full suite includes both frozen bridges and the new export
tests, and independent review reruns selected tests. Do not add these counts.
Exact-head review confirmation and CI are recorded separately in the PR.

The tests use production Store/HTTP/export paths with fresh temporary state and
simulated project allocation. Concurrent checks block artifact and ZIP phases
while real Store revoke/rotate/cancel/settle operations complete. The cancel
case changes the private flag while the public run payload remains running.
The HTTP check revokes after final authorization but before streaming and
confirms the already-authorized archive can finish without holding a reader.
No broker subprocess, Docker or live provider is used by these new controls.

## Original failures retained

- [Initial descriptor finding](pre-review/first/close-finding.json): an injected
  close acknowledgement error stopped the cleanup loop, leaving four other
  descriptors open. The original source and its hashes are preserved under
  `pre-review/first/source`. The fix attempts every close once, retains the first
  failure and refuses publication. It does not retry an ambiguously closed FD.
- [First test run](pre-review/first/suite.log): two test assertions expected a
  nonexistent verifier `status` field. A further
  [incorrect `format` assumption](pre-review/second/suite.log) also failed.
  The verifier returns `valid`; these were test errors, not rejected archives.
  The first run also rediscovered 21 imported finite tests; the final test
  module imports their module instead of exposing their class to discovery.
- [Fourth run](pre-review/fourth/suite.log): the session-revocation assertion
  was accidentally placed in the descriptor-close test. Moving it back to the
  HTTP revocation case corrected the fixture. The separate
  [wrong-directory invocation](pre-review/fourth/wrong-directory.log) is retained
  as an entrypoint/import error, not a runtime defect.
- [First frozen-archive attempt](pre-review/frozen-import/failure.log) omitted
  `test_recovery.py`, a transitive fixture import. Its incomplete archive and
  original bridge are retained. The accepted archive adds that unchanged base
  file; original #41 source, contract, harness and evidence bytes are unchanged.

## Remaining boundaries

Final equality proves current exact state, not detection of an intermediate
membership change followed by reversal. Files changed or removed after verified
copy cannot change those copied bytes. Unknown liabilities and full conservative
charges remain untouched. Failure produces no archive, intent, accounting
mutation or automatic retry. Raw errors cannot disclose private comparison
fields, keys or filesystem paths.

Version bindings are exact; v1/default behavior, OIDC refusal and all finite
restore/cutover refusals remain. Source candidates stay disabled. Capacity is
UNKNOWN, quota admission remains NO-GO, `quota_enforced:false`, and fifth-headroom
FAIL remains preserved. No WAL/memory/time maximum, engine ceiling, checkpoint,
scratch reuse, deletion, migration or provider qualification is claimed. Belay,
parent/rejected worktrees and accepted state are outside this change.

The manifest binds current source and retained evidence. Hashes provide
integrity, not independent authenticity; retain its digest or accepted Git commit
separately. Failed evidence is not overwritten by passing reports.
