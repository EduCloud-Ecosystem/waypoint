# Bounded guard publication candidate evidence

The fresh v3 candidate passes 364 automated controls, including 30 new guard
publication tests and the existing 334 controls. One new test also reruns the
14 applicable bounded-export controls on v3. Providers are simulated in the new
controls; competing processes, process death, files, locks, SQLite and local HTTP
are exercised directly in disposable temporary stores. JavaScript syntax and
whitespace checks pass. Independent code review and exact-head CI are pending.

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
