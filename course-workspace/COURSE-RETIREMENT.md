# Course retirement and portable coursework

`retirement.py` closes a stopped institutional course while preserving its homes,
configuration and encrypted recovery snapshot. It records a retention owner and
review date, blocks startup and policy changes, and exports each retained home's
`work` directory. It never deletes files, starts services, disables identity
accounts or publishes learner work. This version has no reactivation or permanent
deletion operation. A review date schedules a decision; it does not authorize deletion.

## Stop and prepare retirement

Save coursework and announce closure. Stop the Hub, learners and any other writers
to the course's volumes. Use the current checkout on a native Linux worker, a
private canonical deployment directory and owner-only recovery configuration/key.
Establish and independently escrow the encrypted repository as described in
[RECOVERY.md](RECOVERY.md). Keep the images and key available throughout retention.

```sh
sudo python3 deployment.py stop --directory /srv/educloud/course
sudo python3 retirement.py preview --directory /srv/educloud/course \
  --recovery-config /etc/educloud/recovery.json \
  --plan /secure/course-retirement-plan.json \
  --retention-owner course-support --review-after 2027-01-31
```

Choose the institution's owner and review date (today through ten years ahead).
The new plan's parent must be private. The plan cannot overlap homes, Hub data,
backup storage or configuration/key references. Preview checks stopped state,
actual worker/image compatibility, effective quota policy and every retained
home, including removed learners. It takes a fresh encrypted snapshot and binds
the plan to current configuration, file inventory, storage, source manifest,
immutable images, recovery settings/key and tool bytes. Changes refuse apply.

Review the private plan, owner/date and exact snapshot before applying. Public
CLI output exposes only counts, image-independent plan/snapshot digests and status;
it omits roster, paths, owner and configuration/key fingerprints. Protect private
plans and records as institutional operational data.

```sh
sudo python3 retirement.py apply --directory /srv/educloud/course \
  --recovery-config /etc/educloud/recovery.json \
  --plan /secure/course-retirement-plan.json
```

Apply rechecks frozen inputs, writes a private interruption journal, then writes
the sticky `.course-retired.json` marker and verifies retained state before
finishing. `RETIRED` means the stopped worker is sealed. Startup through both
deployment entry points, roster/quota changes and image upgrades refuse retired
or pending retirement state. Stop/status and completed original-course backups
remain available. Direct root/Docker/storage manipulation is outside these guards.

An interrupted `.retirement-pending.json` blocks admission. Keep storage stopped
and resume the exact plan/configuration with `retirement.py resume` using the
same arguments as apply. Changed files, images, configuration or tool bytes need
operator investigation; do not erase markers to conceal drift. An uncertain
completed response can be retried with unchanged inputs. A malformed or linked
marker also blocks startup.

Retire/hide the Cairn workspace binding through its existing instructor workflow
and communicate closure separately. This worker operation does not change Cairn,
the institutional identity provider, reverse proxy or students' institution-wide
accounts. The old workspace endpoint must remain unavailable during closure.

## Export saved coursework

Use a fresh destination outside deployment, homes and Hub data, with an
operator-owned parent that others cannot modify. Export requires completed
retirement, no pending transaction and no active storage writers.

```sh
sudo python3 retirement.py export --directory /srv/educloud/course \
  --destination /secure/coursework-export
sudo python3 retirement.py verify-export --destination /secure/coursework-export
```

The output is an atomic owner-only directory containing `manifest.json` and one
`home-OPAQUE_SUFFIX.tar.gz` per retained learner. Scope is explicitly
`learner-work-directories`: `/home/learner/work` (the default saved coursework
location), including hidden project files. Missing work folders are recorded as
empty; files elsewhere in each home are counted as `outside_work_entries` and
remain in retained storage and the encrypted snapshot. No identity-to-home map,
operator environment, Hub database/session credentials or outside-work runtime
metadata is copied into this portable package. Student-authored project files
can themselves contain private material or secrets; the exporter does not classify
or redact their contents. Treat the plaintext export as private learner data and
encrypt/restrict any transfer under the institution's policy.

The exporter uses a network-disabled read-only helper with the retained course
image, compares source inventory and SHA-256 file digests with the actual archive,
and rechecks retained state before publishing. It refuses unsafe paths, unsupported
types/links, overwritten destinations, drift and bounded entry/file/byte budget
violations. The independent verifier reads and hashes archives without extraction
or execution. Hashes detect damage; authenticate the package's provenance separately.
Export does not delete source files or upload/send work. Preserve the mapping in
the private institutional record when individually delivering opaque archives.

## Retention and recovery stay closed

Retirement preview's fresh snapshot and subsequent retired backups carry
`educloud-retired-v1`. Even an abandoned preview produces a protected snapshot:
restoring it conservatively stays retired. Normal per-instance Restic retention
uses `--keep-tag educloud-retired-v1` in addition to its age/count rules, preserving
tagged snapshots ([official retention semantics](https://restic.readthedocs.io/en/stable/060_forget.html)).
There is no automatic expiry/deletion of these protected snapshots. The institution
must approve any future deletion procedure, including repositories and exports.
Protected bundles use recovery version 3: older version-1/2 restorers refuse them,
and current recovery seals on the bundle version even without the retirement tag.
Ordinary backups remain version 2 and historical version-1/2 recovery is supported.

Preview takes its snapshot before the retention ledger is created. Its snapshot
therefore restores the minimal closure seal, without the later owner/review-date
record. Preserve the reviewed plan/ledger privately off host, or take a further
protected backup of the completed original course to archive that record encrypted.

Use the existing institutional restore into a fresh namespace and prepared XFS
destination. Protected recovery writes a minimal retirement seal before usable
environment/home allocation; failure stays blocked. It restores saved homes and
images without reactivating prior Hub sessions. The original encrypted retirement
record, when present, is historical evidence. A protected snapshot cannot initialize
a local trial. Use the current checkout's `retirement.py export` for a restored
closed course; the recovery runtime provides restoration and admission guards,
not the full lifecycle/export tools. Restored closure still needs an institutional
retention owner/review decision recorded separately.

Completed original courses retain their stopped Hub container and support further
protected backups. A closed fresh restore has no Hub container/database and cannot
take a new full runtime backup through the existing backup command. Preserve its
original protected snapshot/key and verify a new coursework export; do not create
or start a Hub to work around the closure seal.

## Disposable acceptance

`rehearsal/verify_retirement.py --output /tmp/NEW_PRIVATE_DIRECTORY` exercises
synthetic saved Python/R work, retained removed learners, protected backup,
interruption/resume, admission and mutation refusal, private export/verification,
corruption controls, protected fresh-destination restore and actual Restic
retention. It publishes bounded `RETIREMENT-PASS.json` only after all required
controls and cleanup. CI uploads only that selected synthetic report. Private
environment, plans, journals, export packages and backend keys remain excluded.
This verifies disposable component behavior; institutional retention, off-host
recovery, actual course closure and data-delivery acceptance remain external gates.
