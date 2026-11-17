# Isolated OpenBao classroom bridge

Adoption follow-up: `OPENBAO-PILOT-ADOPTION.md`. The v1 results below remain
historical evidence at their stated pins. Current optional OpenBao execution
requires the new AppRole/recovery Portage pin; the ordinary key-file path is unchanged.

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


## Verified outcome — 2026-11-16

The isolated local trial passed. Tested application revisions are Portage
`2ac908615a6d306f7862622cf5fd8ff1eed0c1a1` and Waypoint
`6d44170806bbf7a1fba846fb341d8dfd9d737f3a`. This acceptance-record commit is a
documentation-only descendant; it does not change the tested application tree.
Exact component/image/environment identities and hashed raw-result locations are
in `openbao-local-trial-verification.json`. No changes were merged, deployed or
enabled in the working pilot.

- Real OpenBao: 22 controls passed, including rotation without gateway restart,
  least-privilege reads, denied writes/enumeration/escalation, token expiry and
  revocation, sealing, process restart, cold restore, audit and unknown-usage
  preservation. Six synthetic upstream transports; zero paid calls in this suite.
- Classroom browser: 13 stub controls passed, including light/dark/system,
  narrow keyboard navigation, expiry, saving, explicit four-file submission,
  wrong-answer grading failure, changed-preview refusal and receipt recovery.
- Live classroom: all eight live controls passed with actual notebook execution,
  a signed tutoring hint, saved-file submission and byte-exact receipt, actual
  pinned correctness checks, refusal controls and receipt recovery after restart.
  Three credential reads matched three actual model transports. Model:
  `openai/gpt-4.1-mini` through OpenRouter. SDK retries: zero; provider-internal
  retries are unobservable. Provider-reported cost: $0.0035964. No new unresolved
  usage; the historical $0.025 unknown liability remains recorded separately.
- Credential leak scan passed. Trial servers stopped and the service-token file
  was removed. The submission-receipt screenshot was also visually inspected.
- Portage: 595 tests passed, nine skipped; the three skipped Belay-environment
  tests separately passed. Six sibling-verifier tests remain skipped. Qualification
  regression: 63 passed; all three render configurations and changed-file Ruff passed.
- Waypoint: 769 workspace tests passed serially; rehearsal suite ran 353 tests
  with eight skips. These broad suites tested `5b570ad2e8af4b06a4a6357f29286dc07acdfeb0`.
  The final runtime adds only early Forgejo-image preflight; seven NHANES tests
  and both successful browser flows subsequently passed at `6d44170`.

## Recovery findings and retained failures

OpenBao 2.7.2 uses a single-node Raft backend here, correcting the earlier plan's
file-backend assumption. Initial backend failure, setup timeout, post-unseal
leadership readiness race and asynchronous ledger-finalizer timing failure are
retained in Portage `.qualification/openbao-run-01` through `openbao-run-04`.
They led to Raft configuration, a separate bounded setup timeout, bounded health
polling and a wait for durable ledger state; acceptance was not weakened.
Runs 05 and 06 passed. The intentionally failed synthetic provider request leaves
one unresolved reservation, rather than disguising unknown usage as zero cost.

Restoring a backup can restore old service tokens. The control explicitly revokes
the restored token and issues a fresh one before resuming. The independent Portage
ledger is not rolled back with OpenBao storage. Rotation exercised synthetic key
generations; the real OpenRouter key was neither rotated nor revoked.

Five failed classroom setup attempts and their logs remain under Waypoint
`output/openbao-trial/stub-workflow*`. Required local image objects disappeared
between commands; the cause was not established. A dedicated checkout image was
built from the unchanged Cairn source, saved to `checkout-image.tar`, and restored
immediately before evaluation together with the pinned Forgejo image. Successful
stub run 06 and live run 01 then passed. The old pilot image pin was not rewritten.
A concurrent workspace-suite timing failure is retained in `workspace-tests.log`;
the serial suite passed without increasing its timeout.

## Reproduction and boundaries

Use the frozen source pins above in isolated clean checkouts. Portage's `uv.lock`
is unchanged; its trial environment was built with `uv sync --frozen`. Python is
3.12.13. The browser driver, Belay and notebook environments were reused, with
exact installed-package inventories and reference lock hashes recorded in the
verification JSON; a fresh-machine reconstruction of those environments remains
unverified. Go was pinned to 1.26.11. OpenBao is the native darwin/arm64 2.7.2
release, binary SHA-256
`3e274a55364efb3f1f6af98f560b8b44e2b75a7d7c88527f564f827ae80c7e5a`.
Archive SHA-256 was checked against release metadata:
`2b4fc131910f4b075b2e1c9a9de67c461489491a8f03fbdc73387294d67d9547`.
Signature verification was not performed.

From the frozen Portage checkout, with that binary present, the unpaid control
suite can be reproduced with a new output directory:

```sh
uv sync --frozen
uv run --frozen python integrations/ai_pilot/openbao_trial.py \
  --bao .qualification/openbao-bin/bao \
  --output .qualification/openbao-control-rerun \
  --execute-local
```

Expected result: 22 passed controls, empty `failed`, `cleanup: true`, six synthetic
transports and one deliberately retained unresolved reservation. Do not reuse an
existing output directory or erase prior evidence.

The live orchestration entry point is
`integrations/ai_pilot/openbao_classroom.py`. It requires explicit execution,
private operator key-file reference, exact Waypoint pin, driver/kernel/Belay/Cairn
paths, kernel lock and both image IDs. It retains the existing seven-request,
$0.175 reserved ceiling. Consult the adjacent reproduction record for the exact
successful invocation. A passing historical result is not recurring spend
permission. No further live calls are required to accept this trial.

This is a local single-node TLS trial with read-only short-lived service tokens,
not production key custody, high availability, OS-user isolation, dynamic AWS
credentials or real-student acceptance. Operator root/unseal material exists only
in process memory; preserved encrypted test storage is not a production recovery
procedure. Before a pilot rollout, review the two branches, choose service identity
and recovery-key custody, and repeat deployment-specific rotation/recovery controls.
Human instructional review remains pending. The working pilot remains unchanged.
