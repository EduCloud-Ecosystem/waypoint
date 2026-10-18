# Standard CI broker adoption

The standard `actual-docker` job in
[the research workspace workflow](../.github/workflows/research-workspace.yml)
now builds Outfitter `5446438985e31cb739f2048a2aa2176918831a7e`
(accepted Outfitter PR #16 merge). This follows Waypoint PR #44, merged at
`2317eac63a0bad0d691457b0cde2de14bf6bcc7f`, and its
[two-binary qualification and retained review evidence](evidence/2026-10-18/broker-upgrade/integration-after-pr43/README.md).
Historical evidence remains attributed to its original source and broker pins.

The optional, manually requested `clean-source-builds` job deliberately retains
Outfitter `5827c84e9952cc67b85e91ceff4282e0ead6923b` as the frozen source-build
qualification pin. It is a separate qualification track, not the current
standard integration pin. Its historical contracts, archives, manifests and
disabled source-candidate promotion status are unchanged.

For this adoption, review `controls` and `actual-docker` on the exact PR head.
The existing Docker job exercises HTTP/accounting/export, historical replay,
coordinated recovery and guarded dispatch/recovery against the new broker.
Inspect its `research-public-evidence` artifact for nonempty passing controls,
expected replay/refusal outcomes and verified cleanup; a skipped optional source
build is not new source-build acceptance. No additional local Docker run is
required by this CI-only change.

This changes a CI checkout only. It does not replace an operator's local broker,
cut over an accepted workspace, alter production/profile/default routing,
promote source-build images, enable native/consumer/cloud execution, change
allocation or credential choices, or establish physical storage or all-writer
finality. Any operational rollout retains its own reviewed state/recovery gates.
