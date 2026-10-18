# Bounded guard publication candidate evidence

The corrected fresh v3 candidate passes 368 automated controls, including 34 new guard
publication tests and the existing 334 controls. One new test also reruns the
14 applicable bounded-export controls on v3. Providers are simulated in the new
controls; competing processes, process death, files, locks, SQLite and local HTTP
are exercised directly in disposable temporary stores. JavaScript syntax and
whitespace checks pass. Independent re-review, coordinating review and exact-head
CI remain required before merge.

Independent review rejected the original `cf8befca9cef418d7e1238945872bed45d7fc6f2`
candidate: cancellation before dispatch released its token before the transaction's
final publication check, so a clean committed cancellation incorrectly paused
later ordinary work. Its unchanged manifest, source bindings, full control log,
review report, replayable witness and negative controls are preserved under
[`pre-review/cf8bef/`](pre-review/cf8bef/REVIEW-FAILURE.md). That candidate passed
its original 364 controls and CI; those results did not establish correctness of
the omitted refusal path.

The correction keeps the v3 token live through final publication checks, receipt
commit and connection close, then releases it. Four clean refusal paths
(cancellation, revocation, project closure and expiry) leave later admission
available. Commit, acknowledgement and close failures still preserve uncertainty
and pause later ordinary work. Released-token rejection remains strict; older
profiles retain their existing cancellation path. `refusal-fix-controls.log`
records targeted validation, and `candidate-controls.log` records the full suite.

Coordinating review independently found a second defect in `cf8bef`: an unrelated
safety publication by another process after claim caused the worker's request
file preparation to refuse `durability_unverified`. The v3 worker now qualifies
the new incarnation under the guard before this effect, outside any business
transaction. A real second process publishes the intervening safety action in
the new regression. A later cross-process cancellation still prevents the fake
provider launch at the final authority fence; a failed takeover barrier prevents
both the request file and launch. No provider process is started. The original
probe and its source hashes are retained as `pre-review/cf8bef/root-crossprocess-takeover.json`;
`worker-fix-controls.log` records targeted validation. The intermediate 366-test
refusal-only checkpoint and its changed source files are retained separately.

The approved proposal is retained before and after the required cache
clarification under `design/`. Its NOT RUN language records the preimplementation
review checkpoint. Current source hashes are in `candidate-source.json`; the
acceptance manifest binds this candidate and the retained evidence.

The new controls include current plus two simultaneous exact-4-MiB payloads,
serialization overflow before creation, safety actions with ordinary residue,
safety refusal with residue or unresolved essential debt, missing receipt proof,
write/sync/close/replacement/commit uncertainty, restart and takeover, worker
failure origins, live HTTP logout failure, and the required two-process case
where scratch appears while the cached current incarnation remains unchanged.
No candidate is adopted, overwritten or deleted. Later safety success does not
forgive earlier intent debt.

`pre-review/iterations.json` explains retained initial failures and corrections.
These initial iteration logs have no exact source snapshots and are not claimed
as replayable immutable-source evidence. The later candidate has explicit source
bindings. Failed controls are not overwritten or relabeled as successes.

The conditional 12 MiB limit covers only the three named guard payload files,
not SQLite/WAL, other writers, physical allocation or available capacity. This
is application-level fault injection, not a storage-device, VFS or power-loss
qualification. Quotas remain disabled, capacity UNKNOWN, fifth-headroom FAIL.

All 248 prior evidence files match accepted #42 byte-for-byte. Existing profiles,
OIDC/restore refusals, settings, disabled source candidates, the original cleanup
failure and supplemental conservative charge, and active Outfitter pin remain
unchanged. No accepted store, Belay, parent/rejected/dirty worktree or live provider
was modified. The separate broker-upgrade qualification is outside this change.
