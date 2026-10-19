#!/usr/bin/env python3
"""Actual disposable local Docker/mTLS/Hub/Python/R rehearsal, synthetic learners."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smoke import kernel, change, xsrf
from hub.authority_client import ERRORS
from authenticated_evidence import VERSION, allocation_for_cpu


def command(*args, data=None, timeout=60):
    result = subprocess.run(['docker', *args], input=data, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('docker_fixture_command_failed:' + args[0])
    return result.stdout.strip()


def port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def wait(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = check()
            if result: return result
        except Exception: pass
        time.sleep(0.25)
    raise RuntimeError('fixture_condition_timeout')


def request_notebook_start(session, base, *, previous_runtime_stopped):
    if previous_runtime_stopped:
        # Hub's normal poll must clear its old server metadata first. One spawn
        # GET while it still looks running redirects to the old server; later
        # /user/ readiness requests do not implicitly request a replacement.
        def hub_stopped():
            url = base + '/hub/api/user'
            response = session.get(url, headers={'X-XSRFToken': xsrf(session, url)}, timeout=10)
            response.raise_for_status()
            model = response.json()
            return (isinstance(model, dict) and 'server' in model and model['server'] is None
                    and isinstance(model.get('servers'), dict) and '' not in model['servers'])
        wait(hub_stopped, 45)
    session.get(base + '/hub/spawn/alice', timeout=30, allow_redirects=False).raise_for_status()


def result_matches(reply, field, expected):
    result = reply.get('result')
    return (reply.get('ok') is True and isinstance(result, dict) and field in result
            and type(result[field]) is type(expected) and result[field] == expected)


def configured_cpu_limit(limits):
    """Accept exactly one Docker CPU-limit representation; refuse unbounded caps."""
    nano, quota, period = (limits.get(key, 0) for key in ('NanoCpus', 'CpuQuota', 'CpuPeriod'))
    if any(type(value) is not int for value in (nano, quota, period)):
        raise ValueError('fixture_cpu_limit_invalid')
    if nano > 0 and quota == 0 and period == 0: return nano / 10**9
    if nano == 0 and quota > 0 and period > 0: return quota / period
    raise ValueError('fixture_cpu_limit_invalid')


def run(args, *, exercise=None, evidence=None):
    if evidence is None:
        import authenticated_evidence as evidence
    name = 'wa' + uuid.uuid4().hex[:10]
    root = Path(tempfile.mkdtemp(prefix='waypoint-authority-')).resolve()
    for child in ('control', 'backend'):
        (root / child).mkdir(mode=0o700)
    network, controller = name + '-control', name + '-authority'
    phases = []
    cleanup_authorized = False
    report = {'version': evidence.VERSION, 'status': 'FAIL',
        'scope': evidence.SCOPE, 'phases': phases}
    def phase(value):
        phases.append(value); print(value + ': PASS', flush=True)
    def helper(action, payload=None):
        result = json.loads(command('exec', '-i', controller, 'python', 'authenticated_fixture.py', action, str(root),
            data=json.dumps(payload) if payload is not None else None))
        if action == 'launch':
            for worker in ('worker-one', 'worker-two'):
                guardian = json.loads(command('inspect', name + '-' + worker + '-guardian'))[0]
                limits = guardian['HostConfig']
                assert guardian['Image'] == controller_image and limits['Memory'] == 128 * 1024**2
                assert configured_cpu_limit(limits) == allocation['guardian_cpu']
        return result
    def rpc(op, payload=None, identity='operator', worker='worker-one', course='course-main'):
        reply = helper('rpc', dict(identity=identity, worker_id=worker, course_id=course,
            operation=op, payload=payload or {}))
        if reply.get('ok') is False:
            code = reply.get('code')
            print('RPC refusal: ' + op + ': ' + (code if isinstance(code, str) and code in ERRORS else 'fixture_rpc_refused'), flush=True)
        return reply
    def service(mode='serve'):
        command('exec', '-d', controller, 'python', 'authenticated_fixture.py', mode, str(root))
        try:
            wait(lambda: rpc('ownership.inventory')['ok'])
        except RuntimeError:
            status = helper('service-status')
            error = rpc('ownership.inventory').get('code', 'unknown')
            print('service diagnostic: ' + (status['failure'] or error), flush=True)
            raise
    def learners(suffix):
        return command('ps', '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix,
            '--filter', 'label=educloud.workspace.kind=learner').split()
    def notebook(suffix):
        # Ownership authorization does not imply Guardian readiness. Wait only
        # for the course about to spawn; a pending sibling must remain denied.
        course = 'course-peer' if suffix == 'peer' else 'course-main'
        wait(lambda: helper('guardian-status')[course]['ready'], 30)
        base = bases[suffix]
        session = requests.Session()
        session.max_redirects = 8
        session.get(base + '/hub/login', timeout=10).raise_for_status()
        session.post(base + '/hub/login', data={'username': 'alice', 'password': password,
            '_xsrf': xsrf(session, base + '/hub/login')}, timeout=30, allow_redirects=False).raise_for_status()
        request_notebook_start(session, base, previous_runtime_stopped=not learners(suffix))
        print(suffix + ': waiting for authenticated notebook', flush=True)
        def ready():
            session.get(base + '/user/alice/lab', timeout=10)
            response = session.get(base + '/user/alice/api/contents', timeout=10)
            return response.status_code == 200 and 'application/json' in response.headers.get('Content-Type', '')
        wait(ready, 180)
        return session
    def read(session, suffix):
        response = session.get(bases[suffix] + '/user/alice/api/contents/coursework.txt', timeout=10)
        response.raise_for_status(); assert response.json()['content'] == 'synthetic coursework retained\n'
    def stop(session, suffix):
        change(session, 'DELETE', bases[suffix] + '/hub/api/users/alice/server').raise_for_status()
        wait(lambda: not learners(suffix))
    try:
        course_image = command('image', 'inspect', args.course_image, '--format', '{{.Id}}')
        hub_image = command('image', 'inspect', args.hub_image, '--format', '{{.Id}}')
        controller_image = command('image', 'inspect', args.controller_image, '--format', '{{.Id}}')
        report['images'] = dict(course=course_image, hub=hub_image, controller=controller_image)
        report['source'] = subprocess.check_output(['git', '-C', str(Path(__file__).resolve().parents[2]), 'rev-parse', 'HEAD'], text=True).strip()
        report['source_dirty'] = bool(subprocess.check_output(['git', '-C', str(Path(__file__).resolve().parents[2]), 'status', '--porcelain'], text=True).strip())
        if report['source_dirty'] and not args.allow_dirty:
            raise RuntimeError('clean_source_required')
        workspace = Path(__file__).resolve().parents[1]
        expected = {}
        hub_files = {path: '/srv/jupyterhub/' + path.name for path in (workspace / 'hub').glob('*.py')}
        controller_files = {workspace / file: '/srv/authority/' + file for file in (
            'ownership_contract.py', 'authority_access.py', 'authority_operator.py', 'runtime_authority.py',
            'runtime_guardian.py', 'controller_service.py')}
        controller_files.update({workspace / 'hub/authority_client.py': '/srv/authority/hub/authority_client.py',
            workspace / 'tests/authority_test_pki.py': '/srv/authority/authority_test_pki.py',
            workspace / 'rehearsal/authenticated_fixture.py': '/srv/authority/authenticated_fixture.py',
            workspace / 'rehearsal/authenticated_evidence.py': '/srv/authority/authenticated_evidence.py'})
        if exercise is not None:
            for path in ('worker_enrollment.py', 'worker_credentials.py'):
                controller_files[workspace / path] = '/srv/authority/' + path
            controller_files[workspace / 'rehearsal/enrollment_fixture.py'] = '/srv/authority/enrollment_fixture.py'
        for image, files in [(hub_image, hub_files), (controller_image, controller_files)]:
            current = {target: hashlib.sha256(source.read_bytes()).hexdigest() for source, target in files.items()}
            probe = 'import sys,json,hashlib; from pathlib import Path; paths=json.loads(sys.argv[1]); print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}))'
            observed = json.loads(command('run', '--rm', '--network', 'none', '--entrypoint', 'python', image,
                '-c', probe, json.dumps(list(current))))
            assert observed == current, 'built_image_source_mismatch'
            expected.update({str(source.relative_to(workspace)): current[target] for source, target in files.items()})
        report['code_sha256'] = expected
        for path in evidence.CODE_FILES - set(expected):
            expected[path] = hashlib.sha256((workspace / path).read_bytes()).hexdigest()
        assert set(expected) == evidence.CODE_FILES
        report['recipe_sha256'] = {path: hashlib.sha256((workspace / path).read_bytes()).hexdigest()
            for path in ('.dockerignore', 'hub/Dockerfile', 'controller.Dockerfile', 'course/Dockerfile')}
        ports = {suffix: port() for suffix in ('source', 'receiver', 'peer')}
        assert len(set(ports.values())) == 3
        bases = {suffix: 'http://127.0.0.1:' + str(value) for suffix, value in ports.items()}
        password = secrets.token_urlsafe(32)
        expected_names = {controller, *(name + '-' + suffix + '-hub' for suffix in ('source', 'peer', 'receiver')),
            *(name + '-' + worker + '-guardian' for worker in ('worker-one', 'worker-two'))}
        existing = set(command('container', 'ls', '-a', '--format', '{{.Names}}').split())
        if expected_names & existing: raise RuntimeError('fixture_name_collision')
        for kind, names in [('network', {network, name + '-edge'}), ('volume',
            {name + '-control', name + '-backend', *(name + '-' + suffix + tail for suffix in ('source', 'peer', 'receiver')
                for tail in ('-hubdata', '-home-' + hashlib.sha256(b'alice').hexdigest()[:24]))})]:
            if names & set(command(kind, 'ls', '--format', '{{.Name}}').split()):
                raise RuntimeError('fixture_name_collision')
        for suffix in ('source', 'peer', 'receiver'):
            for kind in ('container', 'network', 'volume'):
                if command(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter',
                    'label=educloud.workspace.instance=' + name + '-' + suffix):
                    raise RuntimeError('fixture_name_collision')
        allocation = allocation_for_cpu(int(command('info', '--format', '{{.NCPU}}')),
            profile_cpus=getattr(args, 'profile_cpus', None))
        cleanup_authorized = True
        command('network', 'create', '--internal', '--label', 'educloud.fixture=' + name, network)
        command('network', 'create', '--label', 'educloud.fixture=' + name, name + '-edge')
        for suffix in ('control', 'backend'):
            command('volume', 'create', '--label', 'educloud.fixture=' + name, name + '-' + suffix)
        command('run', '-d', '--name', controller, '--network', network, '--network-alias', 'authority.local.test',
            '--label', 'educloud.fixture=' + name, '--mount', 'type=bind,src=' + str(root) + ',dst=' + str(root),
            '--mount', 'type=volume,src=' + name + '-control,dst=' + str(root / 'control') + ',volume-nocopy',
            '--mount', 'type=volume,src=' + name + '-backend,dst=' + str(root / 'backend') + ',volume-nocopy',
            '--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock', '--memory', '256m',
            '--cpus', str(allocation['controller_cpu']), controller_image, 'python', '-c', 'import time; time.sleep(86400)')
        observed = helper('bootstrap', dict(name=name, network=network, edge_network=name + '-edge', ports=ports, password=password,
            course_image=course_image, hub_image=hub_image, controller_image=controller_image,
            profile_cpus=getattr(args, 'profile_cpus', None)))
        # Bootstrap binds cleanup to the original private directory before any
        # quota-proof refusal; the service and learner runtimes have not started.
        limits = json.loads(command('inspect', controller))[0]['HostConfig']
        assert configured_cpu_limit(limits) == allocation['controller_cpu'] and limits['Memory'] == 256 * 1024**2
        assert observed['allocation'] == allocation
        report['host'] = {key: observed[key] for key in ('memory_bytes', 'cpu')}
        report['profile_cpus'] = getattr(args, 'profile_cpus', None)
        report['allocation'] = observed['allocation']
        if exercise is not None:
            exercise(dict(helper=helper, service=service, rpc=rpc, learners=learners,
                notebook=notebook, read=read, stop=stop, bases=bases, name=name,
                phase=phase, password=password, wait=wait, command=command, allocation=report['allocation']))
        else:
            service(); helper('launch')
            for base in bases.values(): wait(lambda: requests.get(base + '/hub/login', timeout=3).status_code == 200, 60)
            try:
                wait(lambda: all(item['ready'] for item in helper('guardian-status').values()), 30)
            except RuntimeError:
                print('guardian diagnostic: ' + json.dumps(helper('guardian-status'), sort_keys=True), flush=True)
                raise
            wait(lambda: rpc('hub.authorize', identity='source')['ok'])
            assert not rpc('ownership.inventory', identity='source')['ok']
            assert not rpc('hub.authorize', identity='peer', course='course-main')['ok']
            assert not rpc('hub.authorize', identity='receiver', worker='worker-two')['ok']
            phase('mtls_role_course_and_nonowner_refusal')
            sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
            for suffix, session in sessions.items():
                kernel(session, bases[suffix], 'alice', 'python3', "print('CONTINUITY_OK')")
                kernel(session, bases[suffix], 'alice', 'ir', "cat('CONTINUITY_OK\\n')")
                response = change(session, 'PUT', bases[suffix] + '/user/alice/api/contents/coursework.txt',
                    json={'type': 'file', 'format': 'text', 'content': 'synthetic coursework retained\n'})
                response.raise_for_status(); read(session, suffix)
                hub = json.loads(command('inspect', name + '-' + suffix + '-hub'))[0]
                assert all(mount['Destination'] != '/var/run/docker.sock' for mount in hub['Mounts'])
                cid = learners(suffix)[0]; learner = json.loads(command('inspect', cid))[0]
                host = learner['HostConfig']; mounts = learner['Mounts']
                assert host['ReadonlyRootfs'] and not host['Privileged'] and host['PidsLimit'] == 128
                assert host['Memory'] == host['MemorySwap'] == 512 * 1024**2
                assert host['CpuQuota'] / host['CpuPeriod'] == report['allocation']['learner_cpu']
                assert host['RestartPolicy']['Name'] == 'no' and learner['Config']['User'] == '1000:1000'
                assert len(mounts) == 1 and mounts[0]['Destination'] == '/home/learner'
                assert len(learner['NetworkSettings']['Networks']) == 1
                env = learner['Config']['Env']; assert not any('AUTHORITY' in item for item in env)
                phase(suffix + '_real_python_r_saved_home_and_socketless_hub')
            helper('stop-service')
            # Bound the observation generously: RPC failure and Docker stops need
            # time to complete even though course monitoring runs independently.
            wait(lambda: not learners('source') and not learners('peer'), 20)
            phase('authority_outage_stops_actual_notebooks')
            service()
            sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
            for suffix, session in sessions.items(): read(session, suffix)
            stop(sessions['source'], 'source'); helper('copy-home')
            plan = rpc('ownership.preview', {'receiver_worker_id': 'worker-two'})
            assert plan['ok']; reference = {key: plan['result'][key] for key in ('plan_id', 'plan_sha256')}
            helper('stop-service'); service('serve-prepared-fault')
            sessions['peer'] = notebook('peer')
            assert not rpc('ownership.apply', reference)['ok']
            assert rpc('ownership.inventory')['result']['pending'] is True
            assert not rpc('hub.authorize', identity='source')['ok']
            assert not rpc('hub.authorize', identity='receiver', worker='worker-two')['ok']
            phase('interrupted_transfer_durable_pending_denies_both_endpoints')
            helper('drift-snapshot'); helper('stop-service'); service()
            sessions['peer'] = notebook('peer')
            recovery = rpc('ownership.reconcile_preview', {'selected_worker_id': 'worker-two'})
            assert recovery['ok']; reference = {key: recovery['result'][key] for key in ('plan_id', 'plan_sha256')}
            peer_id = learners('peer')
            assert rpc('ownership.reconcile_apply', reference)['ok']
            assert rpc('ownership.inventory')['result']['pending'] is False
            assert helper('history-proof') == {'transfer_records': 2, 'statuses': ['COMPLETE', 'PREPARED']}
            assert learners('peer') == peer_id
            assert not rpc('hub.authorize', identity='source')['ok']
            assert rpc('hub.authorize', identity='receiver', worker='worker-two')['result']['epoch'] == 2
            wait(lambda: all(item['ready'] for item in helper('guardian-status').values()), 30)
            phase('authenticated_reconciliation_retains_history_and_peer_runtime')
            command('restart', name + '-source-hub')
            wait(lambda: requests.get(bases['source'] + '/hub/login', timeout=3).status_code == 200)
            # Real restarted source Hub rejects fresh authentication and cannot spawn.
            source = requests.Session(); page = source.get(bases['source'] + '/hub/login', timeout=5)
            rejected = source.post(bases['source'] + '/hub/login', data={'username': 'alice', 'password': password,
                '_xsrf': xsrf(source, bases['source'] + '/hub/login')}, timeout=20)
            assert rejected.status_code == 403
            denied = source.get(bases['source'] + '/hub/spawn/alice', timeout=20)
            assert '/hub/login' in denied.url
            assert not learners('source')
            phase('restarted_source_hub_cannot_revive_fenced_epoch')
            receiver = notebook('receiver'); read(receiver, 'receiver')
            kernel(receiver, bases['receiver'], 'alice', 'python3', "print('CONTINUITY_OK')")
            kernel(receiver, bases['receiver'], 'alice', 'ir', "cat('CONTINUITY_OK\\n')")
            read(sessions['peer'], 'peer')
            phase('receiver_real_python_r_and_coursework_continuity')
            command('kill', name + '-worker-two-guardian')
            wait(lambda: not learners('receiver'), 12)
            assert learners('peer') == peer_id
            phase('guardian_loss_authority_watchdog_stops_receiver_preserves_peer')
            command('start', name + '-worker-two-guardian')
            wait(lambda: rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'])
            wait(lambda: all(item['ready'] for item in helper('guardian-status').values()), 30)
            receiver = notebook('receiver'); read(receiver, 'receiver')
            helper('revoke', {'identity': 'guardian-two'})
            wait(lambda: not learners('receiver'), 12)
            assert learners('peer') == peer_id
            phase('guardian_certificate_revocation_stops_only_its_worker')
            helper('restore-acl')
            wait(lambda: all(item['ready'] for item in helper('guardian-status').values()), 30)
            receiver = notebook('receiver'); read(receiver, 'receiver')
            helper('revoke', {'identity': 'receiver'})
            assert not rpc('hub.authorize', identity='receiver', worker='worker-two')['ok']
            helper('restore-acl')
            phase('hub_certificate_revocation_refuses_fresh_access')
            helper('stop-service'); service()
            wait(lambda: result_matches(rpc('hub.authorize', identity='receiver', worker='worker-two'), 'epoch', 2))
            wait(lambda: result_matches(rpc('ownership.inventory'), 'pending', False))
            source_denial = rpc('hub.authorize', identity='source')
            assert source_denial.get('ok') is False and source_denial.get('code') == 'authority_operation_refused'
            phase('service_restart_retains_authority_incarnation_epoch_and_barriers')
        assert all(hashlib.sha256((workspace / path).read_bytes()).hexdigest() == value for path, value in expected.items())
        assert all(hashlib.sha256((workspace / path).read_bytes()).hexdigest() == value for path, value in report['recipe_sha256'].items())
        assert report['source'] == subprocess.check_output(['git', '-C', str(workspace.parent), 'rev-parse', 'HEAD'], text=True).strip()
        if not args.allow_dirty:
            assert not subprocess.check_output(['git', '-C', str(workspace.parent), 'status', '--porcelain'], text=True).strip()
        report['status'] = 'PASS'
    except BaseException as exc:
        # Public evidence intentionally contains no logs, URLs with credentials, or request bodies.
        report['failure'] = type(exc).__name__ if type(exc).__name__ in evidence.FAILURES else 'RuntimeError'
        trace = exc.__traceback__; lines = []; enrollment_lines = []
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == __file__: lines.append(trace.tb_lineno)
            if exercise is not None and trace.tb_frame.f_code.co_filename == exercise.__code__.co_filename:
                enrollment_lines.append(trace.tb_lineno)
            trace = trace.tb_next
        print('fixture failure lines: ' + json.dumps(lines[-8:]), flush=True)
        if enrollment_lines:
            print('enrollment failure lines: ' + json.dumps(enrollment_lines[-8:]), flush=True)
        if (isinstance(exc, KeyError) and exc.args and isinstance(exc.args[0], str)
                and exc.args[0] in {'result', 'epoch', 'pending', 'ok'}):
            print('fixture failure key: ' + exc.args[0], flush=True)
        raise
    finally:
        report['cleanup_verified'] = True
        if cleanup_authorized:
            cleanup_failed = False
            if report['status'] != 'PASS':
                try: print('failure diagnostic: ' + json.dumps(helper('failure-status'), sort_keys=True), flush=True)
                except Exception: pass
                if exercise is not None:
                    try: print('enrollment diagnostic: ' + json.dumps(helper('enrollment-failure-status', {}), sort_keys=True), flush=True)
                    except Exception: pass
                if args.diagnostic_pause:
                    print('diagnostic window active', flush=True)
                    time.sleep(45)
            owned_containers = [controller, *(name + '-' + suffix + '-hub' for suffix in ('source', 'peer', 'receiver')),
                *(name + '-' + worker + '-guardian' for worker in ('worker-one', 'worker-two'))]
            for suffix in ('source', 'peer', 'receiver'):
                try:
                    owned_containers += command('ps', '-aq', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix).split()
                except Exception: pass
            for resource in owned_containers:
                if resource == controller: continue
                try: command('rm', '-f', resource)
                except Exception: pass
            # Quiesce writers before handing private host files back to their
            # original creator. Keep this privileged controller until last.
            try: helper('stop-service')
            except Exception: pass  # It may have already exited after failure.
            try: helper('cleanup-permissions')
            except Exception: cleanup_failed = True
            try: command('rm', '-f', controller)
            except Exception: cleanup_failed = True
            for kind in ('network', 'volume'):
                try:
                    resources = command(kind, 'ls', '-q', '--filter', 'label=educloud.fixture=' + name).split()
                    for suffix in ('source', 'peer', 'receiver'):
                        resources += command(kind, 'ls', '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix).split()
                    for resource in set(resources):
                        try: command(kind, 'rm', resource)
                        except Exception: pass
                except Exception: pass
            # Verify invocation resources are gone, including home/hubdata volumes.
            remaining = []
            for owned_name in [controller, *(name + '-' + worker + '-guardian' for worker in ('worker-one', 'worker-two'))]:
                try: remaining += command('container', 'ls', '-aq', '--filter', 'name=^/' + owned_name + '$').split()
                except Exception: remaining.append('cleanup_check_failed')
            for kind in ('container', 'network', 'volume'):
                for label in ['educloud.fixture=' + name, *('educloud.workspace.instance=' + name + '-' + suffix for suffix in ('source', 'peer', 'receiver'))]:
                    try: remaining += command(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter', 'label=' + label).split()
                    except Exception: remaining.append('cleanup_check_failed')
            report['cleanup_verified'] = not remaining and not cleanup_failed
        try: shutil.rmtree(root)
        except Exception: report['cleanup_verified'] = False
        if not report['cleanup_verified']: report['status'] = 'FAIL'
        report['acceptance_eligible'] = report['status'] == 'PASS' and report.get('source_dirty') is False
        evidence.validate_report(report, require_clean=not args.allow_dirty if report['status'] == 'PASS' else False)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(report, indent=2, sort_keys=True) + '\n'); stream.flush(); os.fsync(stream.fileno())
        directory = os.open(args.output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    if report['status'] != 'PASS': raise RuntimeError('fixture_cleanup_failed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--course-image', default='waypoint-authority-course:local')
    parser.add_argument('--hub-image', default='waypoint-authority-hub:local')
    parser.add_argument('--controller-image', default='waypoint-authority-controller:local')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profile-cpus', type=int, choices=(4, 5, 6),
        help='Select existing smaller service caps; actual daemon capacity is recorded separately')
    parser.add_argument('--allow-dirty', action='store_true', help='Development diagnostics only; recorded as dirty in evidence')
    parser.add_argument('--diagnostic-pause', action='store_true', help='Hold a failed development fixture for 45 seconds before cleanup')
    args = parser.parse_args()
    if args.diagnostic_pause and not args.allow_dirty: parser.error('diagnostic pause requires --allow-dirty')
    args.output = args.output.absolute()
    if args.output.exists() or args.output.is_symlink():
        parser.error('output must be a fresh report path')
    run(args)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('authenticated_runtime_rehearsal_failed') from None
    except KeyboardInterrupt:
        raise SystemExit('authenticated_runtime_rehearsal_interrupted') from None
