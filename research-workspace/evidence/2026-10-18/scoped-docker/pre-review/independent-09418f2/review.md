# Candidate02 independent review: HOLD

Exact source: `09418f231187a5465ba9d0b8546b3b59514959e9`.

**D5 — Final lineage is not tied to the prelaunch hashes** (`qualify_scoped_docker.py:298–301`, `314–326`). The final HTTP run input and recipe hashes are never compared with the recorded prelaunch binding. An export that agrees with that later record can therefore replace input or recipe bytes and update their hashes without failing qualification.

Two independent synthetic witnesses returned PASS with four attempts and120 charges while all four final input hashes, or all four recipe hashes, differed from their prelaunch binding. Replacement bytes were a newline added to the exports; later metadata was changed consistently. No source, actual provider, private production state or original execution evidence was altered. Bind final hashes and fixed recipe/condition to the prelaunch record before accepting settlement, including the final oracle.

Previous diagnostic and project-authority findings are fixed. Export-only numeric/unknown mutations and absence-failure charge-retention controls reject correctly. Five prior independent witnesses reject, and13 additional captured controls behave as expected. All22 focused tests pass in normal and optimized modes. These passing controls do not waive the two new false-PASS witnesses.

The34-file prepared archive matches the source capture. Broker binary `1bf1ce898d6d10e574ab7b2f79a830fa94fa740867e208470bfc15790dc41bb1`, accepted source `5446438985e31cb739f2048a2aa2176918831a7e`, retained build attestation and all22 compiled source hashes match their recorded identities. All1,111 captured repository files remain unchanged. Real marker/output remain absent. Previous review files and protected worktrees are untouched.

No real Docker, broker, production CLI initialization, prepare/run or provider operation occurred. Candidate02 is not cleared for execution or merge. JSON report contains exact hashes, evidence paths and test boundaries.
