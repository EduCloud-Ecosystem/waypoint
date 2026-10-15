#!/usr/bin/env python3
"""Expand and contract a stopped synthetic course while another course stays live."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import selectors
import sys
import subprocess
import tempfile
import time
from unittest.mock import patch
import uuid

from playwright.sync_api import sync_playwright
from notebook import Notebook, SUBJECTS, ROOT
from backup import docker
from deployment import preflight, compose, read_environment
from evidence import _source, sha256_file
from homes import inspect, run as xfs_run
from recovery import load_config, backup_repository, restic, restore_repository
from smoke import change
from verify import port
from verify_multiple_courses import (AUTHORITY, ALLOCATION_FIELDS, bounded_allocation, cleanup_courses,
    active_learners, login_with_current_quotas, login_and_ready_with_current_quotas,
    save_coursework, assert_live, wait_hub)
import maintenance
import retirement
import upgrade

CHECKS = frozenset({
    'two-managed-courses-enrolled', 'fresh-quota-authenticated-saved-work',
    'running-target-preview-refused', 'target-overcommit-refused', 'peak-overcommit-refused',
    'stale-environment-approval-refused', 'changed-worker-policy-refused',
    'stale-ledger-approval-refused', 'tampered-plan-refused', 'peer-preserved-after-refusals',
    'expanded-reservation-committed', 'expanded-apply-did-not-start',
    'normal-start-consumed-expanded-policy', 'expanded-two-learner-admission',
    'expanded-python-r-validated', 'expanded-docker-limits-enforced',
    'expanded-capacity-overflow-429', 'peer-unchanged-after-expansion',
    'interrupted-contraction-retained-peak', 'pending-preflight-and-start-refused',
    'pending-backup-refused-before-provider', 'pending-maintenance-and-upgrade-refused',
    'pending-retirement-refused-before-provider', 'pending-allocation-mutations-refused',
    'wrong-pending-plan-refused', 'peer-admission-allowed-during-target-pending',
    'exact-contraction-resumed', 'contracted-reservation-committed',
    'contracted-apply-did-not-start', 'all-homes-and-work-retained',
    'normal-start-consumed-contracted-policy', 'contracted-capacity-overflow-429',
    'saved-work-after-contracted-restart', 'peer-unchanged-through-contraction',
    'old-approval-replay-refused',
    'missing-receipt-approval-replay-refused',
    'stopped-backup-preserved-resource-history', 'encrypted-history-bundle-verified',
    'managed-restore-kept-history-only', 'managed-restored-start-refused',
    'restored-identity-reuse-refused', 'restored-saved-work-preserved',
    'peer-unchanged-through-closed-recovery',
})
HASH = re.compile(r'[0-9a-f]{64}\Z')
IMAGE = re.compile(r'(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64})\Z')


def expect_refusal(operation, error=ValueError, codes=None):
    try:
        operation()
    except error as exc:
        if codes is not None and getattr(exc,'code',None) not in codes:
            raise AssertionError('operation refused for an unexpected reason') from None
        return
    raise AssertionError('unsafe or stale operation was accepted')


def publish_pass(root, report, cleanup_verified):
    keys = {'waypoint_revision','fixture_sha256','amendment_sha256','allocator_sha256',
            'source_manifests_sha256','policy_sha256','expansion_plan_sha256','contraction_plan_sha256',
            'snapshot_id','receipt_history_sha256',
            'images','initial_allocation','expanded_allocation','pending_allocation','final_allocation','checks'}
    if cleanup_verified is not True or not isinstance(report,dict) or set(report) != keys:
        raise ValueError('resource amendment PASS requires bounded evidence and verified cleanup')
    if (not isinstance(report['waypoint_revision'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',report['waypoint_revision'])
            or any(not isinstance(report[key],str) or not HASH.fullmatch(report[key]) for key in
                   ('fixture_sha256','amendment_sha256','allocator_sha256','policy_sha256','expansion_plan_sha256','contraction_plan_sha256',
                    'snapshot_id','receipt_history_sha256'))
            or not isinstance(report['source_manifests_sha256'],list) or len(report['source_manifests_sha256']) != 2
            or any(not isinstance(value,str) or not HASH.fullmatch(value) for value in report['source_manifests_sha256'])
            or len(set(report['source_manifests_sha256'])) != 2
            or not isinstance(report['images'],dict) or set(report['images']) != {'hub','course'}
            or any(not isinstance(value,str) or not IMAGE.fullmatch(value) for value in report['images'].values())
            or not isinstance(report['checks'],list) or any(not isinstance(value,str) for value in report['checks'])
            or set(report['checks']) != CHECKS or len(report['checks']) != len(CHECKS)):
        raise ValueError('resource amendment evidence schema or completed controls invalid')
    for key, memory, cpu in (('initial_allocation',3072,2.5),('expanded_allocation',4096,3),
                            ('pending_allocation',4096,3),('final_allocation',3072,2.5)):
        value = report[key]
        if (not isinstance(value,dict) or set(value) != ALLOCATION_FIELDS
                or any(type(number) not in (int,float) or not 0 <= number <= 1048576 or not math.isfinite(number)
                       for number in value.values())
                or any(type(value[field]) is not int for field in
                       ('active_courses','released_courses','memory_reserved_mb','memory_budget_mb','host_reserve_mb'))
                or value['active_courses'] != 2 or value['released_courses'] != 0
                or value['memory_reserved_mb'] != memory or value['cpu_reserved'] != cpu
                or value['memory_budget_mb'] != 8192 or value['cpu_budget'] != 3
                or value['host_reserve_mb'] != 4096 or value['host_reserve_cpu'] != 1):
            raise ValueError('resource amendment lease transition evidence invalid')
    data = (json.dumps(dict(report,version=1,status='PASS',scope='same-worker-synthetic-resource-amendment',
                           operator_started_services=True,apply_started_services=False,closed_recovery_only=True,cleanup_verified=True),
                       sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    if len(data) > 64*1024:
        raise ValueError('resource amendment evidence budget exceeded')
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('resource amendment evidence directory must be private')
    fd, temporary = tempfile.mkstemp(prefix='.resource-amendment-evidence-',dir=root)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o600); output.write(data); output.flush(); os.fsync(output.fileno())
        os.link(temporary,root/'RESOURCE-AMENDMENT-PASS.json')
    finally:
        os.unlink(temporary)


def _stopped_diagnostic_command(args, deadline, *, popen=subprocess.Popen, monotonic=time.monotonic):
    """Read only a bounded Docker CLI reply; never publish stderr or raw output."""
    if monotonic()>=deadline: raise ValueError('stopped_diagnostic_unavailable')
    process = popen(['docker', *args], stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT if args[0]=='logs' else subprocess.DEVNULL)
    data = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            for _ in range(128):
                remaining = deadline - monotonic()
                if remaining <= 0: raise ValueError('stopped_diagnostic_unavailable')
                if not selector.select(remaining): raise ValueError('stopped_diagnostic_unavailable')
                chunk = os.read(process.stdout.fileno(), min(4096, 65537-len(data)))
                if not chunk: break
                data.extend(chunk)
                if len(data) > 65536: raise ValueError('stopped_diagnostic_unavailable')
            else: raise ValueError('stopped_diagnostic_unavailable')
        remaining = deadline - monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise ValueError('stopped_diagnostic_unavailable')
        return data.decode('utf-8', errors='replace')
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=1)
        process.stdout.close()


def stopped_state_diagnostic(fixture, *, invoke=None, monotonic=time.monotonic):
    """Failure-only, non-atomic observations; never substitute for stopped proof."""
    report = {'scope':'synthetic-owned-stopped-assertion', 'available':False}
    instance = fixture.instance
    if (not isinstance(instance,str) or not re.fullmatch(r'rehearsal-[0-9a-f]{10}|recovered-[0-9a-f]{12}',instance)
            or any(not isinstance(value,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',value)
                   for value in (fixture.image,fixture.hub_image))):
        return report
    deadline = monotonic()+6
    if invoke is None: invoke = lambda args: _stopped_diagnostic_command(args,deadline)
    # DockerSpawner may encode the UUID's hyphens as -2d in its pinned
    # username-based name. Both forms still bind one known synthetic subject.
    learner_names = {instance+'-learner-'+name for subject in SUBJECTS.values()
                     for name in (subject,subject.replace('-','-2d'))}
    roles = {role:{'count':0,'running_count':0,'states':[]} for role in ('hub','learner')}
    report.update(roles=roles,unverified_count=0,truncated=False,
                  hub_shutdown={'available':False,'cleanup_requested':0,'cleanup_completed':0,
                                'stop_user_failed':0,'cleanup_disabled':0})
    try:
        identities = invoke(('ps','-aq','--no-trunc','--filter','label=educloud.workspace.instance='+instance)).split()
        if (len(identities)>64 or len(set(identities))!=len(identities)
                or any(not re.fullmatch(r'[0-9a-f]{64}',identity) for identity in identities)):
            return report
        report['truncated'] = len(identities)>8
        for identity in identities[:8]:
            try:
                records = json.loads(invoke(('inspect',identity)))
                if not isinstance(records,list) or len(records)!=1: raise ValueError('unverified')
                value = records[0]; labels = value['Config']['Labels']; role = labels.get('educloud.workspace.kind')
                actual = value['Name'].removeprefix('/')
                if (value['Id']!=identity or labels.get('educloud.workspace.instance')!=instance
                        or role not in roles or value['Image']!=(fixture.hub_image if role=='hub' else fixture.image)
                        or (role=='hub' and actual!=instance+'-hub')
                        or (role=='learner' and actual not in learner_names)):
                    raise ValueError('unverified')
                state = value['State']
                if (type(state['Running']) is not bool or type(state['OOMKilled']) is not bool
                        or type(state['ExitCode']) is not int or not 0<=state['ExitCode']<=255):
                    raise ValueError('unverified')
                roles[role]['count']+=1; roles[role]['running_count']+=int(state['Running'])
                roles[role]['states'].append({'running':state['Running'],'oom_killed':state['OOMKilled'],'exit_code':state['ExitCode']})
                if role=='hub':
                    try:
                        raw = invoke(('logs','--tail','200',identity))
                        categories = {'cleanup_requested':'Cleaning up single-user servers...',
                            'cleanup_completed':'...done','stop_user_failed':'Failed to stop user:',
                            'cleanup_disabled':'Leaving single-user servers running'}
                        report['hub_shutdown'] = dict(available=True, **{key:min(raw.count(text),200) for key,text in categories.items()})
                    except Exception: pass
            except Exception: report['unverified_count']+=1
        report['available']=True
    except Exception: pass
    return report


def assert_stopped(fixture):
    running = docker('ps','-q','--filter','label=educloud.workspace.instance='+fixture.instance)
    if running:
        try:
            print('STOPPED_ASSERTION_CONTEXT '+json.dumps(stopped_state_diagnostic(fixture),sort_keys=True),flush=True)
        except Exception: pass
    assert not running, 'amendment started owned services'


def effective_cpu(host):
    nano = host.get('NanoCpus',0)
    if type(nano) is int and nano > 0: return nano/10**9
    quota, period = host.get('CpuQuota',0), host.get('CpuPeriod',0)
    if type(quota) is int and type(period) is int and quota > 0 and period > 0: return quota/period
    raise AssertionError('learner CPU enforcement unavailable')


def verify_runtime_policy(fixture, active_limit, memory_mb, cpu):
    hub = json.loads(docker('inspect',fixture.instance+'-hub'))[0]
    environment = dict(value.split('=',1) for value in hub['Config']['Env'] if value.startswith('WORKSPACE_'))
    assert int(environment['WORKSPACE_ACTIVE_LIMIT']) == active_limit
    assert int(environment['WORKSPACE_MEMORY_MB']) == memory_mb and float(environment['WORKSPACE_CPU_LIMIT']) == cpu
    ids = active_learners(fixture)
    assert len(ids) == active_limit
    for identity in ids:
        learner = json.loads(docker('inspect',identity))[0]
        assert learner['Image'] == fixture.image
        assert learner['HostConfig']['Memory'] == memory_mb*1024**2
        assert effective_cpu(learner['HostConfig']) == cpu


def same_peer(fixture, session, url, expected, learners, hub_id):
    assert json.loads(docker('inspect',fixture.instance+'-hub'))[0]['Id'] == hub_id
    assert_live(fixture,session,url,expected['course.py'],learners)


def preserved_work(fixture, expected):
    for user, files in expected.items():
        import hashlib
        home = fixture.homes/(fixture.instance+'-home-'+hashlib.sha256(SUBJECTS[user].encode()).hexdigest()[:24])
        assert home.is_dir()
        for name, content in files.items():
            assert (home/'work'/name).read_text() == content, 'amendment changed retained coursework'


def start_normally(fixture):
    fixture.env = read_environment(fixture.env_file)
    inspect(fixture.homes,fixture.instance)
    # Changed environment must be consumed by the ordinary operator start path.
    fixture.compose('up','-d','--no-build','--pull','never','hub')
    wait_hub(fixture)


def verify_history_bundle(cfg, snapshot, root, expected_sha):
    with tempfile.TemporaryDirectory(prefix='.resource-history-proof-',dir=root) as scratch:
        restic(cfg,'restore',snapshot,'--target',scratch,'--verify')
        bundle = Path(scratch)/'bundle'
        assert sha256_file(bundle/'resource-amendment-receipt.json') == expected_sha
        assert sha256_file(bundle/'runtime/resource_amendment.py') == sha256_file(ROOT/'resource_amendment.py')
        metadata = json.loads((bundle/'bundle.json').read_text())
        assert metadata['version'] == 4 and metadata['retirement_protected'] is False


def verify_missing_receipt_replay(amend, fixture, old_plan, protected_paths):
    """Missing history must not make an old approval usable; restore owned history."""
    receipt = fixture.deployment/amend.RECEIPT
    original = receipt.read_bytes()
    preserved = {path:path.read_bytes() for path in protected_paths}
    receipt.unlink()
    try:
        expect_refusal(lambda:amend.apply(fixture.deployment,old_plan),amend.AmendmentError,
                       {'completed_resource_history_required'})
        assert all(path.read_bytes() == raw for path,raw in preserved.items())
        assert not (fixture.deployment/amend.PENDING).exists()
        assert_stopped(fixture)
    finally:
        fd = os.open(receipt,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as output:
            output.write(original); output.flush(); os.fsync(output.fileno())
        directory = os.open(receipt.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    assert receipt.read_bytes() == original and receipt.stat().st_mode & 0o777 == 0o600


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if not __debug__: parser.error('optimized Python cannot enforce resource amendment acceptance assertions')
    parser.add_argument('--output',type=Path,required=True); args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('use root on a fresh disposable native Linux worker')
    if AUTHORITY.exists() or AUTHORITY.is_symlink():
        parser.error('resource amendment rehearsal requires fresh disposable worker authority')
    import allocation
    import resource_amendment as amend
    source_state = _source(ROOT.parent)
    assert source_state['dirty'] is False, 'resource amendment acceptance needs clean source'
    fixture_hash = sha256_file(__file__); amendment_hash = sha256_file(ROOT/'resource_amendment.py')
    allocator_hash = sha256_file(ROOT/'allocation.py')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False); root = args.output.resolve()
    policy = {'version':1,'worker_id':'synthetic-'+uuid.uuid4().hex[:12],
              'memory_budget_mb':8192,'cpu_budget':3,'host_reserve_mb':4096,'host_reserve_cpu':1,
              'state_directory':str(root/'worker-state'),'owner':'Synthetic rehearsal operator'}
    policy_path = root/'worker-policy.json'; policy_path.write_text(json.dumps(policy)); policy_path.chmod(0o600)
    roots = [root/'course-one',root/'course-two']
    for course_root in roots:
        course_root.mkdir(mode=0o700); (course_root/'source').mkdir(mode=0o700)
    capacity = {'memory_mb':512,'cpu':0.25,'active_limit':1}
    first = Notebook(roots[0]/'source',extra_users=('carol',),domain='one.rehearsal.test',
                     auth_domain='authone.rehearsal.test',client_id='course-one',capacity=capacity,allocation_policy=policy_path)
    second = Notebook(roots[1]/'source',domain='two.rehearsal.test',auth_domain='authtwo.rehearsal.test',
                      client_id='course-two',capacity=capacity,allocation_policy=policy_path)
    source_instance = first.instance; recovered = None; checks = []
    try:
        first.start(); second.start()
        initial = allocation.inventory(policy_path)
        assert initial['active_courses'] == 2 and initial['memory_reserved_mb'] == 3072 and initial['cpu_reserved'] == 2.5
        checks.append('two-managed-courses-enrolled')
        assert first.hub_image == second.hub_image and first.image == second.image
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                first_context = browser.new_context(ignore_https_errors=True); first_page = first_context.new_page()
                one, one_url = login_and_ready_with_current_quotas(first,first_page,'alice')
                expected = {'alice':save_coursework(first,one,one_url,'resource course one alice')}
                second_context = browser.new_context(ignore_https_errors=True); second_page = second_context.new_page()
                two, two_url = login_and_ready_with_current_quotas(second,second_page,'alice')
                peer_work = save_coursework(second,two,two_url,'resource course two alice')
                peer_learners = active_learners(second); assert len(peer_learners) == 1
                peer_hub = json.loads(docker('inspect',second.instance+'-hub'))[0]['Id']
                checks.append('fresh-quota-authenticated-saved-work')
                running_plan = roots[0]/'running-plan.json'
                expect_refusal(lambda:amend.preview(first.deployment,running_plan,512,0.25,2),amend.AmendmentError,
                               {'stop_course_before_allocation_change'})
                assert not running_plan.exists(); checks.append('running-target-preview-refused')
                first.compose('stop','hub'); assert_stopped(first)
                for check, memory, cpu, active in (('target-overcommit-refused',512,0.25,5),
                                                  ('peak-overcommit-refused',256,0.1,4)):
                    rejected_plan = roots[0]/(check+'.json')
                    expect_refusal(lambda:amend.preview(first.deployment,rejected_plan,memory,cpu,active),amend.AmendmentError,
                                   {'worker_reservations_exceed_budget'})
                    assert not rejected_plan.exists() and not (first.deployment/amend.PENDING).exists()
                    checks.append(check)
                expansion_plan = roots[0]/'expansion-plan.json'
                expansion = amend.preview(first.deployment,expansion_plan,768,0.375,2)
                original_env = first.env_file.read_bytes(); original_plan = expansion_plan.read_bytes()
                changed_env = original_env.replace(b'WORKSPACE_IDLE_SECONDS=1800',b'WORKSPACE_IDLE_SECONDS=1801')
                assert changed_env != original_env
                try:
                    first.env_file.write_bytes(changed_env)
                    expect_refusal(lambda:amend.apply(first.deployment,expansion_plan),amend.AmendmentError,{'stale_resource_approval'})
                finally: first.env_file.write_bytes(original_env)
                checks.append('stale-environment-approval-refused')
                original_policy = policy_path.read_bytes()
                try:
                    policy_path.write_text(json.dumps(dict(policy,host_reserve_cpu=0.9)))
                    expect_refusal(lambda:amend.apply(first.deployment,expansion_plan),amend.AmendmentError,{'worker_binding_changed'})
                finally: policy_path.write_bytes(original_policy)
                checks.append('changed-worker-policy-refused')
                ledger_path = root/'worker-state/ledger.json'; original_ledger = ledger_path.read_bytes()
                try:
                    altered = json.loads(original_ledger)
                    altered['claims'][allocation.path_digest(second.deployment)]['status'] = 'RELEASED'
                    ledger_path.write_text(json.dumps(altered))
                    expect_refusal(lambda:amend.apply(first.deployment,expansion_plan),amend.AmendmentError,{'stale_resource_approval'})
                finally: ledger_path.write_bytes(original_ledger)
                checks.append('stale-ledger-approval-refused')
                try:
                    expansion_plan.write_text('{}')
                    expect_refusal(lambda:amend.apply(first.deployment,expansion_plan),amend.AmendmentError,{'invalid_or_stale_resource_plan'})
                finally: expansion_plan.write_bytes(original_plan)
                assert not (first.deployment/amend.PENDING).exists() and first.env_file.read_bytes() == original_env
                checks.append('tampered-plan-refused')
                same_peer(second,two,two_url,peer_work,peer_learners,peer_hub); checks.append('peer-preserved-after-refusals')
                applied = amend.apply(first.deployment,expansion_plan)
                assert applied['status'] == 'COMPLETE' and applied['deployment_started'] is False
                expanded = allocation.inventory(policy_path)
                assert expanded['memory_reserved_mb'] == 4096 and expanded['cpu_reserved'] == 3
                checks.append('expanded-reservation-committed')
                assert_stopped(first); checks.append('expanded-apply-did-not-start')
                start_normally(first)
                first_context.close(); first_context = browser.new_context(ignore_https_errors=True)
                first_page = first_context.new_page(); one, one_url = login_and_ready_with_current_quotas(first,first_page,'alice')
                bob_context = browser.new_context(ignore_https_errors=True); bob_page = bob_context.new_page()
                bob, bob_url = login_and_ready_with_current_quotas(first,bob_page,'bob')
                assert len(active_learners(first)) == 2; checks.append('expanded-two-learner-admission')
                verify_runtime_policy(first,2,768,0.375)
                checks += ['normal-start-consumed-expanded-policy','expanded-docker-limits-enforced']
                expected['bob'] = save_coursework(first,bob,bob_url,'resource course one bob')
                with ThreadPoolExecutor(max_workers=2) as pool:
                    work = [pool.submit(first.kernel,one,one_url,'python3','print("COURSE_OK")'),
                            pool.submit(first.kernel,bob,bob_url,'ir','cat("COURSE_OK\\n")')]
                    for future in work: future.result()
                checks.append('expanded-python-r-validated')
                carol_context = browser.new_context(ignore_https_errors=True); carol_page = carol_context.new_page()
                login_with_current_quotas(first,carol_page,'carol'); carol = first.http_session(carol_context)
                inspect(first.homes,first.instance)
                overflow = change(carol,'POST',first.origin+'/hub/api/users/'+SUBJECTS['carol']+'/server')
                assert overflow.status_code == 429 and len(active_learners(first)) == 2
                checks.append('expanded-capacity-overflow-429')
                same_peer(second,two,two_url,peer_work,peer_learners,peer_hub); checks.append('peer-unchanged-after-expansion')
                first.compose('stop','hub'); assert_stopped(first)
                contraction_plan = roots[0]/'contraction-plan.json'
                contraction = amend.preview(first.deployment,contraction_plan,512,0.25,1)
                expanded_env = first.env_file.read_bytes()
                with patch.object(amend,'write_environment',side_effect=RuntimeError('synthetic interruption before environment publication')):
                    expect_refusal(lambda:amend.apply(first.deployment,contraction_plan),amend.AmendmentError,
                                   {'resource_amendment_refused_or_incomplete'})
                assert (first.deployment/amend.PENDING).is_file() and first.env_file.read_bytes() == expanded_env
                pending = allocation.inventory(policy_path)
                assert pending['memory_reserved_mb'] == 4096 and pending['cpu_reserved'] == 3
                checks.append('interrupted-contraction-retained-peak')
                expect_refusal(lambda:preflight(first.deployment))
                expect_refusal(lambda:compose(first.deployment,'up','-d','--no-build','--pull','never','hub'))
                expect_refusal(lambda:first.compose('up','-d','--no-build','--pull','never','hub'))
                assert_stopped(first); checks.append('pending-preflight-and-start-refused')
                cfg = load_config(first.deployment/'recovery.json')
                assert cfg['local_test'] is True and Path(cfg['repository']).is_relative_to(root)
                with patch('recovery.restic',side_effect=AssertionError('pending backup reached provider')) as provider:
                    expect_refusal(lambda:backup_repository(first.deployment,cfg))
                    provider.assert_not_called()
                checks.append('pending-backup-refused-before-provider')
                expect_refusal(lambda:maintenance.environment(first.deployment))
                expect_refusal(lambda:upgrade.environment(first.deployment))
                checks.append('pending-maintenance-and-upgrade-refused')
                review_after = (datetime.now(timezone.utc)+timedelta(days=30)).date().isoformat()
                with patch('retirement.backup_repository_locked',side_effect=AssertionError('pending retirement reached provider')) as provider:
                    expect_refusal(lambda:retirement.preview(first.deployment,first.deployment/'recovery.json',
                                   roots[0]/'forbidden-retirement.json','Synthetic retention operator',review_after),retirement.RetirementError)
                    provider.assert_not_called()
                checks.append('pending-retirement-refused-before-provider')
                expect_refusal(lambda:allocation.release(first.deployment),allocation.AllocationError)
                expect_refusal(lambda:allocation.enroll(first.deployment,policy_path),allocation.AllocationError)
                checks.append('pending-allocation-mutations-refused')
                expect_refusal(lambda:amend.apply(first.deployment,expansion_plan),amend.AmendmentError,{'resource_resume_state_changed'})
                checks.append('wrong-pending-plan-refused')
                preflight(second.deployment)
                second.compose('up','-d','--no-build','--pull','never','hub')
                same_peer(second,two,two_url,peer_work,peer_learners,peer_hub)
                checks.append('peer-admission-allowed-during-target-pending')
                resumed = amend.resume(first.deployment,contraction_plan)
                assert resumed['status'] == 'COMPLETE' and resumed['deployment_started'] is False
                assert not (first.deployment/amend.PENDING).exists()
                checks.append('exact-contraction-resumed')
                final = allocation.inventory(policy_path)
                assert final['memory_reserved_mb'] == 3072 and final['cpu_reserved'] == 2.5
                checks.append('contracted-reservation-committed')
                assert_stopped(first); checks.append('contracted-apply-did-not-start')
                preserved_work(first,expected)
                assert len(list(first.homes.glob(first.instance+'-home-*'))) == 3
                checks.append('all-homes-and-work-retained')
                start_normally(first)
                first_context.close(); first_context = browser.new_context(ignore_https_errors=True)
                first_page = first_context.new_page(); one, one_url = login_and_ready_with_current_quotas(first,first_page,'alice')
                verify_runtime_policy(first,1,512,0.25); checks.append('normal-start-consumed-contracted-policy')
                bob_context.close(); bob_context = browser.new_context(ignore_https_errors=True); bob_page = bob_context.new_page()
                login_with_current_quotas(first,bob_page,'bob'); bob = first.http_session(bob_context)
                inspect(first.homes,first.instance)
                overflow = change(bob,'POST',first.origin+'/hub/api/users/'+SUBJECTS['bob']+'/server')
                assert overflow.status_code == 429 and len(active_learners(first)) == 1
                checks.append('contracted-capacity-overflow-429')
                preserved_work(first,expected)
                assert one.get(one_url+'/api/contents/course.py',timeout=20).json()['content'] == expected['alice']['course.py']
                first.kernel(one,one_url,'python3','from pathlib import Path\nassert Path("course.py").read_text() == '+repr(expected['alice']['course.py'])+'\nprint("COURSE_OK")')
                checks.append('saved-work-after-contracted-restart')
                same_peer(second,two,two_url,peer_work,peer_learners,peer_hub); checks.append('peer-unchanged-through-contraction')
                first.compose('stop','hub'); assert_stopped(first)
                preserved = {path:path.read_bytes() for path in
                             (first.env_file,first.deployment/allocation.MARKER,root/'worker-state/ledger.json',first.deployment/amend.RECEIPT)}
                expect_refusal(lambda:amend.apply(first.deployment,expansion_plan),amend.AmendmentError,
                               {'resource_receipt_history_changed'})
                assert all(path.read_bytes() == raw for path,raw in preserved.items())
                assert not (first.deployment/amend.PENDING).exists(); assert_stopped(first)
                checks.append('old-approval-replay-refused')
                verify_missing_receipt_replay(amend,first,expansion_plan,
                    (first.env_file,first.deployment/allocation.MARKER,root/'worker-state/ledger.json'))
                checks.append('missing-receipt-approval-replay-refused')
                receipt_history_sha = sha256_file(first.deployment/amend.RECEIPT)
                restic(cfg,'init'); snapshot = backup_repository(first.deployment,cfg)
                assert_stopped(first); checks.append('stopped-backup-preserved-resource-history')
                verify_history_bundle(cfg,snapshot,roots[0],receipt_history_sha)
                checks.append('encrypted-history-bundle-verified')
                disk = roots[0]/'replacement.img'
                with disk.open('xb') as output: output.truncate(2*1024**3)
                homes = roots[0]/'replacement-homes'; homes.mkdir()
                xfs_run('mkfs.xfs','-f',str(disk)); xfs_run('mount','-o','loop,prjquota',str(disk),str(homes))
                destination = roots[0]/'restored'
                recovered = restore_repository(cfg,snapshot,destination,port(),homes,first.env_file)
                assert 'WORKSPACE_RESOURCE_REVISION' not in recovered
                assert 'WORKSPACE_RESOURCE_REVISION' not in read_environment(destination/'.env')
                history = destination/'source-resource-amendment-receipt.json'
                assert sha256_file(history) == receipt_history_sha and history.stat().st_mode & 0o777 == 0o600
                assert not (destination/amend.RECEIPT).exists() and not (destination/amend.PENDING).exists()
                assert (destination/'.worker-allocation-required.json').is_file() and not (destination/'.worker-allocation.json').exists()
                checks.append('managed-restore-kept-history-only')
                expect_refusal(lambda:preflight(destination))
                result = subprocess.run([sys.executable,str(destination/'runtime/deployment.py'),'start','--directory',str(destination)],
                                        capture_output=True,text=True)
                assert result.returncode != 0 and 'enrollment' in result.stderr.lower()
                assert not docker('ps','-q','--filter','label=educloud.workspace.instance='+recovered['WORKSPACE_INSTANCE'])
                checks.append('managed-restored-start-refused')
                expect_refusal(lambda:allocation.enroll(destination,policy_path),allocation.AllocationError,
                               {'course_identity_or_storage_collision'})
                assert (destination/'.worker-allocation-required.json').is_file()
                assert allocation.inventory(policy_path)['active_courses'] == 2
                checks.append('restored-identity-reuse-refused')
                import copy
                restored_fixture = copy.copy(first)
                restored_fixture.instance = recovered['WORKSPACE_INSTANCE']; restored_fixture.homes = homes
                preserved_work(restored_fixture,expected); checks.append('restored-saved-work-preserved')
                same_peer(second,two,two_url,peer_work,peer_learners,peer_hub)
                checks.append('peer-unchanged-through-closed-recovery')
                for fixture in (first,second): fixture.verify_manifest_frozen()
                assert _source(ROOT.parent) == source_state and sha256_file(__file__) == fixture_hash
                assert sha256_file(ROOT/'resource_amendment.py') == amendment_hash and sha256_file(ROOT/'allocation.py') == allocator_hash
                report = {'waypoint_revision':source_state['revision'],'fixture_sha256':fixture_hash,
                          'amendment_sha256':amendment_hash,'allocator_sha256':allocator_hash,
                          'source_manifests_sha256':[first.manifest_digest,second.manifest_digest],
                          'policy_sha256':initial['policy_sha256'],'expansion_plan_sha256':expansion['plan_sha256'],
                          'contraction_plan_sha256':contraction['plan_sha256'],
                          'snapshot_id':snapshot,'receipt_history_sha256':receipt_history_sha,
                          'images':{'hub':first.hub_image,'course':first.image},
                          'initial_allocation':bounded_allocation(initial),'expanded_allocation':bounded_allocation(expanded),
                          'pending_allocation':bounded_allocation(pending),'final_allocation':bounded_allocation(final),'checks':sorted(checks)}
            finally:
                browser.close()
    finally:
        cleanup_verified = cleanup_courses(first,second,roots[0],source_instance,recovered,policy_path)
    publish_pass(root,report,cleanup_verified)
    print('PASS: stopped course expanded and contracted through explicit operator starts; interrupted contraction stayed gated and reserved; peer course and saved work preserved; cleanup verified.',flush=True)


if __name__=='__main__': main()
