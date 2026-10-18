# Final-accounting review correction

This supplements the historical packet at parent revision
`1d28e7606bfe285c9257002d757d7ec2a33e00bf`. It supersedes that revision's
acceptance-oracle claim, not its retained observations. The original
45-control/seven-attempt report, nine-test logs, both earlier failed controls,
source snapshots, preparation report and manifest remain byte-identical.

## Retained failure

The [owned probe](charge_drop_probe.py) loads the exact
[pre-fix verifier](verifier-before-accounting-fix.py.txt), copies the historical
report and substitutes final status responses through a fake caller. Each
response is a structurally valid pre-admission refusal with matching identity,
zero charge and verified cleanup. The old cleanup/report oracle incorrectly
accepted all seven, reducing the final reported charge from 210 to zero after
the earlier success checks. It executed no broker process or Docker operation.

Both the [normal](owned-red.log) and [optimized](owned-red-optimized.log) owned
probes exit 1 to mark the reproduced defect. The separately authored
[independent probe](independent-probe.py.txt), [normal log](independent-red.log)
and [optimized log](independent-red-optimized.log) preserve the same finding.
The independent source includes two preliminary `assert` statements; the owned
probe uses explicit checks and retains its detection under `-O`.

The archived verifier SHA256 is
`36c047ae0686dff90540dc8f5967bf4015551d268ea70923f4f04102e6b7ceea`.
The original real-run report SHA256 remains
`b59a8c9c16c5ff89d1b47a3cdc27d6b2766c7eeda40169094967fcbfc833143f`.

## Corrected boundary

The qualification v2 oracle requires seven distinct prelaunch request bindings
in the fixed scenario order. Each binds project, image, request hash, broker
hash, state role and canonical state-path hash. Final receipts must match and
retain actual admission/execution, success/exit zero, verified cleanup, no
unresolved liability, strict integer 30/30 reservation/charge and
`estimated_upper_bound` provenance. The total must remain 210. A failure marker,
missing binding, duplicate request, changed state/image/broker, truthy flag or
invalid numeric value fails acceptance. Rejected final observations retain
their private raw output and public receipt hash/error; they do not replace a
previously recorded full charge with zero or authorize retry/refund.

The [16 focused tests](focused-green.log) pass, as do the
[same 16 under optimized Python](focused-green-optimized.log). Counts overlap.
These include an end-to-end fake-caller regression after a valid full-charge
observation; direct report-oracle mutations; prelaunch binding refusal; a
changed cleanup target refusing before a command; and the retained-observation
reader accepting valid data and refusing a substituted zero-charge receipt.
They use temporary synthetic files and mocks, without Docker/provider calls.

## Retrospective check of actual observations

The [read-only reader](recheck_retained_observations.py) checked the seven
original private final status outputs against the historical public report,
their exact original request files, retained config/build hashes and ledger
request identities. The [supplemental report](retained-observations.json)
records **7 valid final receipts, 210 conservative charged CPU seconds, zero new
broker processes and zero new Docker attempts**. The corrected report oracle
accepts the reconstructed bindings and actual observations. Each raw private
receipt/request/ledger is represented by its SHA256; private contents remain
local. Receipt hashes in the derived summaries use canonical JSON, while
`stdout_sha256` hashes the original CLI bytes.

This is retrospective evidence. Those new binding fields were reconstructed
from retained files; the old run did not record the v2 prelaunch contract.
The seven real Docker attempts still belong to original implementation
`89084a70f7341fc11054b9d620d858c214787ced`. There was no final-head workload rerun.
The original v1 report is preserved and intentionally fails the v2 report oracle
until its observations are explicitly reconstructed by this separate reader.

The diagnostic-failure run remains charged even though its metrics were not
published. Two earlier bounded-export status observations also retain missing
diagnostic errors after the owned directory obstruction. Native/consumer state
claims remain limited to unchanged synthetic fixture bytes and CLI refusal/read
behavior; there is no new native, consumer or provider execution evidence.
Unknown fixture liabilities remain unresolved. No active pin, production
runtime, quota, accepted store, cloud permission or physical-capacity claim
changes. CI's existing Docker job remains tied to its unchanged active baseline;
it is not an additional candidate upgrade run.

The append-only [delta manifest](manifest.json) binds the current scoped sources,
new evidence, all 17 files in the prior packet and 248 older evidence files.
The old manifest remains a historical manifest of its own source revision.
