# Fresh operation-guard acceptance

This record accepts only the explicit fresh disposable local profile. It records
235 passing offline controls, nine real local browser/HTTP checks and twelve
local Docker/provider checks. The browser provider is simulated; the provider
report separately exercises actual reviewed legacy Python/R runtimes and v2
replacement. No hosted deployment, real-data acceptance, quota enforcement or
accepted-state migration is claimed.

The [contract](../../../OPERATION-GUARD.md) defines authority, uncertainty,
recovery and the remaining headroom boundary. `verified-source-sha256.json`
binds the application, tests and acceptance scripts used for the final local
checks. Git and exact-commit CI provide the release identity separately.
`sha256.json` covers every public payload in this directory except itself.
Private state, checkpoint authority and credentials are intentionally excluded.

The screenshot shows the paused form with editable input and a disabled Start
run button, after a cancellation request. The browser checks also verify stable
project idempotency after a normal initialization overlap and successful sign-out.
This is automated acceptance, not a manual screen-reader or focus-order review.

## Retained incomplete attempts

- Attempt 1 passed four checks, then the harness attempted to select Cancel
  inside a collapsed run panel and timed out. The panel is now expanded before
  selecting its action.
- Attempt 3 passed three checks, then normal UI polling observed the injected
  hold before the harness could click Start run. The fixture now injects the
  fault inside its test-only request handler immediately before submission,
  making the server-refusal draft check deterministic.

Both original INCOMPLETE reports remain here. They are not counted as passing
product acceptance. Final browser attempt 6 passed all nine checks; final
provider attempt 3 passed all twelve checks. Prior local disposable outputs are
retained outside Git and no accepted pilot store was used or modified.

## Preserved boundaries

All 127 previously tracked evidence files match accepted base
`640fa2d89d9e98be0d0ff8f0fbaae32e83fa74ec`. The prior-evidence manifest records
their hashes. The three files in the rejected dirty draft and its frozen patch
also retain the hashes in `acceptance.json`. The first four frozen failure
witnesses have new passing regression coverage; their original observations
remain unchanged. The fifth consumed-headroom witness remains blocked.

All four source candidates remain disabled and the environment catalog is
byte-for-byte unchanged. The original failed Documents replay and cleanup gate
remain documented, together with the separately recorded later cleanup
observation. Neither record is rewritten into a successful original replay.
Belay and the parent pilot worktrees remain outside this slice.
