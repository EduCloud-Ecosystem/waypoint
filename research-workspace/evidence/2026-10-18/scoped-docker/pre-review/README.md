# Preserved synthetic review witnesses

No witness in this directory is an actual Docker attempt or a qualification
PASS. Candidate 01 remains immutable and unexecuted.

- `first` retains the initial integer-versus-boolean oracle failure.
- `independent-5fd7d44` retains the independent review, ledger budget/population
  probes and diagnostic binding counterexamples. Its original complete source
  snapshot remains at the path and hash in `source-archive-preservation.json`.
  The exact 32 files needed by its probes are also retained in
  `../../candidate-01/source.tar.gz` relative to that directory. For a disposable
  replay, extract those members into `source/research-workspace` alongside copies
  of the unchanged probe scripts. Use a new replay directory for each probe;
  the scripts intentionally create outputs and the diagnostic directory once.
- `root-export` is the unchanged self-contained export mismatch witness. Its
  `probe.py` reads the retained candidate source archive and synthetic fixture,
  builds structurally valid altered bundles, and reports the original false PASS.
- `accounting-absence` retains the original no-process probe and output showing
  a known 30-CPU-second charge omitted from the report after failed independent
  container-absence observation. The original script references the retained
  private source checkout named in its import path.
- `export-test-fixture` retains the first revised export regression run and exact
  harness/test sources. One recipe mutation failed the production bundle schema
  before reaching the new comparison. The corrected regression also changes the
  embedded recipe hash so it remains a valid bundle and tests the intended check.

The `preservation.json` records compare retained original witness files byte for
byte with their original locations. Newly written preservation notes are separate
from those unchanged witness files. Passing revised tests closes these particular
checker counterexamples; real execution still requires review of the revised
manifest and remains NOT RUN.
