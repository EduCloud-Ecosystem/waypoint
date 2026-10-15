"""Actual local mTLS worker-process rehearsal; no hosted/runtime-capacity claim."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import evidence
from ownership_contract import OwnershipError, digest
from remote_worker_fixture import RemoteFixture, read_private, private_json, refresh_snapshot
from worker_transport import WorkerError

VERSION = 'educloud-local-remote-workers-v1'
CHECKS = (
    'two-independent-mtls-worker-processes', 'source-and-peer-actual-processes',
    'retained-byte-equivalence', 'post-fence-controller-interruption',
    'both-endpoint-durable-quarantine', 'pending-source-activation-refused',
    'offline-listener-refuses', 'listener-reconnect-preserves-peer',
    'exact-resume-clears-quarantine', 'no-automatic-target-start',
    'explicit-target-start-retains-bytes', 'peer-process-and-work-unchanged',
    'post-fence-tls-disconnect', 'disconnected-source-stays-pending',
    'reconnected-exact-resume', 'old-plan-divergence-refused',
    'fresh-reviewed-reconciliation', 'independent-divergent-copies-retained',
    'append-only-worker-barrier-history', 'wrong-server-pin-refused',
    'changed-worker-incarnation-refused', 'arbitrary-runtime-options-refused',
    'agent-restart-preserves-journal-and-floor', 'old-epoch-revival-refused',
    'new-owner-epoch-explicitly-starts', 'peer-restarts-with-saved-bytes',
    'owned-process-cleanup-verified',
)
CODE_FILES = (
    'ownership_contract.py', 'remote_backend.py', 'remote_control.py', 'worker_transport.py', 'worker_channel_state.py',
    'rehearsal/remote_worker_fixture.py', 'rehearsal/verify_remote_workers.py', 'rehearsal/test_remote_workers.py',
    'rehearsal/verify_ownership_contract.py', 'rehearsal/evidence.py', 'tests/authority_test_pki.py',
)
FLAGS = {'fixture_only': True, 'authenticated_mtls_exercised': True,
    'actual_two_host_isolation': False, 'production_activation_adapter': False,
    'provider_adapter': False, 'runtime_enforcement': False,
    'measured_profile_enforcement': False, 'encrypted_backup': False,
    'off_host_backup': False, 'docker_exercised': False, 'notebooks_exercised': False}


def require(value):
    if not value: raise ValueError('remote_worker_control_failed')


def refused(callback, expected=None):
    try: callback()
    except (OwnershipError, WorkerError) as exc:
        if expected is not None: require(exc.code == expected)
        return
    raise ValueError('remote_worker_expected_refusal_missing')


def _source():
    source = evidence._source(ROOT)
    if source['dirty']: raise ValueError('clean_source_required')
    return {'revision': source['revision'], 'code_sha256': {name: evidence.sha256_file(ROOT / name) for name in CODE_FILES}}


def publish_pass(directory, value):
    fields = {'version', 'status', 'scope', 'source', 'checks', 'flags', 'logical_workers',
        'concurrent_worker_server_processes', 'worker_agent_restarts', 'real_worker_hosts', 'successful_transfers', 'successful_reconciliations',
        'listener_reconnect_peer_continuity', 'agent_restart_peer_continuity', 'cleanup_verified'}
    if (not isinstance(value, dict) or set(value) != fields or value['version'] != VERSION
            or value['status'] != 'PASS' or value['scope'] != 'two-local-process-workers'
            or value['checks'] != list(CHECKS) or value['flags'] != FLAGS
            or any(type(value[key]) is not int or value[key] != expected for key, expected in {
                'logical_workers': 2, 'concurrent_worker_server_processes': 2, 'worker_agent_restarts': 1, 'real_worker_hosts': 0,
                'successful_transfers': 2, 'successful_reconciliations': 1}.items())
            or value['listener_reconnect_peer_continuity'] is not True
            or value['agent_restart_peer_continuity'] is not False or value['cleanup_verified'] is not True):
        raise ValueError('remote_worker_report_invalid')
    source = value['source']
    if (not isinstance(source, dict) or set(source) != {'revision', 'code_sha256'}
            or not isinstance(source['revision'], str) or not evidence.REVISION.fullmatch(source['revision'])
            or not isinstance(source['code_sha256'], dict) or set(source['code_sha256']) != set(CODE_FILES)
            or any(not isinstance(v, str) or not evidence.HASH.fullmatch(v) for v in source['code_sha256'].values())):
        raise ValueError('remote_worker_provenance_invalid')
    for key, expected in FLAGS.items():
        if value['flags'][key] is not expected: raise ValueError('remote_worker_flags_invalid')
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode() + b'\n'
    if len(raw) > 16384: raise ValueError('remote_worker_report_bound')
    directory = Path(directory)
    if (directory.is_symlink() or not directory.is_dir() or directory.stat().st_uid != os.geteuid()
            or directory.stat().st_mode & 0o077): raise ValueError('private_report_directory_required')
    final = directory / 'REMOTE-WORKERS-PASS.json'
    fd, temporary = tempfile.mkstemp(prefix='.remote-workers-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, final, follow_symlinks=False)
    finally: os.unlink(temporary)
    return final


def history(directory):
    with sqlite3.connect((directory / 'barriers.sqlite').as_uri() + '?mode=ro', uri=True) as connection:
        return connection.execute('SELECT sequence,record,digest FROM history ORDER BY sequence').fetchall()


def publish_failure(output, source, checks, cleanup, failure):
    value = {'version': VERSION, 'status': 'FAIL', 'scope': 'two-local-process-workers',
        'source': source, 'completed_checks': list(checks), 'cleanup_verified': cleanup,
        'failure_type': type(failure).__name__ if failure is not None else 'CleanupError'}
    raw = json.dumps(value, sort_keys=True, allow_nan=False).encode() + b'\n'
    require(len(raw) <= 16384)
    fd = os.open(output / 'REMOTE-WORKERS-FAIL.json', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    print(json.dumps(value, sort_keys=True))


def run(output):
    source = _source(); checks = []
    output = Path(output)
    if os.path.lexists(output): raise ValueError('fresh_report_directory_required')
    output = output.resolve(); output.mkdir(mode=0o700, parents=True, exist_ok=False)
    temporary = tempfile.TemporaryDirectory(prefix='educloud-remote-worker-')
    fixture = None; failed = None; cleanup = False
    def mark(name):
        require(name in CHECKS and name not in checks); checks.append(name)
    try:
        fixture = RemoteFixture(Path(temporary.name) / 'private')
        a = fixture.authority; one = fixture.directories['worker-one']; two = fixture.directories['worker-two']
        require(len({p.child.pid for p in fixture.workers.values()}) == 2)
        mark('two-independent-mtls-worker-processes')
        main = fixture.runtime('worker-one', 'main-course', 'hub.spawn')
        peer = fixture.runtime('worker-one', 'peer-course', 'hub.spawn')
        require(main['running'] and peer['running']); mark('source-and-peer-actual-processes')
        saved = main['work_sha256']; require(evidence.sha256_file(two / 'courses/main-course/retained/saved.txt') == saved)
        mark('retained-byte-equivalence')
        plan = fixture.root / 'first.json'; a.preview('main-course', 'worker-two', plan)
        faults = []
        def fault(stage, _):
            if stage == 'fenced':
                faults.append(stage); raise RuntimeError('controlled_post_fence_fault')
        with patch('ownership_contract.fault_hook', side_effect=fault): refused(lambda: a.apply(plan))
        require(faults == ['fenced'])
        mark('post-fence-controller-interruption')
        require(all(fixture.state(w)['quarantined'] is True for w in fixture.workers)); mark('both-endpoint-durable-quarantine')
        refused(lambda: fixture.runtime('worker-one', 'main-course', 'hub.spawn'), 'ownership_transfer_pending'); mark('pending-source-activation-refused')
        require(fixture.workers['worker-one'].command('pause') == {'paused': True})
        refused(lambda: fixture.backend.inspect_worker('worker-one'), 'worker_transport_unavailable'); mark('offline-listener-refuses')
        fixture.workers['worker-one'].command('resume')
        require(fixture.runtime('worker-one', 'peer-course') == peer); mark('listener-reconnect-preserves-peer')
        require(a.apply(plan)['status'] == 'COMPLETE')
        require(all(fixture.state(w)['quarantined'] is False for w in fixture.workers)); mark('exact-resume-clears-quarantine')
        require(fixture.runtime('worker-two', 'main-course')['running'] is False); mark('no-automatic-target-start')
        target = fixture.runtime('worker-two', 'main-course', 'hub.spawn')
        require(target['running'] and target['work_sha256'] == saved); mark('explicit-target-start-retains-bytes')
        require(fixture.runtime('worker-one', 'peer-course') == peer); mark('peer-process-and-work-unchanged')

        second = fixture.root / 'second.json'; a.preview('main-course', 'worker-one', second)
        import worker_transport
        call = worker_transport._Channel.call; disconnected = []
        def disconnect(channel, operation, payload=None):
            result = call(channel, operation, payload)
            if operation == 'fence' and channel.challenge['worker_id'] == 'worker-two' and not disconnected:
                disconnected.append(True); channel.stream.shutdown(socket.SHUT_RDWR); channel.stream.close()
                raise WorkerError('worker_transport_unavailable')
            return result
        with patch('worker_transport._Channel.call', new=disconnect): refused(lambda: a.apply(second))
        require(disconnected == [True] and fixture.state('worker-two')['quarantined']); mark('post-fence-tls-disconnect')
        fixture.workers['worker-two'].command('pause')
        refused(lambda: a.apply(second)); require(a.inventory()['pending_transfers'] == 1)
        require(fixture.runtime('worker-one', 'peer-course') == peer); mark('disconnected-source-stays-pending')
        fixture.workers['worker-two'].command('resume'); require(a.apply(second)['status'] == 'COMPLETE')
        mark('reconnected-exact-resume')
        fixture.runtime('worker-one', 'main-course', 'hub.spawn')

        third = fixture.root / 'third.json'; a.preview('main-course', 'worker-two', third)
        with patch('ownership_contract.fault_hook', side_effect=fault): refused(lambda: a.apply(third))
        require(fixture.runtime('worker-one', 'peer-course') == peer)
        fixture.runtime('worker-one', 'peer-course', 'hub.stop')
        before_restart = history(one); floor = read_private(third)['old_epoch']
        require(fixture.state('worker-one')['quarantined'] is True)
        fixture.restart('worker-one')
        require(history(one) == before_restart and fixture.state('worker-one')['quarantined'] is True)
        require(fixture.backend.inspect_course('main-course', 'worker-one')['admission_floor'] == floor + 1)
        mark('agent-restart-preserves-journal-and-floor')
        refused(lambda: fixture.runtime('worker-one', 'main-course', 'hub.spawn'), 'ownership_transfer_pending')
        baseline_history = {w: history(d) for w, d in fixture.directories.items()}
        copies = {}; snapshots = {}
        for worker, directory in fixture.directories.items():
            path = directory / 'courses/main-course/retained/saved.txt'
            path.write_text('independent retained copy ' + worker + '\n'); path.chmod(0o600)
            refresh_snapshot(directory, 'main-course'); copies[worker] = path.read_bytes()
            marker = directory / 'courses/main-course/snapshot.fixture'; snapshots[worker] = marker.read_bytes()
            require(marker.read_text().splitlines()[-1] == fixture.backend.inspect_course('main-course', worker)['retained_tree_sha256'])
        refused(lambda: a.apply(third), 'retained_backup_runtime_or_profile_changed'); mark('old-plan-divergence-refused')
        recovery = fixture.root / 'recovery.json'; a.reconcile_preview('main-course', 'worker-one', recovery)
        require(a.reconcile_apply(recovery)['status'] == 'COMPLETE'); mark('fresh-reviewed-reconciliation')
        require(copies['worker-one'] != copies['worker-two'])
        require(all((d / 'courses/main-course/retained/saved.txt').read_bytes() == copies[w] for w, d in fixture.directories.items()))
        require(all((d / 'courses/main-course/snapshot.fixture').read_bytes() == snapshots[w] for w, d in fixture.directories.items()))
        mark('independent-divergent-copies-retained')
        require(all(history(d)[:len(baseline_history[w])] == baseline_history[w] for w, d in fixture.directories.items()))
        mark('append-only-worker-barrier-history')

        client = fixture.clients['worker-one']; pin = client.server_sha256
        healthy = fixture.backend.inspect_worker('worker-one'); inventory = a.inventory()
        try:
            client.server_sha256 = '0' * 64; refused(lambda: fixture.backend.inspect_worker('worker-one'), 'worker_server_pin_changed')
        finally: client.server_sha256 = pin
        require(fixture.backend.inspect_worker('worker-one') == healthy and a.inventory() == inventory)
        mark('wrong-server-pin-refused')
        identity_path = one / 'identity.json'; identity = read_private(identity_path)
        try:
            private_json(identity_path, dict(identity, boot_id='f' * 32))
            refused(lambda: fixture.backend.inspect_worker('worker-one'), 'worker_identity_changed')
        finally: private_json(identity_path, identity)
        require(fixture.backend.inspect_worker('worker-one') == healthy and a.inventory() == inventory)
        mark('changed-worker-incarnation-refused')
        with a.admission('main-course', 'worker-one'):
            refused(lambda: fixture.backend.runtime('worker-one', 'main-course', 'hub.spawn', {'command': 'private-command-canary'}), 'worker_channel_refused')
        require(evidence.sha256_file(one / 'courses/peer-course/retained/saved.txt') == peer['work_sha256'] and a.inventory() == inventory)
        mark('arbitrary-runtime-options-refused')
        request = {'authority_id': fixture.authority_id, 'worker_id': 'worker-one', 'course_id': 'main-course',
            'provider_id': identity['provider_id'], 'boot_id': identity['boot_id'], 'epoch': floor}
        def stale():
            with fixture.clients['worker-one'].session('main-course', 'activate', request):
                raise ValueError('old_epoch_was_admitted')
        refused(stale, 'worker_channel_refused'); mark('old-epoch-revival-refused')
        owner = fixture.runtime('worker-one', 'main-course', 'hub.spawn')
        require(owner['running'] and owner['work_sha256'] == hashlib.sha256(copies['worker-one']).hexdigest())
        mark('new-owner-epoch-explicitly-starts')
        replacement_peer = fixture.runtime('worker-one', 'peer-course', 'hub.spawn')
        require(replacement_peer['running'] and replacement_peer['work_sha256'] == peer['work_sha256']
            and replacement_peer['process_sha256'] != peer['process_sha256']); mark('peer-restarts-with-saved-bytes')
    except Exception as exc:
        failed = exc
    finally:
        try:
            if fixture is not None: fixture.close(); cleanup = fixture.cleanup_verified
            # Constructor failure may include partial worker startup. Its
            # internal cleanup is attempted, but an unreturned fixture cannot
            # supply the positive owned-resource proof required here.
            else: cleanup = False
        except Exception: cleanup = False
        temporary.cleanup()
    if failed is None and cleanup:
        try: require(_source() == source)
        except Exception as exc: failed = exc
    if failed is not None or not cleanup:
        # Deliberately bounded failures remain visible; no partial PASS packet.
        publish_failure(output, source, checks, cleanup, failed)
        raise ValueError('remote_worker_rehearsal_failed') from None
    mark('owned-process-cleanup-verified'); require(set(checks) == set(CHECKS))
    return publish_pass(output, {'version': VERSION, 'status': 'PASS', 'scope': 'two-local-process-workers',
        'source': source, 'checks': list(CHECKS), 'flags': dict(FLAGS), 'logical_workers': 2,
        'concurrent_worker_server_processes': 2, 'worker_agent_restarts': 1, 'real_worker_hosts': 0, 'successful_transfers': 2,
        'successful_reconciliations': 1, 'listener_reconnect_peer_continuity': True,
        'agent_restart_peer_continuity': False, 'cleanup_verified': True})


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--output', required=True); args = parser.parse_args()
    try:
        run(args.output); print('remote-worker-rehearsal:PASS')
    except Exception: raise SystemExit('remote_worker_rehearsal_failed') from None
