# Studio readability integration — November 15, 2026

Owner: Greg Chism; Codex integration in the existing Studio worktree. Scope: integrate the UI chat's c314c90 through e50b66d follow-up with current main 2112a38. The accepted 9b81504 application baseline and current main differ only in maintenance policy; policy files are preserved. Permitted changes: tutor/file-picker presentation, submission browser controls, finite instructor launcher pins and verification records. No new service, student admission or spending limit.

Cairn producer/consumer pin: 533c16a414b9fef1979d0de783b3436f31dd03a7. Belay-classroom c5a27073e74b9eeb7ab697190915378b50132883 and Portage b72881f009e61e1311b6e46b92b42841991fd163 remain unchanged. All modes use the reviewed Cairn UI; live retains at most seven requests, $0.175 reserved and the original tutor deadline. Human review and actual deployment remain separate.

Initial test invocation incorrectly selected pytest, which is absent from the cached driver environment. No installation was made. Corrected to the repository's unittest commands; their logs and all subsequent full-workflow failures are retained under output/readable-integration and timestamped output directories. Acceptance results will be added after execution. Rollback: previous merged application source, preserving evidence and receipts.
