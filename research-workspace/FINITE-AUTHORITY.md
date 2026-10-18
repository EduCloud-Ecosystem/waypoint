# Fresh finite logical authority

This opt-in disposable profile adds limits to recorded authority and serialized
fields. It is local-key-only and requires a fresh state directory. It does not
measure free space or enforce storage quotas. Whole-store capacity admission
remains **NO-GO**, `quota_enforced:false`, and available capacity **UNKNOWN**.

The [engineering decision](https://github.com/EduCloud-Ecosystem/educloud/blob/538feced4f36b0ac61fd2b9e13fabbaa21d6a653/docs/decisions/2026-10-17-research-pilot-capacity.md#finite-authority-engineering-decision)
authorizes these initial test-profile limits. They imply no measured disk fit,
throughput, approved host capacity or permission to execute 1,000 broker runs.

## Admission and retained history

| Authority | Limit | Counting rule |
| --- | ---: | --- |
| Users | 128 | All rows, including revoked subjects |
| Projects | 20 | All rows, including closed projects |
| Memberships | 1,024 | All existing membership rows, including owners |
| Runs | 1,000 | All rows, including failed, cancelled and interrupted work |
| Live sessions | 256 | Unexpired sessions, with at most eight per subject |

The existing 100 runs per project, 20 queued/running runs, eight-hour session
expiry, compute budgets and 4,096 ordinary operation-intent limits still apply.
History limits do not reset when status changes. Existing expiry/logout behavior
may remove session rows; no new history deletion or allowance reset is added.

User creation, project creation and its owner membership, new membership rows
(including viewers), run submission and each new sign-in are admissions. Counts
come directly from existing authority tables. Admission checks run under the
existing guard and are repeated inside the SQLite transaction. No second
capacity ledger exists. Invalid keys and full-profile refusals are checked
before allocating an ordinary intent. Completed idempotent requests reuse their
records. An in-progress project initialization defers other finite admissions
until its owner membership is published; its own admitted launch remains usable.

Existing sessions remain usable at row caps. Logout, existing-member demotion or
removal, operator revoke/rotate, cancellation and conservative settlement retain
their safety behavior. Adding a viewer is an admission, not an essential-slot
bypass. A failed write never reports success. Serialized-data overflow after
possible execution retains prior records, charges or unknown accounting and a
guard hold; it does not fabricate success or refund work.

## Version and field contract

`logical_authority` contains the exact supported `educloud-finite-authority-v1`
policy. Its digest is bound into `educloud-operation-guard-v2` configuration and
sidecar state. SQLite `user_version=0x45464131` records the database profile.
The existing `operation_authority` table must contain exactly one matching
identity before any finite-profile access or safety bypass. Missing, unsupported
or inconsistent configuration, database or sidecar bindings refuse; they cannot
fall back to unrestricted operation. Valid interrupted operations still allow
reads and safety actions while holding new work. Coordinated rollback of all
bindings requires an external trusted anchor to detect and is not claimed here.

| Encoded record | Maximum bytes |
| --- | ---: |
| Configuration | 65,536 |
| Project | 32,768 |
| Run | 262,144 |
| Result | 65,536 |
| Worker request | 32,768 |
| Broker receipt | 2,097,152 |
| Recovery record (reserved; restore refused) | 65,536 |
| Checkpoint manifest | 4,194,304 |

Ceilings apply to canonical JSON bytes, including escapes, before publication.
Configuration reads are bounded and reject duplicate/nonfinite values. State,
broker and checkpoint-root paths are limited to 1,024 UTF-8 bytes. Derived paths
also occur in bounded requests/manifests. Guard generation and operation
sequence stay within signed 64-bit bounds; their overflow refuses before write.
Existing guard-file and broker-output limits remain separate. These are logical
record limits, not bounds on SQLite pages, WAL, journals, filesystem allocation,
repeated failed scratch files or external broker/Docker writers.

For requests, each 1,024-byte root can expand to at most 6,144 JSON bytes under
worst-case escaping. Two derived paths plus fixed IDs, reviewed image/command
strings and bounded numeric settings fit below 16 KiB with the current schema;
32 KiB retains headroom and remains an enforced refusal ceiling. The input and
recipe contents are separate files, not embedded in this request. A schema or
catalog expansion still must pass that ceiling.

The production collector accepts at most 262,144 stdout bytes. A conservative
sixfold string-escaping allowance is 1,572,864 bytes, below the 2,097,152-byte
receipt ceiling. The receipt serializer is defense in depth behind the stricter
collector and diagnostic-field checks. The overflow control deliberately injects
about 720 KiB through a mocked collector, outside that collector contract, to
exercise serialization refusal and retained uncertainty. A real collector would
reject those bytes earlier; this is not a reachable accepted receipt overflow.

## Operator use and recovery

Add `--finite-authority` to the ordinary `python -m research_workspace init`
command, supplying a new empty state directory with an existing durable parent,
the broker and immutable-image arguments, subject and compute budget. This flag
also creates the operation guard. Existing initialization defaults and accepted
stores are unchanged; there is no migration command. Do not strip bindings to
open the store with older software. Serving and operator commands reopen the
same bound Store; direct Store methods enforce the same policy.

OIDC setup is explicitly refused for this first finite profile. Its separate
in-memory subjects/sessions and authorization lifetime require qualification.
Checkpoints preserve the policy, database marker, sidecar and source fingerprint.
Finite restore and activation are explicitly **not qualified** and refuse before
provider calls, destination creation or source locking/sealing. All public and
direct guarded entry points inspect the immutable checkpoint database marker as
well as configuration. A trusted, internally hash-consistent checkpoint cannot
select older recovery behavior merely by stripping its configuration markers.
Keep the checkpoint and source for a separately reviewed recovery increment.

## Evidence and remaining gates

The [finite-profile acceptance record](evidence/2026-10-18/finite-authority/README.md)
separates synthetic boundary seeding, production-method transitions, simulated
provider behavior, preserved failed controls and current checks. It adds no
human, browser, real-data, host-fit or live-provider acceptance for this profile.
Ordinary CI continues to exercise existing Docker/guard/recovery modes; those
checks are compatibility evidence, not finite-profile provider qualification.

PR39's [capacity contract](STORAGE-CAPACITY-CONTRACT.md), source pins and original
observations remain byte-identical. Its eight Python controls now execute from
`qualification/capacity/pinned-v1.tar.gz`, a verified 43-file Git archive of
`599ee0ce23942bbfc578124af12285484c357157`. It includes all contract/manifest
inputs, the original test bridge and the packaged historical recipe required by
the catalog. The current bridge verifies the archive digest, original source and
evidence hashes, and unchanged contract/harness bytes before execution. It does
not relabel those v1 witnesses as tests of modified production source.

The fifth consumed-headroom failure and all unknown writer/peak bounds remain.
All four source environments remain disabled candidates. The original failed
Documents replay and unresolved cleanup observation remain intact; the separate
[reconciliation record](evidence/2026-10-17/replay-cleanup-reconciliation/README.md)
records later container absence and the full conservative 30 CPU-second charge,
without rerun or refund. Belay, accepted state, parent pilot worktrees, rejected
drafts and the shared Docker baseline are outside this change.
