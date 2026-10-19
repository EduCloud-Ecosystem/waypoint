# Synthetic assessment wire fixtures

These are synthetic metadata vectors for Waypoint manifest preparation and the
Outfitter pure receipt validator. They do not establish an actual assessment,
reservation, observed resource consumption, verified cleanup or operator consent.
The historical legacy shape represents one conservative 30 CPU-second charge;
all actual-use counters are unsupported and provider charges are unknown.

The manifest declares one fitting/10-row/repetition-1/analysis reservation out
of twelve planned slots. Its fixed expiry is October 20, 2026. Keep the vector
unchanged when it expires; tests of expiry use fixed clocks or separate inputs.

Canonical bytes use recursively sorted keys, ASCII JSON, integer tokens and
no whitespace or newline. SHA-256 values:

| Object | SHA-256 |
| --- | --- |
| `research-default.json` | `506a3587d73e70c1d545631ea2dd455056db92570e394230d5f56d81a7738420` |
| `legacy-receipts.json` | `27061101e45bcff439f0e3bfff871d4f3bfe82ec6e02afb7410492e1b318d809` |
| Measurement object | `36805e95c8852613a944509c98d1ab5c4a6cba1b842c489a020ac85fc586a52d` |
| Cleanup object | `e0e747be8a561293893585aec0dbc48e36d90903cf14c34af7160999959e9cf5` |

`legacy-validation.json` is the agreed expected validator response, not a receipt
authenticated by running a worker. The fixture was aligned with Outfitter on
2026-10-19. Legacy termination/access revocation stay unknown, cleanup stays
unresolved, and reported execution-resource deletion remains an untrusted claim.

See [ASSESSMENT.md](../../ASSESSMENT.md) for the trust boundary and verification
scope. The Waypoint transport tests use a local stub; real cross-component
validator integration is a separate check.

The `estimation/` subdirectory contains invented series and a closed scenario for
the [synthetic estimator](../../ESTIMATION.md). Regenerate them with
`python3 assessment/fixtures/generate_estimation_fixtures.py --output assessment/fixtures/estimation`
from `research-workspace`. The readable generator documents every value; the
JSON files use canonical compact bytes. No benchmark or workload is run.
