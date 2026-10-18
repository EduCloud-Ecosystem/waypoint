# Preserved initial append witness

These files preserve commit `5168434554d0aa1c8276acc63adaefeffd1aa95f`'s initial
qualification observation, Python harness, manifest and independent review.
They are historical evidence, not the current qualification or acceptance.

Root review found that the initial append control passed the same post-write
inventory as expected and observed. It established an arithmetic example but
did not independently establish expected paths and content. Its initial
no-blocker review therefore missed this evidentiary weakness. The current
harness derives the expected manifest before submission and exercises actual
same-length content mutation and unexpected-file negative controls.

The archived harness is inert source evidence in this directory. Its relative
imports are not a reproduction entry point; use the corrected qualification
entry point described in the current contract. The earlier manifest retains
its original paths/hashes as a historical record; the current parent manifest
hashes the archived payloads at these new preservation paths.
