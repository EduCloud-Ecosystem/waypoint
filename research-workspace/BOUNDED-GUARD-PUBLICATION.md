# Fresh bounded guard publication

`init --bounded-guard-publication` selects a fresh local-key profile that limits
guard publication to current authority plus one ordinary and one safety scratch
file. It inherits finite row/field limits and bounded exports. Existing profiles
and initialization defaults are unchanged; profile selectors are mutually
exclusive and no existing store can be migrated or adopted.

This profile trades availability for a bounded set of guard payload paths.
**An occupied safety scratch path or an unresolved essential intent blocks new
safety actions as well as ordinary work.** Logout, revocation, cancellation or
settlement can fail. A failed action does not certify a revoked session, cleaned
provider or settled liability. Inspect the returned error and held state.

## Select the profile

Use the [local initialization instructions](README.md#start-the-local-pilot)
with a new empty state directory and add `--bounded-guard-publication` to the
`init` command. Read the exhaustion contract before selecting this disposable
profile. It remains limited to public/synthetic data, loopback access and local
keys. OIDC and all finite-profile restore/cutover entry points remain refused.

| Binding | Exact value |
| --- | --- |
| Mode | `local-finite-public-data-v3` |
| Logical policy | `educloud-finite-authority-v3` |
| Logical policy SHA-256 | `432f95df152db6f1b16b1829f85d192021b6998fd265d7a4075ef2e29dde25af` |
| Guard | `educloud-operation-guard-v4` |
| Database version | `0x45464133` |
| Export | `educloud-bounded-export-v1` |

Missing or mixed bindings cannot fall back to an older profile. The reviewed
[publication contract](BOUNDED-GUARD-PUBLICATION-PROPOSAL.md) defines the exact
policy, role mapping and publication identity. The database schema and existing
operation receipt authority are retained; no second ledger is added.

## Failure and restart

The existing guard lock covers fixed-path exclusive creation, bounded writes,
file synchronization, replacement and parent synchronization. Each publication
records its exact incarnation and predecessor digest. No failed candidate is
adopted, overwritten, truncated, renamed into authority or deleted. Ordinary
failure notes use only the ordinary path. Safety actions require fresh intents;
they cannot borrow an earlier unresolved essential marker.

A publication failure holds the failing process. A fresh process must validate
the exact current authority and receipts and qualify durability before any
mutation. Another running process must do the same when current changes.
Cached durability never caches mutation permission: namespace, receipt outcomes
and holds are checked again under the lock on every mutation/publication.
An unchanged current file does not hide newly created residue.

Ordinary residue can coexist with safety work only while current authority and
receipt proof remain valid, the safety path is free, and no essential debt blocks
it. Missing receipt proof can therefore block safety work even when its scratch
path is empty. Failed acknowledgement remains uncertain; later safety success
cannot erase prior intent or compute debt. No automatic in-place recovery or
residue removal is provided, and startup can refuse to start the worker.

## What the size bound means

The sum of the logical file sizes of `operation-guard.json`,
`.operation-guard-ordinary.pending` and `.operation-guard-safety.pending` is at
most 12,582,912 bytes (12 MiB) under controlled fresh state and cooperative
writers, with each payload capped at 4 MiB before creation. Unknown namespace
entries, unsupported types or links, and oversize files refuse that claim and
mutation. This is not a free-space reservation or a physical allocation bound.

SQLite, WAL, SHM, engine temporaries, filesystem metadata/journaling, external
readers retaining unlinked generations, broker state and other writers are
outside this bound. Settings remain unchanged; `quota_enforced:false`, available
capacity UNKNOWN and the fifth-headroom FAIL remain. Source candidates stay
disabled and the original cleanup failure and supplemental conservative charge
remain preserved.

## Disposable controls

Run from `research-workspace`:

```sh
python3 -W error::ResourceWarning -m unittest discover -s tests -p test_guard_publication.py -v
```

The controls use fresh temporary stores and simulated providers, including real
competing processes, process death, file/SQL failure injection, the safety matrix,
cached-current residue observation, and inherited bounded-export authorization
checks. Application-level failure injection is not a power-loss, storage-device
or SQLite VFS qualification. Earlier frozen capacity/protocol evidence retains
its original source bindings and does not certify this new profile.
