# Independent review: HOLD

Exact candidate: `5fd7d445855b191fd2f20eee218a4860961cf38e`. Candidate01 remains unexecuted. The prepared manifest and all 32 prepared archive files match this source; all 1,071 files in the independent capture remain unchanged.

## Blocking findings

1. **D1 — Missing diagnostic-to-receipt correlation** (`qualify_scoped_docker.py:228–249`, `272–275`, `373–388`, `545–548`). The oracle checks receipt/application results without binding stdout/stderr to the broker's retained `output_sha256`/`output_bytes` diagnostic file. The full synthetic exercise passes with absent or inconsistent diagnostic references. A stronger retained probe passes with bounded, valid JSON sidecars having correct hashes and sizes but different stdout/stderr. Production P1 checkpoint validates present reference hash/size, so the malformed-reference probe alone is not proof of a real end-to-end PASS; it does not repair the valid-but-different-output correlation gap. Require and retain the exact diagnostic bytes, verify reference/hash/size and compare both output streams to the receipt before acceptance.

2. **D2 — Unverified project authority** (`qualify_scoped_docker.py:336–344`, `212–215`, `273–280`, `459–467`, `545–551`). Requesting two 60-second projects does not prove that the ledger contains exactly those projects and budgets. The full synthetic exercise passes with both budgets changed to 600, or an extra unplanned 600-second project. Four settled attempts and 120 charges still hold, so this is not a fifth-launch bypass. Bind the exact project map and integer 60-second budgets after creation, before launches and at final acceptance.

## Evidence and boundaries

The 16 focused synthetic tests pass. Five independent negative probes expose the two qualification-proof gaps above. Scripts and every failed witness are retained beside this report. Full wiring probes mock CLI initialization, broker/process execution, Docker observations and checkpoint; no provider was invoked. The separate valid-hash diagnostic witness is a pure final-oracle control.

The restart marker, single-use four-launch guard, no-retry behavior, original failure persistence, current-access/cancellation refusals, bounded exports and artifact writes outside scoped SQLite owners showed no additional blocker in this bounded review. These observations do not waive D1/D2 or establish actual Docker qualification. No protected worktree, source, pin, provider or prior evidence was changed.

The prepared P1 broker pairing is the reviewed candidate's identity. The proposed accepted-current broker replacement is a new candidate boundary and is not cleared by this report. No execution or merge authority is granted.
