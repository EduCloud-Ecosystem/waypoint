# Combined revision check after accepted PR #43

Accepted PR #43 merge `5f2b21f1535702f9c18592b15e61c4c347d93558`
was incorporated with merge commit
`c6425ab64f13ce9ceefb5b34e20d2eb823925916`, which has a DCO `Signed-off-by`
trailer and no cryptographic signature, without rebasing or rewriting the
qualification history. Production modules match that accepted base exactly.
The qualification still uses the unchanged active broker pin and archived
catalog/image/qualification files; no runtime/profile/default was edited here.

The final harness additionally keeps a rejected later receipt's hash separate
from the last valid final receipt and its full charge. The failed observation
stays visible, and its hash cannot be mistaken for the earlier valid summary.
The fake-caller regression checks this pairing as well as refusal of charge loss.

- [384 full controls](full-controls.log) pass with ResourceWarning treated as
  errors. This includes the 16 qualification controls and the accepted guard
  publication controls; the counts are not additive.
- The [same 16 qualification controls under optimized Python](focused-optimized.log)
  pass. [JavaScript syntax](javascript-check.log) exits zero without output.
- [Read-only retained-observation validation](retained-observations.json) again
  accepts the seven historical final status receipts, all charged 30 CPU seconds
  with verified cleanup, totaling 210. It starts zero broker processes and zero
  Docker attempts. This is not another provider or workload observation.

The actual seven-attempt/45-control run remains attributed to
`89084a70f7341fc11054b9d620d858c214787ced`. Its original report and both earlier
failures remain unchanged, as do the accounting-review red probes and first
green delta. The accounting correction is documented in the
[review supplement](../review-accounting/README.md). Its first manifest describes
revision `17df74d15447ffc49abf7ec303d987ed0afd81d9`; this integration manifest
binds the final combined source instead. Neither retroactively labels the old
Docker run as a run of the final source.

All 304 evidence files from accepted PR #43 and all 31 prior files in this
qualification packet are byte-identical. [manifest.json](manifest.json) records
the current 25 scoped source hashes, test source identities and seven evidence files, including the archived prior
README and manifest. CI's existing Docker job uses the unchanged baseline broker; a passing job
is not a second candidate upgrade qualification. Unknown synthetic liabilities,
native/consumer execution exclusions, finite-restore refusal and all-writer
uncertainty are unchanged. This PR remains qualification only, not pin adoption.

This documentation-only clarification corrects the earlier ambiguous word
“signed.” The [prior README](provenance-clarification/README-before-dco-clarification.md)
and [prior manifest](provenance-clarification/manifest-before-dco-clarification.json)
are preserved byte-for-byte. Source, test logs and workload observations are
unchanged; no local tests, broker processes or Docker workloads were repeated.
