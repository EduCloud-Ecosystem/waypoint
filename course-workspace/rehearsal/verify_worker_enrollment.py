#!/usr/bin/env python3
"""Actual local worker enrollment and certificate rotation, synthetic coursework."""
import argparse
import json
import math
from pathlib import Path
import time

import requests

import worker_enrollment_evidence as evidence
from verify_authenticated_runtime import run
from smoke import kernel


def await_hub_authorization(call, worker='worker-one', course='course-main', *, timeout=30,
                            monotonic=time.monotonic, sleep=time.sleep):
    """Await startup transport readiness; logical denials never become retries."""
    deadline = monotonic() + timeout
    while True:
        reply = call()
        if isinstance(reply, dict) and reply.get('ok') is True:
            assert set(reply) == {'ok', 'result'}
            result = reply.get('result')
            assert (isinstance(result, dict) and set(result) == {'authorized', 'epoch', 'worker_id', 'course_id'}
                and result['authorized'] is True and type(result['epoch']) is int and 1 <= result['epoch'] <= 10**9
                and result['worker_id'] == worker and result['course_id'] == course)
            return
        assert (isinstance(reply, dict) and set(reply) == {'ok', 'refused', 'code'}
            and reply['ok'] is False and reply['refused'] is True and reply['code'] == 'authority_transport_unavailable')
        if monotonic() >= deadline: raise RuntimeError('fixture_hub_startup_unavailable')
        sleep(0.2)


def await_guardian_authorization(call, *, timeout=5, monotonic=time.monotonic, sleep=time.sleep):
    """Retry only explicit authenticated acquisition deferrals, never failures."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 10:
        raise ValueError('fixture_guardian_probe_timeout_invalid')
    deadline = monotonic() + timeout
    while True:
        reply = call()
        if isinstance(reply, dict) and set(reply) == {'authorized'} and reply['authorized'] is True:
            return
        if (not isinstance(reply, dict) or set(reply) != {'authorized', 'deferred'}
                or reply['authorized'] is not False or reply['deferred'] is not True):
            raise ValueError('fixture_guardian_probe_invalid')
        if monotonic() >= deadline: raise RuntimeError('fixture_guardian_probe_busy')
        sleep(min(.1, max(0, deadline - monotonic())))


def verify_contention_batch(result, *, expiry):
    assert isinstance(result, dict) and set(result) == {'observations', 'expiry_while_held'}
    assert result['expiry_while_held'] is expiry
    assert isinstance(result['observations'], list) and len(result['observations']) == 2
    for observed in result['observations']:
        assert isinstance(observed, dict) and set(observed) == {'deferred', 'held_before', 'held_after', 'heartbeat_unchanged', 'elapsed_ms'}
        assert all(observed[key] is True for key in ('deferred', 'held_before', 'held_after', 'heartbeat_unchanged'))
        assert type(observed['elapsed_ms']) in (int, float) and 0 <= observed['elapsed_ms'] < 3000


def await_contention_release(probe, *, timeout=10, monotonic=time.monotonic, sleep=time.sleep):
    """Keep bounded release/exit/renewal evidence; never swallow probe failures."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 10:
        raise ValueError('fixture_contention_timeout_invalid')
    deadline = monotonic() + timeout
    previous = None
    keys = {'held', 'heartbeat_unchanged', 'heartbeat_expired', 'holder_exited', 'heartbeat_renewed'}
    while True:
        try: value = probe()
        except Exception as error:
            category = type(error).__name__
            if category not in {'CalledProcessError', 'TimeoutExpired', 'RuntimeError', 'ValueError', 'OSError'}:
                category = 'OtherFailure'
            print('CONTENTION_STATUS_REFUSAL ' + json.dumps({'category': category}), flush=True)
            raise RuntimeError('fixture_contention_status_unavailable') from None
        if (not isinstance(value, dict) or set(value) != keys or any(type(item) is not bool for item in value.values())
                or value['heartbeat_renewed'] and value['heartbeat_unchanged']):
            raise ValueError('fixture_contention_status_invalid')
        if value != previous:
            print('CONTENTION_STATUS ' + json.dumps(value, sort_keys=True), flush=True)
            previous = dict(value)
        if value['heartbeat_expired']:
            raise RuntimeError('fixture_contention_heartbeat_expired')
        remaining = deadline - monotonic()
        if remaining < 0: raise RuntimeError('fixture_contention_release_timeout')
        if not value['held'] and value['holder_exited'] and value['heartbeat_renewed']:
            return
        if remaining <= 0: raise RuntimeError('fixture_contention_release_timeout')
        sleep(min(.25, remaining))


def exercise(ctx):
    helper, rpc, phase = ctx['helper'], ctx['rpc'], ctx['phase']
    wait, learners, notebook, read = ctx['wait'], ctx['learners'], ctx['notebook'], ctx['read']
    bases = ctx['bases']
    helper('enrollment-create', {})
    ctx['service']()
    assert helper('enrollment-negative', {}) == {'unknown_worker_refused': True}
    for identity, worker in (('source', 'worker-one'), ('peer', 'worker-one'), ('receiver', 'worker-two')):
        course = 'course-peer' if identity == 'peer' else 'course-main'
        denied = rpc('hub.authorize', identity=identity, worker=worker, course=course)
        assert denied.get('ok') is False and denied.get('code') == 'authority_access_refused'
    phase(evidence.PHASES[0])
    for worker, identity in (('worker-one', 'source'), ('worker-two', 'receiver')):
        helper('enrollment-preview', {'worker': worker})
        assert rpc('hub.authorize', identity=identity, worker=worker)['ok'] is False
        assert helper('enrollment-apply', {'worker': worker, 'wrong_digest': True}) == {'refused': True}
        assert helper('enrollment-apply', {'worker': worker}) == {'enrolled': True}
        assert helper('enrollment-apply', {'worker': worker, 'expect_replay': True}) == {'refused': True}
    assert rpc('hub.authorize', identity='source')['ok'] is True
    assert rpc('hub.authorize', identity='source', worker='worker-two')['ok'] is False
    assert rpc('hub.authorize', identity='source', course='course-peer')['ok'] is False
    phase(evidence.PHASES[1])
    helper('launch')
    for base in bases.values(): wait(lambda: requests.get(base + '/hub/login', timeout=3).status_code == 200, 60)
    sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
    from smoke import change
    for suffix, session in sessions.items():
        kernel(session, bases[suffix], 'alice', 'python3', "print('CONTINUITY_OK')")
        kernel(session, bases[suffix], 'alice', 'ir', "cat('CONTINUITY_OK\\n')")
        response = change(session, 'PUT', bases[suffix] + '/user/alice/api/contents/coursework.txt',
            json={'type': 'file', 'format': 'text', 'content': 'synthetic coursework retained\n'})
        response.raise_for_status(); read(session, suffix)
        hub = json.loads(ctx['command']('inspect', ctx['name'] + '-' + suffix + '-hub'))[0]
        assert all(mount['Destination'] != '/var/run/docker.sock' for mount in hub['Mounts'])
        learner = json.loads(ctx['command']('inspect', learners(suffix)[0]))[0]
        host = learner['HostConfig']
        assert host['ReadonlyRootfs'] and not host['Privileged'] and host['PidsLimit'] == 128
        assert host['Memory'] == host['MemorySwap'] == 512 * 1024**2
        assert host['CpuQuota'] / host['CpuPeriod'] == ctx['allocation']['learner_cpu']
        assert host['RestartPolicy']['Name'] == 'no' and learner['Config']['User'] == '1000:1000'
        assert len(learner['Mounts']) == 1 and learner['Mounts'][0]['Destination'] == '/home/learner'
    # A real four-second held endpoint exceeds the unchanged three-second RPC
    # budget. Authenticated busy responses must not renew its heartbeat or stop
    # either live notebook; the next observation recovers after release.
    busy_ids = {suffix: learners(suffix) for suffix in ('source', 'peer')}
    assert all(len(value) == 1 for value in busy_ids.values())
    for suffix, session in sessions.items(): read(session, suffix)
    verify_contention_batch(helper('enrollment-contention-batch', {'course': 'course-main', 'seconds': 4}), expiry=False)
    await_contention_release(lambda: helper('enrollment-contention-status', {'course': 'course-main'}))
    wait(lambda: helper('guardian-status')['course-main']['ready'], 10)
    for suffix, session in sessions.items():
        assert learners(suffix) == busy_ids[suffix]; read(session, suffix)
    phase(evidence.PHASES[2])
    helper('enrollment-stage', {'identity': 'source'})
    assert rpc('hub.authorize', identity='source-next')['ok'] is False
    assert rpc('hub.authorize', identity='source')['ok'] is True
    helper('stop-service'); ctx['service']()
    assert rpc('hub.authorize', identity='source-next')['ok'] is False
    await_hub_authorization(lambda: rpc('hub.authorize', identity='source'))
    # Authority restart may correctly trigger Guardian outage stops. Reopen
    # saved notebooks before measuring worker-local rotation continuity.
    sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
    for suffix, session in sessions.items(): read(session, suffix)
    peer_id = learners('peer')
    phase(evidence.PHASES[3])
    helper('enrollment-activate', {'identity': 'source', 'overlap_seconds': 30})
    assert rpc('hub.authorize', identity='source')['ok'] is True
    assert rpc('hub.authorize', identity='source-next')['ok'] is True
    phase(evidence.PHASES[4])
    helper('enrollment-retire', {'identity': 'source'})
    assert rpc('hub.authorize', identity='source')['ok'] is False
    assert rpc('hub.authorize', identity='source-next')['ok'] is True
    helper('enrollment-install', {'identity': 'source'})
    wait(lambda: requests.get(bases['source'] + '/hub/login', timeout=3).status_code == 200, 60)
    sessions['source'] = notebook('source'); read(sessions['source'], 'source')
    assert rpc('hub.authorize', identity='source-previous')['ok'] is False
    # Hub restart/poll cleanup may replace its own ephemeral notebook. Saved
    # coursework and the unaffected peer's exact runtime must survive.
    assert learners('source') and learners('peer') == peer_id
    read(sessions['peer'], 'peer')
    phase(evidence.PHASES[5])
    # Move the saved home to worker two using the existing reviewed transfer,
    # so its Guardian credential can expire without disturbing the peer worker.
    ctx['stop'](sessions['source'], 'source'); helper('copy-home')
    plan = rpc('ownership.preview', {'receiver_worker_id': 'worker-two'})
    assert plan['ok'] is True
    reference = {key: plan['result'][key] for key in ('plan_id', 'plan_sha256')}
    assert rpc('ownership.apply', reference)['ok'] is True
    receiver = notebook('receiver'); read(receiver, 'receiver')
    helper('enrollment-stage', {'identity': 'guardian-two'})
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two-next'}) == {'authorized': False, 'denied': True}
    helper('enrollment-activate', {'identity': 'guardian-two', 'overlap_seconds': 5})
    await_guardian_authorization(lambda: helper('enrollment-guardian-probe', {'identity': 'guardian-two-next'}))
    # Keep the old certificate installed: its bounded overlap must expire on
    # fresh request/watchdog checks even without an operator retirement call.
    wait(lambda: not learners('receiver'), 20)
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two'}) == {'authorized': False, 'denied': True}
    assert learners('peer') == peer_id; read(sessions['peer'], 'peer')
    phase(evidence.PHASES[6])
    helper('enrollment-install', {'identity': 'guardian-two'})
    wait(lambda: helper('guardian-status')['course-main']['ready'], 30)
    receiver = notebook('receiver'); read(receiver, 'receiver')
    kernel(receiver, bases['receiver'], 'alice', 'python3', "print('CONTINUITY_OK')")
    kernel(receiver, bases['receiver'], 'alice', 'ir', "cat('CONTINUITY_OK\\n')")
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two-previous'}) == {'authorized': False, 'denied': True}
    assert learners('peer') == peer_id
    phase(evidence.PHASES[7])
    helper('enrollment-revoke-worker', {'worker': 'worker-two'})
    wait(lambda: not learners('receiver'), 20)
    assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is False
    assert helper('enrollment-reenroll-refusal', {'worker': 'worker-two'}) == {'refused': True}
    assert learners('peer') == peer_id; read(sessions['peer'], 'peer')
    phase(evidence.PHASES[8])
    helper('enrollment-checkpoint', {})
    # Prolonged exclusion never turns busy into a lease renewal. Prove the
    # actual saved heartbeat expires, service watchdog closes, and the owned
    # peer stops. Service outage may stop every course; restart must retain its
    # coursework and the already-revoked worker must remain denied.
    wait(lambda: helper('guardian-status')['course-peer']['ready'], 10)
    verify_contention_batch(helper('enrollment-contention-batch', {'course': 'course-peer', 'seconds': 13}), expiry=True)
    wait(lambda: helper('service-status')['failure'] == 'runtime_endpoint_enforcement_unavailable', 15)
    wait(lambda: not learners('peer'), 30)
    helper('stop-service'); ctx['service']()
    assert helper('enrollment-checkpoint-proof', {}) == {'unchanged': True}
    assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is False
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two'}) == {'authorized': False, 'denied': True}
    # Preserve bytes/history across authority restart; process continuity is
    # required only for unaffected workers while authority stays available.
    sessions['peer'] = notebook('peer'); read(sessions['peer'], 'peer')
    assert helper('history-proof') == {'transfer_records': 1, 'statuses': ['COMPLETE']}
    phase(evidence.PHASES[9])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--course-image', default='waypoint-authority-course:local')
    parser.add_argument('--hub-image', default='waypoint-authority-hub:local')
    parser.add_argument('--controller-image', default='waypoint-authority-controller:local')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile-cpus', type=int, choices=(4, 5, 6),
        help='Select existing smaller service caps; actual daemon capacity is recorded separately')
    parser.add_argument('--allow-dirty', action='store_true')
    parser.add_argument('--diagnostic-pause', action='store_true')
    args = parser.parse_args()
    if args.diagnostic_pause and not args.allow_dirty: parser.error('diagnostic pause requires --allow-dirty')
    args.output = args.output.absolute()
    if args.output.exists() or args.output.is_symlink(): parser.error('output must be a fresh report path')
    run(args, exercise=exercise, evidence=evidence)


if __name__ == '__main__':
    try: main()
    except Exception: raise SystemExit('worker_enrollment_rehearsal_failed') from None
    except KeyboardInterrupt: raise SystemExit('worker_enrollment_rehearsal_interrupted') from None
