#!/usr/bin/env python3
"""Actual local worker enrollment and certificate rotation, synthetic coursework."""
import argparse
import json
from pathlib import Path

import requests

import worker_enrollment_evidence as evidence
from verify_authenticated_runtime import run
from smoke import kernel


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
    phase(evidence.PHASES[2])
    helper('enrollment-stage', {'identity': 'source'})
    assert rpc('hub.authorize', identity='source-next')['ok'] is False
    assert rpc('hub.authorize', identity='source')['ok'] is True
    helper('stop-service'); ctx['service']()
    assert rpc('hub.authorize', identity='source-next')['ok'] is False
    assert rpc('hub.authorize', identity='source')['ok'] is True
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
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two-next'}) == {'authorized': False}
    helper('enrollment-activate', {'identity': 'guardian-two', 'overlap_seconds': 5})
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two-next'}) == {'authorized': True}
    # Keep the old certificate installed: its bounded overlap must expire on
    # fresh request/watchdog checks even without an operator retirement call.
    wait(lambda: not learners('receiver'), 20)
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two'}) == {'authorized': False}
    assert learners('peer') == peer_id; read(sessions['peer'], 'peer')
    phase(evidence.PHASES[6])
    helper('enrollment-install', {'identity': 'guardian-two'})
    wait(lambda: helper('guardian-status')['course-main']['ready'], 30)
    receiver = notebook('receiver'); read(receiver, 'receiver')
    kernel(receiver, bases['receiver'], 'alice', 'python3', "print('CONTINUITY_OK')")
    kernel(receiver, bases['receiver'], 'alice', 'ir', "cat('CONTINUITY_OK\\n')")
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two-previous'}) == {'authorized': False}
    assert learners('peer') == peer_id
    phase(evidence.PHASES[7])
    helper('enrollment-revoke-worker', {'worker': 'worker-two'})
    wait(lambda: not learners('receiver'), 20)
    assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is False
    assert helper('enrollment-reenroll-refusal', {'worker': 'worker-two'}) == {'refused': True}
    assert learners('peer') == peer_id; read(sessions['peer'], 'peer')
    phase(evidence.PHASES[8])
    helper('enrollment-checkpoint', {})
    helper('stop-service'); ctx['service']()
    assert helper('enrollment-checkpoint-proof', {}) == {'unchanged': True}
    assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is False
    assert helper('enrollment-guardian-probe', {'identity': 'guardian-two'}) == {'authorized': False}
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
