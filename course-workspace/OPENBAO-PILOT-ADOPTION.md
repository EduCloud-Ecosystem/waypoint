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

Earlier evidence: `OPENBAO-LOCAL-TRIAL.md`. New results will be recorded here after
verification. No deployment, production secret import or student admission occurs.
