# Testing acceptance worksheet

Package version: `educloud-testing-v1`, issued 2026-10-11. Copy this worksheet to
private operator records for each candidate. Unchecked boxes are pending, not
implicit passes. Commands live in [RUNBOOK.md](RUNBOOK.md); historical results
live in [VALIDATION.md](../VALIDATION.md). A prior green run does not accept a new
binary, dashboard bundle, image or deployment configuration.

## Record the candidate and owners

| Required record | Fill before the relevant stage |
| --- | --- |
| Run ID, UTC window, operator and instructor | |
| Cairn / Waypoint full commit IDs; source clean or dirty | |
| Cairn / simulator binary hashes; dashboard digest; image IDs | From bounded PASS artifact |
| Scope | local-subset / full-linux / hosted-synthetic / Brightspace sandbox |
| Worker owner, SSH alias, architecture, CPU/RAM/disk | Private inventory; no passwords or private keys |
| Source and replacement worker failure domains | Separate workers required for hosted recovery acceptance |
| Backup destination owner, repository reference, key custody | Private configuration path; never paste credentials |
| Requested RPO / RTO and rollback owner | Set before measuring; no defaults implied |
| Identity administrator, issuer/client reference, approved roster | Private inventory; immutable subjects, no learner names in public evidence |
| LMS administrator and sandbox course reference | Required only for real Brightspace stage |
| Review decision and evidence location | GO / NO-GO / pending, reviewer and date |

## Stage A: local synthetic rehearsal

Budget one operator session after dependencies/images are available. This is a
planning allowance, not a measured duration or provider commitment.

- [ ] Build reviewed Cairn dashboard, server, simulator and checkout image using
  RUNBOOK commands; prepare Waypoint dependencies and course image.
- [ ] Run `cairn_journey.py` into a new private output directory. Require zero
  exit and a bounded `PASS.json` with `scope=local-subset`, all required phases
  and `cleanup_verified=true`; inspect source revisions and dirty flags.
- [ ] Confirm real private-repository checkout, signed push grading, replay,
  two-learner isolation, reviewed feedback/proposed-score approval, simulated
  passback and server restart persistence are represented by the required phases.
- [ ] Review the 5/15 distinct-user HTTP observations and available sampled
  process memory/backlog metrics. They do not measure 15 concurrent notebook
  servers or grading jobs. SQL query counts are not instrumented.
- [ ] Review browser accessibility/keyboard evidence when produced by this
  candidate. Automated scans do not establish screen-reader usability.
- [ ] No secret/raw-log upload; no invocation-owned resources left. Dirty-source
  results are development evidence until a clean candidate is rerun.

**GO:** all required evidence passes for this exact candidate. **NO-GO:** missing
or stale artifact, failed isolation, wrong learner association, unexpected grade
write, leaked secret or incomplete cleanup. Stage A permits Stage B testing; it
does not admit students or certify a hosted deployment.

## Stage B: disposable Linux rehearsal

Allow a second session for Linux setup and diagnosis; image downloads/builds may
dominate. Use a disposable runner with root and no real coursework.

- [ ] Run `verify_course.py` as documented; require `scope=full-linux` and all
  local plus notebook phases. Verify Python/R, saved-file survival across actual
  notebook stop/resume, learner isolation, revocation and bounded admission.
- [ ] Check bounded load semantics: two admitted notebook servers, a refused
  third, then successful admission after capacity is released. Do not extrapolate
  this to a class-size commitment.
- [ ] Run the separate `verify_institutional_recovery.py --output NEW_DIRECTORY`
  rehearsal. Confirm current-roster restoration, removed-user refusal, old Hub
  session rejection, remaining-user saved files/Python/R and quota preservation.
- [ ] Require final success artifact only after all owned containers, volumes,
  networks and both original/replacement loop mounts are absent.
- [ ] With a screen reader, test navigation, labels, errors, review/approval,
  feedback and grade-delivery status. Test 200%/400% zoom, narrow/mobile layout,
  visible focus and keyboard-only completion. Record browser/AT versions and
  human observations; unresolved barriers in essential flows block user testing.

**GO:** successful candidate-specific Linux and recovery evidence plus human
accessibility review for the intended testers. Same-worker institutional recovery
is not independent-worker or real off-host backup acceptance.

## Stage C: hosted worker and off-host recovery

Plan one to two working days **after** infrastructure and administrator access
are supplied, with time for an actual scheduled backup and reboot. Use synthetic
accounts initially. Follow [HOSTED-PILOT.md](../HOSTED-PILOT.md),
[RECOVERY.md](../RECOVERY.md), including its worker-render/doctor instructions.

- [ ] Assign source/replacement worker, backup and incident owners; record RPO/RTO.
  Review worker-rendered configuration before provisioning. Supply dedicated
  quota-backed storage, TLS/DNS and the approved identity client privately.
- [ ] Run `worker.py doctor` on the actual worker. Test real root/user subdomain
  TLS, kernel WebSockets, allowed/denied login and active-session revocation.
- [ ] Reboot and verify mounts/quotas before admission. Test certificate renewal
  through the actual deployment's renewal mechanism.
- [ ] Configure the actual remote Restic repository and independent key custody;
  run init/backup/snapshots/check. Record the exact snapshot ID and successful
  backup time. A local repository or a second folder on this host does not pass.
- [ ] Restore onto the separate replacement worker using current independent
  identity/roster configuration. Keep the original Hub stopped during cutover.
  Verify saved-file hashes, Python/R, quotas, removed-user denial and re-login.
  Measure actual recovery time and recoverable data age against recorded RTO/RPO.
- [ ] Enable reviewed backup/health timers for the correct deployment directory;
  observe a scheduled success and controlled failure/staleness detection. Assign
  an operator to receive/check health results; no paging service is implied.
- [ ] Exercise the intended small-group workload on this exact worker, recording
  errors, latency, memory, disk/inodes and queue age. Set an initial admission cap
  from these results, not the local HTTP-user count.

**GO:** every check above passes on the selected infrastructure, owners accept
measured recovery objectives and rollback is feasible. Otherwise keep enrolment
closed. Do not substitute a successful synthetic Linux run for this stage.

## Stage D: Brightspace sandbox and instructor acceptance

Schedule a joint administrator/instructor session after tool registration. Use
Cairn's [Brightspace handoff](https://github.com/EduCloud-Ecosystem/cairn/blob/main/docs/brightspace.md)
and [testing readiness](https://github.com/EduCloud-Ecosystem/cairn/blob/main/docs/testing-readiness.md).

- [ ] One sandbox course, one numeric grade item, one instructor and two synthetic
  learner accounts; explicit Cairn classroom allowlist and immutable bindings.
- [ ] Top-level signed launch and correct learner mapping; tampered, expired,
  replayed and unrelated-course launches denied.
- [ ] Instructor-reviewed grade and numeric zero appear for the correct learner
  in the Brightspace gradebook and read back correctly. Pending/unassessable
  work and unapproved model proposals do not send grades.
- [ ] Controlled uncertain delivery, stale grade version and a conflicting LMS
  edit stop unsafe retry/overwrite. Reconcile against the gradebook before retry.
- [ ] LMS administrator and instructor sign the result. Real feedback quality
  still requires each instructor's separate calibration and adjudication records.

Stage D is optional for a notebook-only component test; it is mandatory before
promising real LMS grade delivery. No automated pilot pass promotes calibration
or authorizes autonomous grading.

## Stop, rollback and triage

Stop new admission and grade sends for any wrong-user access, identity ambiguity,
quota bypass, unrecoverable files or uncertain grade delivery. Preserve private
state and receipt/snapshot IDs. Do not delete evidence, clear identity bindings,
force-retry scores or run daemon-wide Docker prune.

For a fixture failure, inspect only invocation-owned resources and mounts; clean
those resources and rerun into a new directory. For a hosted failure, stop the
replacement before returning traffic to the original stopped worker; inspect
writes made since cutover before choosing which copy is authoritative. Do not run
two Hubs against the same homes. Database rollback uses a verified pre-upgrade
backup in a fresh database with the corresponding application version, not an
assumed downgrade of migrated state.

Use this issue template privately; publish only a redacted summary:

```text
Run/package version and exact component revisions:
Scope, UTC time and failed step/check ID:
Expected / observed behavior and reproducible synthetic steps:
Impact: access/identity | data/recovery | grades | availability | usability
Affected synthetic accounts/resources (private references only):
Evidence: bounded artifact or minimal redacted excerpt; no tokens, URLs with credentials, raw DBs
Cleanup status and resources still owned by the run:
Containment/rollback taken; any uncertain writes:
Owner, next action, retest candidate and explicit GO/NO-GO decision:
```

Security/identity errors, data loss, incorrect grades and essential accessibility
barriers block the affected pilot scope. Cosmetic or nonessential issues may be
tracked with an owner and explicit acceptance; missing evidence is always pending.
