# Local streaming summary evidence

This records bounded native Python/R/checker tests of the disabled candidate
component. It does not qualify an execution adapter, container image, host,
large-input workload, storage enforcement, private data or trusted cleanup.
All readiness flags remain false.

[verification.json](verification.json) binds the source at
`bd4848453e60ee01e6b894d34e907bb5df5149c5` and these separate, overlapping runs:

| Record | Outcome | Meaning |
| --- | --- | --- |
| [initial-failed.log](initial-failed.log) | 25 methods: 24 passed, 1 failed; no skips | The negative fixture was actually valid CRLF. This is a retained test-fixture error, not a product defect. |
| [corrected-workload.log](corrected-workload.log) | 28 passed; no skips | Test-only correction and stronger Python/R-to-checker controls. |
| [final-isolated.log](final-isolated.log) | 55 passed; no skips | Combined workload/catalog/checker selection with an isolated parent. |

Logs and their original result/interpreter JSON are byte-preserved. The first
two parent commands were not isolated; their children used Python `-I -B` or
R `--vanilla`. The final parent used `-I -B -W error::ResourceWarning` with
[the exact trusted-root bootstrap](final-bootstrap.py). That bootstrap contains
the observed local checkout path; it is evidence, not a portable launcher.

The protected coordinator archive retains complete 1,320-file before/after
inventories for each run, plus the bounded command runners. Their hashes,
counts and availability are explicit in the verification record. Interpreter
hashes identify native executable frontends, not their transitive dependencies
or a qualified container image. The actual R version probe was retained and
was not rerun for the final selection.

The original failed log preserves a trailing space emitted by unittest before
its failed subtest. A whitespace check excluding that exact raw log is a
source/document check; it must not be described as an unqualified whole-diff
whitespace pass. Hosted CI and independent final acceptance are pending.
