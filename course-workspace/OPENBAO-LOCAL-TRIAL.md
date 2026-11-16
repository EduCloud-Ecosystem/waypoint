# Isolated OpenBao classroom bridge

This branch adds an explicit credential option to the existing finite synthetic
NHANES evaluator. It is not enabled in the instructor launcher or working pilot.
Base: `de701887e06c3ae19292b05639a6c9e83ba53d3c`; branch:
`codex/waypoint-openbao-local-trial`. Companion Portage implementation:
`2ac908615a6d306f7862622cf5fd8ff1eed0c1a1`, `codex/openbao-local-trial`.

The new option is the complete tuple `--openbao-address`, `--openbao-ca`,
`--openbao-token-file`. It requires explicit live-mode approval, the exact trial
Portage source and a clean tracked checkout. It cannot accompany `--env-file` or
be used in stub/no-AI mode. The original Portage pin remains required for the
original key-file path. All existing question, authority, retry, budget, grading,
receipt and cleanup controls remain in place.

Run through Portage's `integrations/ai_pilot/openbao_classroom.py`, which creates
the disposable TLS OpenBao service, imports the existing operator test key
without displaying it, supplies only service-connection references to Waypoint,
records secret reads and closes OpenBao afterward. Never pass the vendor key to
the browser, notebook kernel or Belay. Seven actual provider transports and
$0.175 reserved is the unchanged maximum, not permission to repeat an unknown run.

Acceptance: the existing full NHANES browser evaluator must pass with live
Portage; audit credential reads must equal actual model transports; logs and
artifacts must not contain the raw vendor key or OpenBao access tokens. This is
local synthetic instructor evidence, not student deployment or security review.

Ownership: the app worktree helper stalled without returning a worktree; a
separate Git worktree was created at
`/Users/gregchism/dev/.educloud-worktrees/waypoint-openbao-local-trial`.
No original Waypoint, Cairn, Belay or instructor-launcher source was changed.
