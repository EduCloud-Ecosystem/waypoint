# Research workspace

A working local research pilot: record a question and success criterion, give
collaborators project roles, run reviewed Python/R summaries on public or
synthetic CSV data, compare conditions, and export an inspectable evidence
bundle. It runs independently of the course workspace and makes no AI calls.

Waypoint owns project access and research records. The separate
[Outfitter broker](https://github.com/EduCloud-Ecosystem/outfitter) owns execution,
finite compute reservations, cancellation and cleanup. This release uses a
loopback-only identity adapter and Docker on the operator's computer. It is not
a shared institutional service or an arbitrary-code notebook.

## Start the local pilot

Requirements: Python 3.12+, Go 1.24+ to build Outfitter, and a running Docker
daemon. The service has no third-party Python or JavaScript dependencies. Run
the commands below from `research-workspace/`. Build the Outfitter revision
pinned in `.github/workflows/research-workspace.yml` with
`go build -o bin/outfitter ./cmd/outfitter` in its checkout.

Pull the reviewed immutable runtimes once. These references are also recorded
in `images.lock.json`; admission never pulls images implicitly.

```sh
docker pull python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
docker pull r-base@sha256:fa1972f31def171b83e0911e947ab8b57db143f0fc8a67af4c0d5ac329041646
```

Initialize a fresh private local directory, replacing the broker path with its
absolute path. Keep the resulting access key private. The following commands
save keys to ignored files readable only by their owner.

```sh
umask 077
mkdir -p .runtime
python3 -m research_workspace init \
  --state .runtime/workspace \
  --broker /absolute/path/to/outfitter/bin/outfitter \
  --subject researcher \
  --total-budget-cpu-seconds 600 \
  --python-image python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 \
  --r-image r-base@sha256:fa1972f31def171b83e0911e947ab8b57db143f0fc8a67af4c0d5ac329041646 \
  > .runtime/researcher.private.json
python3 -m research_workspace user-add \
  --state .runtime/workspace --subject collaborator \
  > .runtime/collaborator.private.json
python3 -m research_workspace serve --state .runtime/workspace --port 18765
```

Open **http://127.0.0.1:18765** and enter the access key from the private file.
The operator provisions subjects; a project owner can then add those subjects
as editors or viewers. Revoking a subject with `user-revoke` immediately prevents
new access and cancels its queued/running work. Treat the CLI and local Docker
access as operator privileges. Each active session expires after eight hours.

## Complete the first experiment

1. Create a project with a question, success criterion, end date and a 120
   CPU-second allowance. The workspace commits that allowance from its finite
   total; closing a project does not reset or recycle it.
2. Use the built-in sample or upload `examples/public-fixture.csv`. The format
   is exactly `group,value`, with at most 1,000 rows and 64 KiB. Use synthetic
   subject names and public/synthetic data only. Group labels are preserved exactly;
   values must use finite ASCII decimal notation (including decimal exponents),
   with absolute value at most 1e12. Numeric separators and Unicode digits are
   refused before any compute reservation.
3. Run the Python baseline. Run the R alternative against the same input.
   Baseline includes all observations. Alternative removes one minimum and one
   maximum per group if that group has at least five observations. These are
   descriptive demonstration methods, not recommended study designs.
4. Inspect history and comparison. Both runtime outputs are independently
   checked against the declared method before a result becomes successful.
5. Download evidence and verify it:
   `python3 -m research_workspace verify-bundle /path/to/evidence.zip`.
6. Create a second project with `examples/second-project.csv` and repeat without
   changing source code. Have a second person follow these steps before claiming
   independent human reproduction.

Each admitted run reserves at most 30 CPU-seconds and is conservatively charged
that full ceiling after verified cleanup, even if it finishes sooner. This is
an estimate, not measured CPU time or money. The recipe runs without network
access as a non-root user, with read-only inputs and limits on memory, CPU,
processes, output and wall time. Only the two packaged recipes can be selected.

## Failure and recovery

The history retains failed, cancelled and interrupted attempts. Project closure
and end dates stop new execution and publication; authorized members retain
evidence access. Removed members cannot read or export. A repeated API request
with the same idempotency key returns the original run; a conflicting payload
is refused. The browser does not silently retry submissions.

Restarting the service marks an unfinished run interrupted, preserves unknown
accounting and does not replay it. Outfitter's separate watchdog continues
cleanup after the submitting process exits. Its guarantee still depends on the
watchdog, host and Docker daemon remaining available; it is not provider-native
self-expiry. An independently supervised `outfitter ... reap` is recommended
for an operated pilot. Keep the exact broker executable and state available.

For an interrupted run, an operator can inspect the private broker `status`,
request `cancel` or `reconcile`, and retain the resulting receipt. See
Outfitter's `docs/LOCAL-RESEARCH-BROKER.md`. Reconciliation updates the broker
ledger; this first workspace does not retroactively rewrite the original run
record or publish uncertain results. Unknown liability is never assumed zero.

Keep the entire state directory together: SQLite authority, immutable run
files, configuration and broker ledger. Do not delete or restore an older
ledger to reset allowances, copy a live SQLite database by itself, or place
this store on network-mounted storage. [RECOVERY.md](RECOVERY.md) describes
private offline checkpoints and a held replacement that activates only after
the current source is verified and fenced. Off-host recovery, automatic
retention/deletion and store-wide disk quotas remain open. Stop admission if
authority is lost and preserve the original state.

## Verification and expansion

Run `python3 -m unittest discover -s tests -v` for authorization, admission,
restart, receipt, and evidence controls. `verify.py` exercises actual HTTP,
Outfitter and Docker using both pinned runtimes and two different datasets:

```sh
python3 verify.py \
  --broker /absolute/path/to/outfitter/bin/outfitter \
  --python-image python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36 \
  --r-image r-base@sha256:fa1972f31def171b83e0911e947ab8b57db143f0fc8a67af4c0d5ac329041646 \
  --output .runtime/acceptance-new
```

Use a fresh output directory. Only the public synthetic bundles and verification
JSON are suitable for sharing; `state.private/` contains private authority and
operator diagnostics. CI uploads an explicit allowlist of public artifacts.

Read [VERIFICATION.md](VERIFICATION.md) for observed evidence and failed controls,
[CONTRACT.md](CONTRACT.md) for the integration/API boundary, and
[EVIDENCE.md](EVIDENCE.md) for bundle guarantees. The next institutional gates
are campus identity, shared hosting, off-host recovery, approved data storage
and retention, and independently verified provider cleanup/accounting. Real
participant data, live cloud provisioning and scientific validity are not
accepted by this local proof of concept.
