# Blocked storage admission draft evidence

This is a rejected implementation checkpoint. **Quota enforcement is not
implemented.** An exit-zero reproducer means the five recorded defects were
reproduced with their comparison controls; it is not product acceptance.

`draft.json` pins accepted base `2a7d6cd08ce7881a5ea36d761e53fc0c6f0e4b71`, the
three draft source hashes and the inert `draft.patch` SHA-256:
`aa4fa211bc0e7dff69096c21616c15c9d3d5b291eadcfc973fb033794f489c39`.
The patch is evidence, not an installation instruction. The application files
in the accepted Git tree are unchanged. Embedded hashes establish integrity,
not independent authenticity; retain the accepted evidence commit separately.

Run from a local Waypoint checkout containing the pinned base commit, using a
Unix Python installation with SQLite, `fcntl` and Git:

```sh
python3 research-workspace/evidence/2026-10-17/storage-admission-blocked/reproduce.py --repo "$PWD"
```

The reproducer reads only the pinned Git tree and the evidence files, reconstructs
the required package/recipe/catalog files in a new temporary directory, checks
the patch and source hashes, then runs synthetic controls. It does not apply the
patch to the caller's checkout. Git processes run only during reconstruction;
subsequent process launches are forbidden or intercepted. No Docker, broker,
identity provider, network, accepted pilot store or existing credential is used.
Only its disposable files are removed on exit.

`observation.json` records these deterministic results:

| Case | Boundary exercised | Comparison control |
| --- | --- | --- |
| Claim hold race | Real disposable SQLite/files; insert a hold at transaction entry after the preliminary check; intercept the attempted provider launch. | A claim with no inserted hold remains unheld. |
| Rollback failure | Actual draft transaction/begin/end/hold code with an in-memory coordinator and failing database stub. | A successful rollback clears the active marker and retains the hold for the original I/O failure. |
| Result before commit | Real disposable SQLite/files; supply a deliberately oversized internal run record, then exercise fallback settlement. | An injected post-result `OSError` retains the intent and hold. |
| Project initialization | Real disposable SQLite/files; run the worker loop during a stub broker's unlocked initialization. | Initialization without worker overlap remains unheld. |
| Consumed headroom | Inventory arithmetic with a 128 KiB database, 64 KiB protected unknown file and no runs; physical free-space check mocked. | One extra policy byte accepts the same essential check. |

The first headroom-model harness attempt selected a policy below the draft's
minimum and stopped at `limits()` before exercising its intended condition. The
corrected model includes the protected unknown file and validates the legal
policy before the comparison. This was a harness correction, not a product fix.
No disk-full or power-loss event was induced. Neither the oversized record nor
the inventory model is claimed to arise naturally from an ordinary recipe.

The [blocker record](../../../STORAGE-ADMISSION-BLOCKED.md) separates these
witnesses from remaining writer-bound and recovery work. No implementation
readiness, quota enforcement, deletion, source promotion or hosted/private-data
acceptance is established here.
