# Local synthetic OIDC acceptance

This evidence advances the identity/access portion of
[issue 29](https://github.com/EduCloud-Ecosystem/waypoint/issues/29).
It does not qualify hosted operation, private/student data, source-environment
promotion, storage capacity, deletion, or an institutional service.

The final full local run passed 74 real browser/service checks in
`verification.json`. The backend used the existing reviewed Python/R images
and approved Outfitter source. No source candidate was enabled. `provenance.json`
records immutable image references, observed versions, broker hashes and all
18 application/static/recipe hashes. They match revision `b818d02` exactly.

| Control | Observed result |
| --- | --- |
| Four real browser identities | Owner/editor/viewer/outsider sign in; only current project membership authorizes access |
| Boundary and failure controls | Forged headers, collaborator keys, private routes, wrong audience, expired/revoked credentials and provider outage are denied |
| Real compute | Python and R complete through Outfitter with retained 30 CPU-second charges and verified cleanup |
| Active compute revocation | Exact owned workload was Running+Paused when the worker requested cancellation; its container was then absent, with no published metrics and its charge retained |
| Queued demotion | Role loss cancels before broker admission with zero compute charge |
| Post-execution revocation | A separately receipt-gated run suppresses publication and retains its charge; this is distinct from active compute cancellation |
| Provider outage | Worker denial occurs before recovery; a separate viewer session signs out locally during outage; the run stays denied after recovery |
| Local replacement | OIDC mode, charges and membership removal survive; old app/proxy cookies fail, including after fresh sign-in; fresh authorized export succeeds |
| Keyboard activation | Sign-in, project and arrow-tab selection, membership update, invalid-input error, reviewed run, download and local sign-out pass |
| Provider sign-out and visual supplement | Separate confirmation removes the exact current provider session identified by private introspection; desktop/mobile screenshots are inspected and the mobile page has no horizontal overflow |

`keyboard-evidence.zip` is the actual synthetic project export downloaded by
the keyboard control. Screenshots show synthetic records and opaque account
identifiers. No raw browser trace, authorization code, token, cookie, private
operator configuration or service log is published.

The credential control specifically searched for the current owner's real
access token/cookies and the generated client/boundary secrets in runs, broker
state, the decompressed export and four successfully retrieved service logs.
It is not a claim to have scanned every historical credential or browser trace.
Public artifacts are separately checked against the generated secret canaries.

## Exact revisions and retained failures

The full run used the `b818d02` harness. Independent review subsequently found
that a failed observer could still pause after cleanup, or skip unpause when
event teardown raised. The cleanup-only patch uses a stop signal and a shared
pause lock, guarantees an exact-owned unpause attempt, and verifies observer
termination. Two targeted regression tests pass, within 158 passing offline
controls. This later harness patch is not retroactively attributed to the full
browser run. Application image bytes are unchanged.
The generator later gained only the exact provider logout-confirmation route;
that route was applied to the owned stack and verified by the separate
`provider-logout.json` control. The same control is now in the reusable harness.

The original partial/failed reports remain under `failed-attempts`:

- Attempt a: the initial realm omitted `basic` scope, so required subject/time
  validation denied access. After that correction, a harness JSON-body defect
  stopped the recorded attempt at its thirteenth check, after 12 passing checks.
  Neither is a pass.
- Attempt b: 44 checks passed before a short-token control left a 30-second
  client lifespan in place; Keycloak preserves omitted attributes during update.
  A later observer consequently received 401. The final harness explicitly
  restores 600 seconds. Its earlier queued/publication checks are partial evidence.
- Attempt c and the first attempt d: application readiness preceded provider
  readiness and browser sign-in timed out after one passing configuration-outage
  check. The corrected harness checks discovery issuer and private proxy ping.
- The visual supplement initially used `document` in the automation execution
  context instead of `page.evaluate`. That read-only harness call failed; the
  corrected layout control is a separate pass, recorded in `supplemental.json`.
- Provider confirmation initially returned 403 because its exact path was not
  forwarded. That failed boundary observation remains in `provider-logout.json`.
  The corrected route and exact-session termination passed separately; the
  original 74-check run did not exercise provider logout confirmation.

`cleanup.json` records only the exact generated stacks and broker owner labels.
Replacement state contains the predecessor's charges; those copied rows are
not counted twice. Private state and images remain retained. There was no
shared Docker restart, broad prune, automatic deletion or parent-worktree edit.

The original 79 source-build evidence files, failed replay and unresolved
cleanup gate remain unchanged. The later resource/accounting observation in
the separate replay-cleanup-reconciliation directory remains supplemental;
it does not turn the failed workload into a success.

## Open gates

Manual screen-reader use, complete keyboard focus/tab order, a new independent
human walkthrough, intended hosted cohort/concurrency, store-wide byte/entry
and output admission, retention/deletion, service ownership/support, off-host
recovery and approved private-data classes remain open. Four source-built
environments remain disabled candidates. No claim of scientific validity is
made for these synthetic descriptive summaries.
