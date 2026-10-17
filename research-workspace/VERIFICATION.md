# Research workspace verification — 17 October 2026

Observed scope: one operator's computer, loopback HTTP, local SQLite authority,
the Outfitter Docker broker, pinned Python/R images and synthetic inputs. The
browser checks use Chromium. Neither live cloud execution nor participant data
was involved. Belay and the existing course/AI pilots were unchanged.

| Layer | Observed result | Evidence boundary |
| --- | --- | --- |
| Unit and adversarial controls | 57 tests passed, with ResourceWarnings treated as errors. | Membership, cross-project refusal, durable state, allocation races, cancellation/recovery, receipt checks, and bounded archive verification; not cloud acceptance. |
| Actual HTTP → broker → Docker | 41 checks passed; every recorded run had verified cleanup. | Python/R, two different datasets/projects, role/refusal controls, budget refusal, comparison, portable export and independent store reopening. |
| Browser | 23 observed checks passed. | Sign-in, creation, real runs, preserved failure, membership/removal, viewer controls, CSV upload, native ZIP download, keyboard tabs, mobile layout, draft preservation and project closure. |
| Downloaded evidence | Valid; three runs and 15 files, including the failed R attempt. | Internal integrity and selected lineage; not authenticity, publication certification or scientific validity. |
| Independent source review | Receipt, access-revocation and connection-lifetime findings fixed and regression-tested. | Scoped agent review, not an exhaustive security certification. |

The final local HTTP run used Outfitter commit
`8542f092f9f4058c38c7b77ca019cd47803e0627` and the immutable references in
`images.lock.json`. The browser run used its earlier Docker baseline. The final
HTTP report contains hashes of the Python/R source, static interface and tests exercised. Browser
checks preceded the final receipt/refusal review;
the final automated HTTP run and unit controls cover those backend changes.

Merge review also found that R inferred numeric group labels and missing-value
names. The recipe now preserves labels such as `001`, `NA` and `NULL` exactly,
and input validation accepts only decimal spellings shared by both runtimes.
Fresh Python/R execution verified these controls; unsupported numeric spellings
are refused before admission.

Outfitter's final scoped review also fixed authority-ledger growth, corrupted
accounting acceptance, ambiguous late creation and expired cloud mutation
permissions. Bounded diagnostics now live outside the core ledger, while missing
diagnostics cannot prevent cleanup/accounting updates. Its provider-failure
regressions use simulated providers; its separate real-Docker suite covers local
execution, broker loss, cancellation, cleanup and concurrent reservations.

## Portability and coordinated local recovery

The P1 code snapshot `bf275dd` extends the accepted baseline above. Its local
HTTP and recovery reports record exact exercised source hashes and the broker
binary; the pinned recovery broker revision is
`5827c84e9952cc67b85e91ceff4282e0ead6923b`.

| Layer | Observed result | Boundary |
| --- | --- | --- |
| Unit/adversarial | 119 passed | Includes 17 catalog, 26 reproduction, 16 recovery and 2 operator-file controls; simulated providers remain labelled. |
| Current HTTP → Docker | 41 passed, all recorded cleanup verified | New admission uses exact active catalog pairs; two public/synthetic studies and the R-label control. |
| Historical reproduction | Two selected successful runs passed | Original Python v1 and archived R v1 bytes; original failed R remains NOT RUN. Fresh 60 CPU-second authority, full charges retained, no owned containers remaining. |
| Workspace recovery | 25 passed | Two actual Docker runtimes; exact completed records, access restrictions and charges preserved through held restore and an injected cutover interruption. |
| Environment browser interface | 17 passed | Desktop/mobile, keyboard, active/archive disclosure and draft preservation; includes two explicitly simulated controls and no compute submissions. |
| Broker recovery | 38 passed in its separate suite | Local CLI/Docker evidence in Outfitter; independent review fixed and verified a special-file hang. |

The final workspace restore observation was 0.773 seconds on this computer,
with no lost completed records or artifacts at the frozen checkpoint. This is
not an approved recovery target. Clean image builds from source, independent
human/second-machine reproduction, off-host recovery, live cloud operation and
private-data admission remain **NOT RUN**. Recorded OCI images were reused;
no source-build or architecture-portability claim is made.

Evidence: [P1 acceptance manifest](evidence/2026-10-17/p1/acceptance.json),
[current HTTP report](evidence/2026-10-17/p1/http-docker-verification.json),
[recovery report](evidence/2026-10-17/p1/recovery-verification.json),
[reproduction report](evidence/2026-10-17/reproduction/verification.json), and
[catalog browser report](evidence/2026-10-17/catalog-browser-verification.json).
The P1 evidence directory retains both synthetic project ZIPs and recovery
before/after ZIPs. Private checkpoint/authority directories are excluded.

## Retained baseline evidence

- [Final HTTP/Docker report](evidence/2026-10-17/http-docker-verification.json)
- [Browser report](evidence/2026-10-17/browser-verification.json)
- [Actual browser download](evidence/2026-10-17/browser-evidence.zip)
- [Desktop](evidence/2026-10-17/desktop.png) and
  [mobile](evidence/2026-10-17/mobile.png) screenshots
- [Initial failed HTTP control](evidence/2026-10-17/initial-r-failure.json)

The initial R execution failed because the Docker file-descriptor ceiling of
128 was too low for R. Outfitter raised the bounded ceiling to 1,024. The later
R execution passed, and the original failure remains in the browser history,
bundle and initial failed report. It was not relabelled successful or refunded.

The first browser dataset's baseline has 10 observations, overall mean 23.2,
and group means 17.2/29.2. The alternative has six retained observations,
overall mean 17 and group means 12/22. These describe the synthetic fixture,
not a research finding or an intervention effect.

Only the explicitly selected synthetic artifacts above are committed. Access
keys, sessions, membership storage, private logs, runtime databases and broker
authority remain outside source control. Selected artifacts were checked for
the actual local access-key bytes before inclusion. That check does not license
or declassify future user-provided inputs.

## Remaining acceptance

- GitHub Actions repeats unit/static controls and actual Docker integration on
  the PR. Its run status is separate evidence from these local reports.
- Browser cancellation, forced session expiry and manual screen-reader review
  were not exercised. Backend cancellation and expiry enforcement have focused
  controls; they do not substitute for those browser/manual assessments.
- A second independent researcher has not yet completed the runbook.
- Shared hosting, campus SSO, off-host backup/restore, retention/deletion,
  store-wide quotas and approved private-data handling remain future work.
- Jetstream2 preflight/worker simulation in Outfitter is separate from live
  resource acceptance. No native service-unit accounting, independent cloud
  supervisor or actual Jetstream2 worker is accepted by these local tests.
- The demonstration does not validate a research question, study design,
  statistical inference, participant consent or scientific conclusion.
