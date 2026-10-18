# Final dispatch correction evidence

The [original PR37 witness](pr37-red/README.md) is an unchanged, source-pinned
failure record. Its earlier-denial controls pass; its three final-boundary
denials fail. Re-running it produced the byte-identical original observation,
with all process launches intercepted. Exit zero means the defect reproduced.

The separate [local verification](local-verification.json) binds the correction
source and records 295 passing offline controls, including 60 final-dispatch
tests. Fourteen focused controls were independently rerun to verify the two
review findings: superseding accounting and process ownership after fence errors.
Both profiles retain unknown liability after possible launch; new late-denial
cases make zero launch attempts. Tests cover legitimate public/OIDC execution
and both concurrent control/launch orderings. No actual provider process is used
by the final-dispatch test file; broader Docker acceptance is recorded by CI.

During development, two new integrity-refusal tests initially stopped because
the fixture input was correctly read-only. The fault harness now explicitly
makes its disposable input writable before corrupting it. This was a harness
setup correction, not a relaxation of application file permissions.

The [contract and reproduction command](../../../FINAL-DISPATCH.md) define the
scope. Exact-head CI and the requested root recheck remain release requirements,
recorded separately from this local evidence. All 137 prior evidence files,
the disabled source catalog and rejected draft retain their original bytes.
Cleanup and headroom gates remain open; quotas are not enforced.
