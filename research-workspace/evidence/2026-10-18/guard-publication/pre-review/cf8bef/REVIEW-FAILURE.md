# Rejected original guard-publication candidate

Independent review rejected immutable commit
`cf8befca9cef418d7e1238945872bed45d7fc6f2`. The original acceptance manifest
SHA-256 is `c8f04225df57beed2077d6c960e6765f636cc68167f9afd4795d13c31b638938`.
These original files and the independent review artifacts are retained unchanged.

Cancellation, revocation, closure and expiry all reproduced a committed
cancellation followed by `missing_operation` and a latched `operation_failed`.
The original 364 passing tests and CI omitted this path. The review also recorded
30 passing publication tests, nine passing adversarial controls, and five
detected deliberately unsafe mutations; those are bounded evidence, not an
approval of the rejected candidate.

Coordinating review also reproduced a distinct cross-process takeover refusal
before request file preparation. `root-crossprocess-takeover.json` retains the
exact probe source, five reviewed source hashes and observed result at `cf8bef`.
It made no provider, network or Docker calls. The corrected worker qualifies an
intervening valid publication before the external effect, while retaining the
final dispatch authorization check and prohibiting takeover within business SQL.

To replay the four-case witness in a new disposable directory, run
`python3 replay_cf8bef.py --repository /path/to/waypoint --output /tmp/fresh-review`.
The repository needs the exact original commit available. The wrapper exports
that commit without requiring its branch to be checked out and copies the
unchanged witness beside the disposable export. It also verifies all five
coordinating-review source hashes and reruns that exact cross-process probe.
It refuses an existing output directory, records exact source hashes, and checks
for both original failures.
Pass `--full-review` to additionally rerun the original publication, adversarial
and negative controls. Providers remain simulated; no Docker service is used.

The preserved original `independent/prepare.py` records the original execution
paths and clean-head assertions. Use the wrapper for a fresh replay after the
source checkout has advanced. Original logs and JSON reports retain their
original paths and results; they are not rewritten to describe the correction.
