# Private local recovery

This procedure replaces a stopped local workspace with a fresh instance on the
same trusted host. It captures SQLite authority, immutable run files,
configuration and Outfitter's matching ledger. The replacement stays held until
the current source is verified and permanently fenced. An unavailable source
remains a recovery gate; this is not off-host disaster recovery.

Use a recovery-capable Outfitter binary. Its ledger upgrades in place to v2;
older binaries cannot operate that ledger afterward. Keep the updated binary
available for cleanup. Use fresh synthetic state for the acceptance exercise.

## Capture and inspect

Stop the workspace service normally. Recovery refuses an occupied service lock.
Inspect Outfitter's `recovery-status` and reconcile unfinished provider work at
the source before capture. A checkpoint can retain unknowns for inspection, but
it cannot activate them into another admitting authority.

```sh
python3 -m research_workspace checkpoint \
  --state /private/current-workspace --output /private/new-checkpoint \
  --broker /absolute/path/to/recovery-capable/outfitter
```

Keep the returned `checkpoint_sha256` separately in a trusted operator record.
The checkpoint contains private research and authentication authority; it is
**not** a public evidence ZIP. Use approved private storage. Embedded checksums
do not establish authenticity if the entire checkpoint is replaced.

The source database holds a write transaction while its SQLite backup, run files
and broker checkpoint are captured. The manifest binds logical database content,
configuration and every selected file. Later source changes—including membership
removal, closure, published results or spending—invalidate activation of the old
snapshot. No checkpoint overwrites an existing directory. Failed partial outputs
remain inspectable and cannot activate as complete checkpoints.

Bounds: 10,000 regular files, 64 MiB per file and 128 MiB total. Symbolic links,
hard links and special files are refused. Keep checkpoint and replacement paths
outside the source and each other. These bounds are not a retention policy or
workspace-wide storage quota.

## Restore held, then transfer authority

```sh
python3 -m research_workspace restore \
  --checkpoint /private/new-checkpoint --trusted-sha256 RETAINED_SHA256 \
  --state /private/replacement-workspace \
  --broker /absolute/path/to/recovery-capable/outfitter
python3 -m research_workspace recovery-status --state /private/replacement-workspace
python3 -m research_workspace activate-recovery \
  --state /private/replacement-workspace --source-state /private/current-workspace \
  --checkpoint /private/new-checkpoint --trusted-sha256 RETAINED_SHA256
```

Restore clears sessions and replaces every stored access-key hash with an
undisclosed fresh value, preserving active/revoked subjects and memberships.
Queued work becomes cancelled; running work becomes interrupted with unknown
accounting. Nothing is redispatched. Completed records, comparisons, exact
input/recipe/result bytes and broker charges are retained. Historical unknown run
records stay visible even when separate broker reconciliation later settles them.

Activation locks both stopped services and both databases, compares the current
source with its checkpoint, and checks the held replacement has not changed.
It seals source access, then Outfitter fences that source to the exact replacement
before enabling destination admission. Only then can its service start.

Issue fresh access keys deliberately after activation, saving them privately:

```sh
python3 -m research_workspace user-rotate \
  --state /private/replacement-workspace --subject EXISTING_ACTIVE_SUBJECT \
  > /private/replacement-key.json
python3 -m research_workspace serve --state /private/replacement-workspace
```

`user-rotate` never revives a revoked subject. Prior keys and sessions fail.
Neither allowances nor settled charges reset during activation or retries.
The retired source refuses service and mutations, including queued submission.

## Interruption and limitations

An interrupted cutover leaves one or both applications held. Inspect both status
reports, then retry the **same** checkpoint/source/destination pair. Another
destination cannot reuse the source fence. If accounting or source content
changed, stop and investigate; do not remove fences, edit balances or enable the
destination manually. An interruption followed by external source changes can
require operator repair beyond the supported same-pair retry.

This assumes trusted operators and a filesystem supporting flock, SQLite
transactions, atomic rename and fsync. It cannot prevent privileged rollback at
the original path, copied legacy authorities or cloned machines. No network
SQLite, multi-host writer, remote fencing, deletion reconciliation or approved
institutional recovery target is provided. These tests admit no private research
or student data.

## Executable exercise

```sh
python3 verify_recovery.py --broker /absolute/path/to/outfitter \
  --output .runtime/recovery-exercise-new
```

The exercise creates synthetic state, executes Python/R through Docker, restores
a replacement and injects an application interruption before broker cutover. It
checks results, charges, revocations, stale-snapshot refusal, old-key refusal,
queued cancellation, fencing and a bounded destination run. Only
`verification.json`, `before.zip` and `after.zip` are public synthetic evidence;
`state.private/` contains private checkpoints and ledgers.

Unit controls separately interrupt execution after broker activation and before
each application admission write. These are simulated failures, not independent
host/control-plane failures. Observed restore time and zero completed-data loss
at a frozen point are fixture measurements, not service commitments. A second
operator and replacement machine still need to complete the runbook.
