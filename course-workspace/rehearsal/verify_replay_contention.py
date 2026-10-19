"""Eight concurrent replay writers on real temporary SQLite; no network calls.

Run inside a disposable container with --cpus .5 to reproduce the controller
allowance. This measures replay admission only, not notebook/Guardian acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from authority_access import ReplayStore, publish
from hub.authority_client import AuthorityError, ERRORS, VERSION


def run(output):
    output = Path(output).resolve()
    if output.exists(): raise ValueError('fresh_output_required')
    proof = hashlib.sha256((ROOT / 'authority_access.py').read_bytes()).hexdigest()
    barrier = threading.Barrier(8)
    with tempfile.TemporaryDirectory(prefix='waypoint-replay-contention-') as directory:
        store = ReplayStore.create(Path(directory).resolve() / 'replay', 'a' * 32)
        def worker(_):
            barrier.wait(timeout=30)
            results = []
            for _ in range(50):
                frame = dict(version=VERSION, authority_id='a' * 32, nonce=uuid.uuid4().hex,
                    issued_at=int(time.time()), operation='guardian.observe', worker_id='one', course_id='main', payload={})
                started = time.monotonic()
                try:
                    store.consume(frame, 'b' * 64)
                    result = 'accepted'
                except AuthorityError as error:
                    result = error.code if error.code in ERRORS else 'other'
                results.append((result, time.monotonic() - started))
            return results
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [item for batch in pool.map(worker, range(8)) for item in batch]
        # Reopening replays and validates the complete durable history.
        store = ReplayStore.open(store.directory, 'a' * 32)
        with store._connection() as conn:
            durable = conn.execute('SELECT count(*) FROM requests').fetchone()[0]
        counts = {code: sum(value == code for value, _ in results) for code in sorted({'accepted', *(value for value, _ in results)})}
        assert durable == counts.get('accepted', 0)
        assert proof == hashlib.sha256((ROOT / 'authority_access.py').read_bytes()).hexdigest()
    report = dict(version='waypoint-replay-contention-v1', scope='local-single-process-eight-thread-real-sqlite',
        status='PASS' if counts.get('accepted') == 400 else 'FAIL', calls=400, outcomes=counts,
        durable_nonces=durable, maximum_seconds=max(duration for _, duration in results),
        authority_access_sha256=proof, temporary_state_removed=True,
        hosted_acceptance=False, notebook_acceptance=False, production_ready=False)
    publish(output, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
        help='fresh report file in an existing private directory')
    sys.exit(run(parser.parse_args().output))
