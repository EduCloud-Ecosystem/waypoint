# Finite logical-authority acceptance

This record qualifies fresh disposable local-key authority limits. It does not
qualify storage quotas, finite-profile restore/OIDC, disk fit or real-data use.
The source hashes in [acceptance.json](acceptance.json) identify this increment;
PR39's contract and evidence retain their original bytes and source meaning.

The local suite passed 317 tests, including 21 new finite-profile controls and
one bridge that executes eight frozen v1 capacity controls. These counts overlap
and must not be added. [suite.log](suite.log) retains the raw local output.
[finite-controls.log](finite-controls.log) records the final 21 controls, including
the corrected out-of-contract receipt assertion. [encoding.json](encoding.json)
records the conservative request/receipt size rationale.
JavaScript syntax and whitespace checks pass. All 182 existing October 17
evidence files match base `599ee0ce23942bbfc578124af12285484c357157` exactly.
Exact-head CI and independent review are recorded separately in the PR.

Large row boundaries are synthetic historical fixtures seeded in existing
tables. Final transitions use production Store methods; this is not execution
of 1,000 provider runs. Controls exercise revoked/closed/failed history, final
slot competition for users, per-subject sessions, projects, memberships and runs,
invalid/repeated sign-ins, existing sessions, membership insertion/demotion,
project owner-slot serialization, conservative accounting, encoded JSON limits,
pre-effect refusal, and checkpoint preservation/refusal.
The escaped receipt test deliberately bypasses the real collector's 262,144-byte
ceiling through a mock; production would reject its roughly 720 KiB input earlier.
It tests serializer defense in depth and retained uncertainty, not a reachable
accepted receipt overflow.

The combined safety exercise starts with all row limits full, then performs
logout, demotion/removal, rotate/revoke, cancellation and conservative settlement.
Earlier actions free some session/membership capacity. It is a sequential
exercise, not independent proof of every action with every cap simultaneously
full. Separate per-cap tests cover their stated boundaries. Provider effects
are simulated; new finite-profile browser, real-provider and human acceptance
remain NOT RUN. Existing ordinary CI Docker/recovery checks are compatibility
coverage for existing profiles.

## Retained review failures

[pre-fix/review-regressions.log](pre-fix/review-regressions.log) and the six
[pre-fix source files](pre-fix/source/tests/test_finite_authority.py) are preserved
unchanged. Two tests produced 15 failed subtests and three errors:

- Keeping config/guard/SQLite profile markers intact while deleting, retargeting
  or duplicating the database authority allowed reopening and some safety writes.
  The correction requires exactly one matching database authority before access.
- A trusted-digest, internally hash-consistent synthetic checkpoint with a finite
  SQLite marker and stripped config markers reached direct guarded destination
  creation or activation locking. All four restore/cutover entry points now
  reject through the same config/database preflight before those effects.
- The three cancellation errors were AccessError after earlier revoke changed
  the shared fixture. They are sequencing errors, not independently demonstrated
  cancellation bypasses. The final regression uses a fresh fixture for each
  mutation/action and checks unchanged database, guard and config after refusal.

The original red witness is not overwritten by the corrected test. Earlier
exploratory failures and their limitations are listed in acceptance.json; raw
logs available from those runs are retained in pre-fix. Intermediate exploratory
source trees were not frozen, so these logs make no exact-replay claim. The
first archive omitted a required historical recipe; the final 43-file archive
includes its exact Git bytes and the original eight controls pass.

## Preservation and remaining gates

The [profile contract](../../../FINITE-AUTHORITY.md) describes all limits and
refusals. The existing v1 contract/harness and 182 prior evidence files are
unchanged. The original v1 test bridge is preserved inside the archive while the
current bridge verifies and runs that frozen source. The fifth headroom failure
remains FAIL, writer/SQLite/broker/destination peaks remain UNKNOWN, and
`quota_enforced:false` remains explicit.

All four source-built environments stay disabled. Original replay/cleanup
failures and the later full 30 CPU-second reconciliation remain separate.
No accepted-state migration, new history deletion, journal/default change,
provider call, Docker restart or work in Belay/parent pilot worktrees occurred.
The manifest lists file hashes for integrity, not independent authenticity;
retain the accepted Git commit or manifest digest separately.
