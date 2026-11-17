# OpenBao local instructor adoption

Greg authorized service identity, operator custody, integration when ready and a
fresh classroom verification on 2026-11-16. Companion Portage application pin:
`124da40b12e6422a0106181c14a60c622ec53927` (`codex/openbao-local-trial`).
This Waypoint change only binds the existing optional credential tuple to that
reviewed AppRole/recovery implementation. No classroom authority, UI, submission,
grading, expiry, inference retry or spend gate changes. The original static-key
Portage pin remains `b72881f009e61e1311b6e46b92b42841991fd163`.

Portage now provisions the named `portage-classroom` role, revokes bootstrap root,
and obtains its exact-path service credential through a short-lived single-use
wrapped handoff. The operator recovery package is outside all runtime output and
is never passed to the notebook, tutor or gateway. Cold restore revokes old service
tokens and pending SecretIDs before re-enrollment, without rolling back accounting.

Greg selected an institutional password manager and separate backup. Actual import
and independent backup confirmation remain human steps; the staged import file is
owner-only plaintext, not an encrypted vault. Persistent/default rollout remains
closed until custody is confirmed. Hosted student use additionally needs separate
OS service identity, certificate lifecycle and selected-host acceptance. The local
fixture remains synthetic and uses the operator's OS account.

Run the Portage `integrations/ai_pilot/openbao_classroom.py` entry point with the
existing exact source/environment/image arguments and a new `--custody-export`
path in an owner-only directory outside all repos and runtime output. The run is
finite (seven model requests/$0.175 reserved ceiling), retains failed work, tests
actual post-workflow cold recovery without additional inference, and stops its
owned services. A run result does not authorize automatic repeated paid calls.

Earlier evidence: `OPENBAO-LOCAL-TRIAL.md`. Results are recorded below. No deployment, production secret import or student admission occurs.


## Verified local results

The optional integration passed at the application pins above: 20 actual OpenBao
identity/custody controls, 13 unpaid browser controls and eight live classroom
controls. Portage regressions: 599 passed, nine skipped (six missing sibling
verifier integrations and three optional Belay-environment tests); five focused
Waypoint tests passed. Earlier broad Waypoint suites remain historical evidence,
not a claim of rerunning all of them for this pin-only companion change.

The live run made three actual model requests with zero SDK retries and
$0.0035944 provider-reported cost. Provider-internal retries are unobservable.
No new unknown usage remained; the historical $0.025 liability is still retained.
The NHANES fixture scored 4/4 and the deliberate wrong-output control failed.
Four saved files matched the repository receipt, which survived restart. The
receipt screenshot was also visually inspected.

After the classroom workflow, the actual Bao store was cold-restored. A fresh
operator process recovered it, invalidated the retired service token and verified
the provider credential without further inference. The independent accounting
file stayed byte-identical. Three classroom credential reads are recorded
separately from one denied retired-token read and one successful recovery read.
The credential/recovery-material leak scan passed; all owned services closed.

An initial unpaid browser run failed because Docker stopped during repository
preparation; cleanup could not finish then. Docker was started again, only the
failed fixture's labeled resources were removed, and Cairn's recovery inventory
verified zero remaining resources. The original failure remains unchanged beside
a separate successful cleanup-recovery record. No paid work was replayed.

Full sanitized evidence, source/environment/image pins and raw-result hashes are
in the verification JSON. Reproduction uses the same entrypoint/arguments as the
prior run plus `--custody-export`; the exact successful command is retained at
`output/openbao-trial/adoption-live-command.sh` in the Waypoint worktree. It is a
record, not standing permission for another paid run.

See [the exact invocation](OPENBAO-PILOT-REPRODUCTION.md) and
[verification record](openbao-pilot-adoption-verification.json). The private
operator handoff is at
`/Users/gregchism/Library/Application Support/EduCloud/OpenBao Recovery/classroom-adoption-01/HANDOFF.md`.
It belongs to this local rehearsal; a future hosted instance needs its own escrow.
