# Studio readability integration — November 15, 2026

Owner: Greg Chism; Codex integration in the existing Studio worktree. Scope: integrate the UI chat's c314c90 through e50b66d follow-up with current main 2112a38. The accepted 9b81504 application baseline and current main differ only in maintenance policy; policy files are preserved. Permitted changes: tutor/file-picker presentation, submission browser controls, finite instructor launcher pins and verification records. No new service, student admission or spending limit.

Cairn producer/consumer pin: 533c16a414b9fef1979d0de783b3436f31dd03a7. Belay-classroom c5a27073e74b9eeb7ab697190915378b50132883 and Portage b72881f009e61e1311b6e46b92b42841991fd163 remain unchanged. All modes use the reviewed Cairn UI; live retains at most seven requests, $0.175 reserved and the original tutor deadline. Human review and actual deployment remain separate.

Initial test invocation incorrectly selected pytest, which is absent from the cached driver environment. No installation was made. Corrected to the repository's unittest commands; their logs and all subsequent full-workflow failures are retained under output/readable-integration and timestamped output directories. Acceptance results will be added after execution. Rollback: previous merged application source, preserving evidence and receipts.

## Independently repeated acceptance

On source 3fd02fff7d72ee33774fb04704b0308dce443039, all 13 actual NHANES browser controls passed in 232.48 seconds with zero provider calls and owned cleanup. Cairn's nine supplementary browser controls passed. The 764 workspace tests and 352 rehearsal tests passed (eight expected skips); Cairn Go, dashboard build and 25 dashboard tests passed. See studio-readability-integration-verification.json for source/environment identities and hashes of private raw evidence. This documentation-only follow-up does not change the tested application tree.

Sonar S5779 on studio_controls.py was inspected: the outer exception handler retains the traceback and appends to failed, and the script exits nonzero when failed is nonempty. It does not convert an assertion failure into success. Findings remain advisory; thresholds were not weakened. Hosted checks and merge state are separate from the local evidence.

The advisory reviewer also identified a remaining old hint-success string in the legacy roster presenter. Its assertion now matches the actual current 'Hint ready.' response, already exercised in the full NHANES browser control. This changes a rehearsal observer only; the UI and qualified NHANES path are unchanged. Focused tutoring tests passed. The older generic roster presenter was not independently requalified as a separate end-to-end product path.
