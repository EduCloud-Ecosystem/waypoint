# Capacity contract qualification evidence

Local, disposable evidence for [the contract](../../../STORAGE-CAPACITY-CONTRACT.md).
No accepted store, broker service, Docker resource or remote identity is used.

- `waypoint.json`: eight production-connected controls on Python/SQLite versions
  recorded in the report. Includes successful reproductions of current failure
  paths and explicitly modeled capacity rules; not quota acceptance.
- `broker.json`: seven existing controls at the pinned Outfitter commit, executed
  from a disposable Git archive with simulated providers. Real watchdog process
  ownership and storage peaks remain NOT RUN.
- `review-waypoint.json`, `review-negatives.json` and `REVIEW.md`: corrected
  independent review and negative controls. The earlier self-comparison witness
  remains under `pre-review/`.
- `manifest.json`: hashes of the precise contract, harnesses and observations;
  all 168 earlier evidence files matched the PR38 merge. The original fifth
  headroom failure is not replaced or repaired.

The local complete suite passed 296 tests, including the entry point that runs
all eight new Python controls. The seven Go controls are a separate pinned run.
`quota_enforced` remains false. Review the contract's UNKNOWN fields and next
slice conditions before any authority-growth or admission implementation.
