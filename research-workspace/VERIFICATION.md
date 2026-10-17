# Research workspace verification — 17 October 2026

Observed scope: one operator's computer, loopback HTTP, local SQLite authority,
the Outfitter Docker broker, pinned Python/R images and synthetic inputs. The
browser checks use Chromium. Neither live cloud execution nor participant data
was involved. Belay and the existing course/AI pilots were unchanged.

| Layer | Observed result | Evidence boundary |
| --- | --- | --- |
| Unit and adversarial controls | 55 tests passed, with ResourceWarnings treated as errors. | Membership, cross-project refusal, durable state, allocation races, cancellation/recovery, receipt checks, and bounded archive verification; not cloud acceptance. |
| Actual HTTP → broker → Docker | 35 checks passed; every recorded run had verified cleanup. | Python/R, two different datasets/projects, role/refusal controls, budget refusal, comparison, portable export and independent store reopening. |
| Browser | 23 observed checks passed. | Sign-in, creation, real runs, preserved failure, membership/removal, viewer controls, CSV upload, native ZIP download, keyboard tabs, mobile layout, draft preservation and project closure. |
| Downloaded evidence | Valid; three runs and 15 files, including the failed R attempt. | Internal integrity and selected lineage; not authenticity, publication certification or scientific validity. |
| Independent source review | Receipt, access-revocation and connection-lifetime findings fixed and regression-tested. | Scoped agent review, not an exhaustive security certification. |

The final local HTTP run used Outfitter commit
`8542f092f9f4058c38c7b77ca019cd47803e0627` and the immutable references in
`images.lock.json`. The browser run used its earlier Docker baseline. The final
HTTP report contains hashes of the Python source and tests exercised. Browser
checks preceded the final receipt/refusal review;
the final automated HTTP run and unit controls cover those backend changes.

Outfitter's final scoped review also fixed authority-ledger growth, corrupted
accounting acceptance, ambiguous late creation and expired cloud mutation
permissions. Bounded diagnostics now live outside the core ledger, while missing
diagnostics cannot prevent cleanup/accounting updates. Its provider-failure
regressions use simulated providers; its separate real-Docker suite covers local
execution, broker loss, cancellation, cleanup and concurrent reservations.

## Retained evidence

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
- Shared hosting, campus SSO, complete backup/restore, retention/deletion,
  store-wide quotas and approved private-data handling remain future work.
- Jetstream2 preflight/worker simulation in Outfitter is separate from live
  resource acceptance. No native service-unit accounting, independent cloud
  supervisor or actual Jetstream2 worker is accepted by these local tests.
- The demonstration does not validate a research question, study design,
  statistical inference, participant consent or scientific conclusion.
