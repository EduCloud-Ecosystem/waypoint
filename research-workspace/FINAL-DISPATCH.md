# Final dispatch authority correction

PR37 head `68fe54960d9d3e56b39bd6aa51f21728f532b4c9`, merged as
`a58001b2731fa0dc9a1150b65c8940bf656625ec`, had a gap between the worker's early
local-authority check and broker spawn. A cancellation, local user revocation or
project closure could commit after request.private.json was written and still
be followed by a launch attempt. PR37's passing checks did not cover that
boundary and must not be treated as acceptance of it.

The [original independent witness](evidence/2026-10-17/final-dispatch/pr37-red/README.md)
is retained unchanged, including its 22 immutable source files. Three earlier
denial controls pass and three final-boundary cases fail. Every launch is
intercepted; zero real provider processes run. Its exit-zero reproducer confirms
the known failure, not product acceptance. The witness manifest SHA-256 is
`28eae870ef94d3d257508289324991c01c4b839875024c517ceef900b39aecab`.
Its observation SHA-256 is
`880dc49971e429d320c3c52292c725a85d8f9d2491617ed65cfd35a1fa374b8d`.

## Corrected boundary

`Store.launch_run` places the current running row, cancellation, active local
user, editor/owner membership, project status and expiry check inside the final
spawn fence. Guarded stores acquire the existing guard before SQLite's write
reservation; legacy stores use the same SQLite writer ordering. Local OIDC
publication follows SQLite, then current local checks run before Popen. A wait
for the identity lock cannot leave an earlier expiry observation in use.
Remote identity checks and provider waiting remain outside these locks.

A denied current run becomes cancelled/not_admitted with zero compute only when
no spawn was attempted. A superseded terminal or interrupted row is preserved;
it cannot be overwritten using the stale worker's zero-cost refusal. Held or
retired authority cannot launch. Any exception after a possible launch remains
uncertain. Controls that commit after launch remain usable during provider
waiting, prevent result publication and retain receipts and conservative charges.
Every worker settlement also compares the current row under its own transaction,
so recovery or a completed run cannot be overwritten in the gap after refusal.
This applies to early identity, integrity and cancellation refusals as well.
The worker owns the process handle as soon as Popen returns, before database
cleanup. If fence rollback or close fails, it tears down the CLI observer outside
the locks with a five-second wait bound and closes both pipes. This is not proof
of provider cleanup and does not clear unknown liability.
No capacity policy, quota, schema change or accepted-store migration is added.

## Verification contract

The focused `tests/test_final_dispatch.py` controls cover cancellation, revocation,
closure, membership removal/demotion and expiry at the last boundary, across
legacy and guarded profiles. Concurrent controls exercise both commit-before-
launch and launch-before-commit, including expiry during identity-publication
waiting. Current-row recovery/settlement, held/retired modes, postlaunch rollback
and close failures, unknown liability, retained result/receipt/charge behavior,
and legitimate public/OIDC execution are checked separately.

Run the focused checks from the repository root:

```sh
PYTHONPATH=research-workspace:research-workspace/tests python3 -W error::ResourceWarning -m unittest test_final_dispatch -v
```

The full research-workspace control suite and actual Docker/provider CI must
pass on the independently reviewed final commit before merge. The frozen red
witness must continue reproducing its original failures. Simulated boundary
tests and actual Docker happy-path acceptance are separate evidence; neither
establishes hosted, real-data or manual accessibility readiness.

All four source candidates remain disabled. The original cleanup gate, rejected
admission draft and fifth headroom witness remain unchanged and unresolved.
Belay, accepted pilot stores and parent pilot worktrees are outside this change.
