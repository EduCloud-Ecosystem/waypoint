# Supplemental observation: failed Documents replay

This observation supplements the immutable [PR 31 source-build evidence](../source-builds/README.md).
It resolves the later resource/accounting observation for one recorded replay;
the original workload result remains **FAIL**. No original evidence is replaced.

[observation.json](observation.json) records timestamped, bounded, read-only checks
of the exact recorded container ID and name, plus identity comparisons against
the retained private request, ledger and receipt. Its SHA-256 is
`02f6ae6d1bb782aca9d14aadc81f1d840b755efc13cd399590c806cd37771205`.
Retain this digest and the accepted Git commit outside the artifact; embedded
hashes establish integrity, not independent authenticity.

| Evidence | Original observation | Later observation |
| --- | --- | --- |
| Workload | Failed Documents-directory replay | Still FAIL; not rerun |
| Container | Removal in progress; cleanup unverified | Exact original ID and name both absent at the current Docker endpoint |
| Broker ledger | Earlier receipt records unknown cleanup and unresolved liability | Matching run is cancelled, cleanup verified, unresolved liability false |
| Accounting | 30 CPU-seconds reserved | 30 CPU-seconds conservatively charged; estimated upper bound, no refund |
| Original receipt/report | Saved unknown receipt and failed report | Original bytes retained unchanged; later state is recorded only here |

The ledger owner matches its run owner. The retained private request matches
the ledger request, and request/project/image identities match the original
receipt. The original cleanup attempt binds the historical container ID and
name to this request. The ledger hash is unchanged from the
[earlier read-only review note](prior-review-note.json), and all private source
artifacts were unchanged across this inspection. Private ledger/request/receipt
files remain private; their hashes and selected observation fields are recorded
here. No full private ledger is published.

The ledger's stored finish timestamp is evidence from the ledger, not an
independently witnessed cleanup time. This observation does not attribute which
process completed cleanup. The original failed report, receipt and workload
remain historical facts even though resource/accounting state has since settled.
The earlier review note is retained as earlier context, not current gate status.

No cleanup command, ledger mutation, refund, rerun of this failed replay, Docker
restart, or source-environment promotion was performed. All four source-built
environments remain disabled candidates. Independent-human reproduction on a
second machine, hosted admission and real-data admission remain NOT RUN. Belay
and parent pilot worktrees were not modified.
