#!/usr/bin/env python3
"""Review and transfer a stopped synthetic course; keep its peer live."""
import argparse
import copy
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch
import uuid

from playwright.sync_api import sync_playwright
from notebook import Notebook, SUBJECTS, ROOT
from backup import docker
from deployment import preflight, compose, read_environment
from evidence import _source, sha256_file
from homes import inspect, run as xfs_run
from recovery import load_config, backup_repository, restic, restore_repository
from verify import port
from verify_multiple_courses import (AUTHORITY, cleanup_worker, active_learners,
    login_with_current_quotas, login_and_ready_with_current_quotas, save_coursework)
from verify_resource_amendment import expect_refusal, assert_stopped, same_peer, preserved_work, verify_runtime_policy
from verify_institutional_recovery import wait_proxy, browser_after_cutover
from smoke import change
import maintenance
import retirement
import upgrade

CHECKS = frozenset({
    'two-managed-courses-enrolled', 'fresh-quota-python-r-saved-work',
    'stopped-source-backup', 'fresh-third-filesystem-managed-receiver',
    'ordinary-receiver-enrollment-collision-refused', 'forward-preview-no-start',
    'stale-environment-approval-refused', 'tampered-plan-refused',
    'interrupted-transfer-kept-conservative-source-lease', 'both-participant-journals-durable',
    'pending-public-and-private-start-refused', 'pending-backup-refused-before-provider',
    'pending-maintenance-upgrade-retirement-resource-refused', 'pending-enroll-release-refused',
    'peer-admission-allowed-during-pending', 'exact-forward-resume-complete',
    'transfer-preserved-worker-reservation', 'forward-apply-no-start',
    'replaced-source-start-backup-mutations-refused', 'source-fence-loss-does-not-restore-admission',
    'pre-use-reviewed-rollback-complete',
    'rollback-apply-no-start', 'rollback-retained-full-home-equality',
    'reviewed-immediate-receiver-reactivation', 'reactivation-no-start',
    'old-forward-and-rollback-approvals-refused', 'missing-history-refused-and-restored',
    'peer-unchanged-through-reviewed-roundtrip', 'bundled-operator-started-receiver',
    'scoped-loopback-relay-original-origin', 'old-hub-cookie-refused',
    'fresh-receiver-login-saved-python-r', 'receiver-docker-resource-limits',
    'receiver-capacity-overflow-429', 'peer-unchanged-after-receiver-use',
    'receiver-only-work-preserved', 'rollback-refused-full-home-divergence',
    'stopped-active-receiver-backup', 'encrypted-cutover-history-runtime-verified',
    'fourth-namespace-restored-history-only', 'closed-history-start-and-enroll-refused',
    'closed-history-saved-work-preserved', 'peer-unchanged-through-closed-recovery',
})
HASH = re.compile(r'[0-9a-f]{64}\Z')
IMAGE = re.compile(r'(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64})\Z')
ALLOCATION_FIELDS = {'active_courses','released_courses','transferred_courses','memory_reserved_mb','cpu_reserved',
                     'memory_budget_mb','cpu_budget','host_reserve_mb','host_reserve_cpu'}
REPORT_HASHES = {'fixture_sha256','cutover_sha256','allocator_sha256','policy_sha256',
                 'initial_snapshot_id','receiver_snapshot_id','forward_plan_sha256','rollback_plan_sha256',
                 'reactivation_plan_sha256','receipt_history_sha256'}


def bounded_allocation(value):
    return {key:value[key] for key in ALLOCATION_FIELDS}


def publish_pass(root, report, cleanup_verified):
    """Publication is exclusive and requires every control and owned cleanup."""
    keys = REPORT_HASHES | {'waypoint_revision','source_manifests_sha256','images','initial_allocation',
                           'pending_allocation','transferred_allocation','rollback_allocation','final_allocation','checks'}
    if cleanup_verified is not True or not isinstance(report,dict) or set(report) != keys:
        raise ValueError('cutover PASS requires complete bounded evidence and verified cleanup')
    if (not isinstance(report['waypoint_revision'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',report['waypoint_revision'])
            or any(not isinstance(report[key],str) or not HASH.fullmatch(report[key]) for key in REPORT_HASHES)
            or not isinstance(report['source_manifests_sha256'],list) or len(report['source_manifests_sha256']) != 2
            or any(not isinstance(value,str) or not HASH.fullmatch(value) for value in report['source_manifests_sha256'])
            or len(set(report['source_manifests_sha256'])) != 2
            or not isinstance(report['images'],dict) or set(report['images']) != {'hub','course'}
            or any(not isinstance(value,str) or not IMAGE.fullmatch(value) for value in report['images'].values())
            or not isinstance(report['checks'],list) or any(not isinstance(value,str) for value in report['checks'])
            or set(report['checks']) != CHECKS or len(report['checks']) != len(CHECKS)):
        raise ValueError('cutover evidence schema or controls invalid')
    for key, transferred in (('initial_allocation',0),('pending_allocation',0),('transferred_allocation',1),
                             ('rollback_allocation',1),('final_allocation',1)):
        value=report[key]
        if (not isinstance(value,dict) or set(value) != ALLOCATION_FIELDS
                or any(type(number) not in (int,float) or not 0 <= number <= 1048576 or not math.isfinite(number) for number in value.values())
                or any(type(value[field]) is not int for field in ('active_courses','released_courses','transferred_courses',
                    'memory_reserved_mb','memory_budget_mb','host_reserve_mb'))
                or value != dict(active_courses=2,released_courses=0,transferred_courses=transferred,
                    memory_reserved_mb=3072,cpu_reserved=2.5,memory_budget_mb=8192,cpu_budget=2.75,
                    host_reserve_mb=4096,host_reserve_cpu=1.25)):
            raise ValueError('cutover conservative lease evidence invalid')
    data=(json.dumps(dict(report,version=1,status='PASS',scope='same-worker-synthetic-reviewed-cutover',
            apply_started_services=False,operator_started_receiver=True,rollback_completed_before_receiver_use=True,
            rollback_after_receiver_divergence=False,closed_history_recovery_only=True,cleanup_verified=True),
            sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    if len(data)>64*1024: raise ValueError('cutover evidence budget exceeded')
    root=Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('cutover evidence directory must be private')
    fd,temporary=tempfile.mkstemp(prefix='.cutover-evidence-',dir=root)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o600); output.write(data); output.flush(); os.fsync(output.fileno())
        os.link(temporary,root/'CUTOVER-PASS.json')
    finally: os.unlink(temporary)


def restored_view(fixture, directory, homes, env):
    view=copy.copy(fixture)
    view.instance=env['WORKSPACE_INSTANCE']; view.env=env
    view.deployment=Path(directory); view.env_file=view.deployment/'.env'; view.homes=Path(homes)
    view.mounted=os.path.ismount(homes)
    return view


def fresh_filesystem(root, name):
    disk=root/(name+'.img')
    with disk.open('xb') as output: output.truncate(2*1024**3)
    homes=root/(name+'-homes'); homes.mkdir(mode=0o700)
    xfs_run('mkfs.xfs','-f',str(disk)); xfs_run('mount','-o','loop,prjquota',str(disk),str(homes))
    return homes


def cleanup_cutover(first, peer, root, policy_path):
    """Dispose all exact invocation namespaces, including partial restores."""
    failures=[]; directories=[first.deployment,peer.deployment]
    # Recover private env after partial restore; keep each namespace's mount separate.
    for name in ('receiver','closed-history'):
        directory=root/name; homes=root/(name+'-homes')
        try:
            if (directory/'.env').exists():
                env=read_environment(directory/'.env'); instance=env.get('WORKSPACE_INSTANCE','')
                if not re.fullmatch(r'recovered-[0-9a-f]{12}',instance):
                    raise RuntimeError('invalid restored fixture ownership')
                view=restored_view(first,directory,homes,env)
                if hasattr(view,'session'): del view.session
                if view.close() is not True or view.cleanup_verified is not True:
                    raise RuntimeError('restored cleanup unverified')
                if name=='receiver': directories.append(directory)
        except Exception: failures.append(name+'-cleanup-failed')
        # Mount can precede .env publication; independently dispose that owned mount.
        try:
            if os.path.ismount(homes): xfs_run('umount',str(homes))
            if os.path.ismount(homes): raise RuntimeError('owned mount remains')
        except Exception: failures.append(name+'-mount-cleanup-failed')
    for name,fixture in (('source',first),('peer',peer)):
        try:
            if fixture.close() is not True or fixture.cleanup_verified is not True:
                raise RuntimeError('course cleanup unverified')
        except Exception: failures.append(name+'-cleanup-failed')
    try:
        # Relay is independently verified even though source.close owns its label.
        if docker('ps','-aq','--filter','name=^/'+first.instance+'-cutover-relay$'):
            failures.append('relay-remains')
    except Exception: failures.append('relay-absence-unverified')
    if not failures:
        try:
            if cleanup_worker(policy_path,directories) is not True: failures.append('authority-cleanup-unverified')
        except Exception: failures.append('authority-cleanup-failed')
    if failures: raise RuntimeError('cutover cleanup unverified: '+', '.join(failures))
    return True


# Trusted transport only: bounded concurrent streams, no payload logging or mounts.
RELAY_SCRIPT = '''import socket, threading, select, sys
gateway, port, target = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
slots = threading.BoundedSemaphore(8)
def pipe(client):
    upstream = None
    try:
        upstream = socket.create_connection(("127.0.0.1", target), timeout=10)
        client.setblocking(False); upstream.setblocking(False)
        sockets = (client, upstream)
        while True:
            ready, _, _ = select.select(sockets, (), (), 120)
            if not ready: break
            for source in ready:
                data = source.recv(65536)
                if not data: return
                destination = upstream if source is client else client
                view = memoryview(data)
                while view:
                    if not select.select((), (destination,), (), 10)[1]: return
                    view = view[destination.send(view):]
    except (OSError, ValueError): pass
    finally:
        client.close()
        if upstream is not None: upstream.close()
        slots.release()
listener = socket.socket(); listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind((gateway, port)); listener.listen(8)
while True:
    client, _ = listener.accept()
    if slots.acquire(False): threading.Thread(target=pipe, args=(client,), daemon=True).start()
    else: client.close()
'''


def start_relay(first, receiver):
    """Bridge the fixture proxy to the real loopback-only receiver endpoint."""
    if not re.fullmatch(r'rehearsal-[0-9a-f]{10}',first.instance) or not IMAGE.fullmatch(first.image):
        raise ValueError('relay requires exact synthetic ownership and immutable image')
    network=json.loads(docker('network','inspect',first.network))[0]
    if (network['Name']!=first.network or network['Labels'].get('educloud.rehearsal')!=first.instance
            or network['Driver']!='bridge' or len(network['IPAM']['Config'])!=1):
        raise ValueError('relay requires owned bridge gateway')
    gateway=network['IPAM']['Config'][0]['Gateway']
    if not ipaddress.ip_address(gateway).is_private or ipaddress.ip_address(gateway).is_loopback:
        raise ValueError('relay gateway invalid')
    backend=int(receiver.env['WORKSPACE_PORT']); relay_port=port()
    if not 1024<=backend<=65535 or not 1024<=relay_port<=65535 or backend==relay_port:
        raise ValueError('relay ports invalid')
    name=first.instance+'-cutover-relay'
    if docker('ps','-aq','--filter','name=^/'+name+'$'): raise ValueError('relay name already exists')
    identity=docker('run','-d','--name',name,'--label','educloud.rehearsal='+first.instance,
        '--network','host','--read-only','--user','1000:1000','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--memory','64m','--memory-swap','64m',
        '--cpus','0.05','--pids-limit','16','--entrypoint','python3',first.image,
        '-I','-S','-c',RELAY_SCRIPT,gateway,str(relay_port),str(backend))
    state=json.loads(docker('inspect',identity))[0]
    assert state['State']['Running'] is True and state['Image']==first.image
    assert state['Config']['Labels'].get('educloud.rehearsal')==first.instance
    assert state['Mounts']==[] and state['HostConfig']['NetworkMode']=='host'
    assert state['HostConfig']['ReadonlyRootfs'] is True and state['Config']['User']=='1000:1000'
    assert state['HostConfig']['Memory']==64*1024**2 and state['HostConfig']['NanoCpus']==50000000
    nginx=first.root/'nginx.conf'; original=nginx.read_text()
    old='http://'+first.instance+'-hub:8000'; new='http://'+gateway+':'+str(relay_port)
    assert original.count(old)==1 and 'http://identity:8080' in original
    changed=original.replace(old,new)
    assert changed.count(new)==1 and 'http://identity:8080' in changed
    nginx.write_text(changed)
    docker('restart',first.instance+'-proxy'); wait_proxy(first)
    return identity


def bundled_start(receiver):
    inspect(receiver.homes,receiver.instance)
    subprocess.run([sys.executable,str(receiver.deployment/'runtime/deployment.py'),'start','--directory',
                    str(receiver.deployment)],check=True)


def verify_peer(fixture, session, url, expected, learners, hub_id):
    same_peer(fixture,session,url,expected,learners,hub_id)
    response=session.get(url+'/api/contents/course-r.txt',timeout=20)
    assert response.status_code==200 and response.json()['content']==expected['course-r.txt']
    fixture.kernel(session,url,'ir','stopifnot(paste(readLines("course-r.txt"),collapse="\\n")=='+
                   json.dumps(expected['course-r.txt'].rstrip('\n'))+'); cat("COURSE_OK\\n")')


def receiver_start_with_fixture_trust(first, receiver):
    bundled_start(receiver)
    hub=receiver.instance+'-hub'
    docker('network','connect',first.network,hub)
    docker('cp',str(first.root/'ca.pem'),hub+':/usr/local/share/ca-certificates/fixture.crt')
    docker('exec',hub,'update-ca-certificates')
    before=json.loads(docker('inspect',hub))[0]['Id']
    compose(receiver.deployment,'stop','hub'); bundled_start(receiver)
    assert json.loads(docker('inspect',hub))[0]['Id']==before
    assert json.loads(docker('inspect',hub))[0]['Image']==receiver.hub_image


def missing_history_control(cut, source, receiver, plan, protected_paths):
    receipt=receiver.deployment/cut.RECEIPT; original=receipt.read_bytes()
    protected={path:path.read_bytes() for path in protected_paths}
    receipt.unlink()
    try:
        expect_refusal(lambda:cut.preview(receiver.deployment,source.deployment,plan,'rollback'),cut.CutoverError,
                       {'completed_cutover_history_required'})
        assert not plan.exists() and all(path.read_bytes()==raw for path,raw in protected.items())
        assert_stopped(source); assert_stopped(receiver)
    finally:
        fd=os.open(receipt,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as output:
            output.write(original); output.flush(); os.fsync(output.fileno())
        directory=os.open(receipt.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    assert receipt.read_bytes()==original and receipt.stat().st_mode & 0o777==0o600


def fence_loss_control(cut, source, policy_path, ledger_path):
    """A removed convenience fence cannot undo the daemon-wide transfer lease."""
    import allocation
    fence=source.deployment/cut.FENCE; original=fence.read_bytes()
    protected={path:path.read_bytes() for path in (source.env_file,source.deployment/allocation.MARKER,
                                                  source.deployment/cut.RECEIPT,ledger_path)}
    fence.unlink()
    try:
        expect_refusal(lambda:allocation.check(source.deployment,read_environment(source.env_file)),allocation.AllocationError,
                       {'active_course_reservation_required'})
        expect_refusal(lambda:allocation.enroll(source.deployment,policy_path),allocation.AllocationError,
                       {'transferred_course_cannot_enroll'})
        expect_refusal(lambda:allocation.release(source.deployment),allocation.AllocationError,
                       {'transferred_course_cannot_release'})
        assert all(path.read_bytes()==raw for path,raw in protected.items()); assert_stopped(source)
    finally:
        fd=os.open(fence,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as output:
            output.write(original); output.flush(); os.fsync(output.fileno())
        directory=os.open(fence.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    assert fence.read_bytes()==original


def reject_lifecycle(fixture, cfg, policy, plan_root, *, pending):
    """Use actual public/private control paths, with provider tripwires."""
    import allocation
    import resource_amendment as amend
    expect_refusal(lambda:preflight(fixture.deployment))
    expect_refusal(lambda:compose(fixture.deployment,'up','-d','--no-build','--pull','never','hub'))
    expect_refusal(lambda:fixture.compose('up','-d','--no-build','--pull','never','hub'))
    expect_refusal(lambda:allocation.check(fixture.deployment,read_environment(fixture.env_file)),allocation.AllocationError,
                   {'unfinished_cutover'} if pending else {'allocation_refused_or_incomplete'})
    with patch('recovery.restic',side_effect=AssertionError('closed backup reached provider')) as provider:
        expect_refusal(lambda:backup_repository(fixture.deployment,cfg)); provider.assert_not_called()
    for fn in (maintenance.environment,upgrade.environment,retirement.environment):
        expect_refusal(lambda fn=fn:fn(fixture.deployment))
    expect_refusal(lambda:amend.preview(fixture.deployment,plan_root/(fixture.instance+'-forbidden-resource.json'),512,0.25,1))
    expect_refusal(lambda:allocation.enroll(fixture.deployment,policy),allocation.AllocationError)
    expect_refusal(lambda:allocation.release(fixture.deployment),allocation.AllocationError)
    assert_stopped(fixture)


def verify_history_bundle(cfg, snapshot, root, receipt_sha):
    with tempfile.TemporaryDirectory(prefix='.cutover-history-proof-',dir=root) as scratch:
        restic(cfg,'restore',snapshot,'--target',scratch,'--verify')
        bundle=Path(scratch)/'bundle'
        assert sha256_file(bundle/'cutover-receipt.json')==receipt_sha
        assert sha256_file(bundle/'runtime/cutover.py')==sha256_file(ROOT/'cutover.py')
        assert json.loads((bundle/'bundle.json').read_text())['version']==4


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    if not __debug__: parser.error('optimized Python cannot enforce cutover acceptance assertions')
    parser.add_argument('--output',type=Path,required=True); args=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0: parser.error('use root on a fresh disposable native Linux worker')
    if AUTHORITY.exists() or AUTHORITY.is_symlink(): parser.error('cutover rehearsal requires fresh disposable worker authority')
    import allocation
    import cutover as cut
    source_state=_source(ROOT.parent); assert source_state['dirty'] is False, 'cutover acceptance needs clean source'
    fixture_hash=sha256_file(__file__); cutover_hash=sha256_file(ROOT/'cutover.py'); allocator_hash=sha256_file(ROOT/'allocation.py')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False); root=args.output.resolve()
    policy={'version':1,'worker_id':'synthetic-'+uuid.uuid4().hex[:12],'memory_budget_mb':8192,'cpu_budget':2.75,
            'host_reserve_mb':4096,'host_reserve_cpu':1.25,'state_directory':str(root/'worker-state'),
            'owner':'Synthetic rehearsal operator'}
    policy_path=root/'worker-policy.json'; policy_path.write_text(json.dumps(policy)); policy_path.chmod(0o600)
    for name in ('source','peer'): (root/name).mkdir(mode=0o700)
    capacity={'memory_mb':512,'cpu':0.25,'active_limit':1}
    first=Notebook(root/'source',domain='one.rehearsal.test',auth_domain='authone.rehearsal.test',
                   client_id='course-one',capacity=capacity,allocation_policy=policy_path)
    peer=Notebook(root/'peer',domain='two.rehearsal.test',auth_domain='authtwo.rehearsal.test',
                  client_id='course-two',capacity=capacity,allocation_policy=policy_path)
    checks=[]
    try:
        first.start(); peer.start()
        initial=allocation.inventory(policy_path)
        assert bounded_allocation(initial)==dict(active_courses=2,released_courses=0,transferred_courses=0,
            memory_reserved_mb=3072,cpu_reserved=2.5,memory_budget_mb=8192,cpu_budget=2.75,host_reserve_mb=4096,host_reserve_cpu=1.25)
        checks.append('two-managed-courses-enrolled')
        assert first.hub_image==peer.hub_image and first.image==peer.image
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                context=browser.new_context(ignore_https_errors=True)
                one,one_url=login_and_ready_with_current_quotas(first,context.new_page(),'alice')
                expected={'alice':save_coursework(first,one,one_url,'cutover source alice')}
                peer_context=browser.new_context(ignore_https_errors=True)
                two,two_url=login_and_ready_with_current_quotas(peer,peer_context.new_page(),'alice')
                peer_work=save_coursework(peer,two,two_url,'cutover independent peer alice')
                peer_learners=active_learners(peer); peer_hub=json.loads(docker('inspect',peer.instance+'-hub'))[0]['Id']
                checks.append('fresh-quota-python-r-saved-work')
                first.compose('stop','hub'); assert_stopped(first)
                cfg=load_config(first.deployment/'recovery.json')
                assert cfg['local_test'] is True and Path(cfg['repository']).is_relative_to(root)
                restic(cfg,'init'); initial_snapshot=backup_repository(first.deployment,cfg)
                assert_stopped(first); checks.append('stopped-source-backup')
                receiver_homes=fresh_filesystem(root,'receiver'); destination=root/'receiver'
                recovered=restore_repository(cfg,initial_snapshot,destination,port(),receiver_homes,first.env_file)
                receiver=restored_view(first,destination,receiver_homes,recovered)
                assert (destination/allocation.REQUIRED).exists() and not (destination/allocation.MARKER).exists()
                assert_stopped(receiver); preserved_work(receiver,expected)
                checks.append('fresh-third-filesystem-managed-receiver')
                expect_refusal(lambda:allocation.enroll(destination,policy_path),allocation.AllocationError,
                               {'course_identity_or_storage_collision'})
                assert (destination/allocation.REQUIRED).exists()
                checks.append('ordinary-receiver-enrollment-collision-refused')
                forward_plan=root/'forward-plan.json'; forward=cut.preview(first.deployment,destination,forward_plan)
                assert forward['status']=='PLAN_READY' and forward['deployment_started'] is False
                assert_stopped(first); assert_stopped(receiver); checks.append('forward-preview-no-start')
                original_env=first.env_file.read_bytes(); original_plan=forward_plan.read_bytes()
                changed=original_env.replace(b'WORKSPACE_IDLE_SECONDS=1800',b'WORKSPACE_IDLE_SECONDS=1801'); assert changed!=original_env
                try:
                    first.env_file.write_bytes(changed)
                    expect_refusal(lambda:cut.apply(first.deployment,destination,forward_plan),cut.CutoverError,
                                   {'cutover_retained_state_changed','cutover_endpoint_state_changed','stale_cutover_approval'})
                finally: first.env_file.write_bytes(original_env)
                checks.append('stale-environment-approval-refused')
                try:
                    forward_plan.write_text('{}')
                    expect_refusal(lambda:cut.apply(first.deployment,destination,forward_plan),cut.CutoverError,
                                   {'invalid_or_stale_cutover_plan'})
                finally: forward_plan.write_bytes(original_plan)
                checks.append('tampered-plan-refused')
                before_ledger=(root/'worker-state/ledger.json').read_bytes()
                with patch.object(cut,'write_ledger',side_effect=RuntimeError('synthetic interruption before lease transfer')):
                    expect_refusal(lambda:cut.apply(first.deployment,destination,forward_plan),cut.CutoverError,
                                   {'cutover_refused_or_incomplete'})
                assert (root/'worker-state/ledger.json').read_bytes()==before_ledger
                pending=allocation.inventory(policy_path); assert bounded_allocation(pending)==bounded_allocation(initial)
                checks.append('interrupted-transfer-kept-conservative-source-lease')
                assert all((path/cut.PENDING).is_file() for path in (first.deployment,destination))
                assert (first.deployment/cut.FENCE).is_file()
                checks.append('both-participant-journals-durable')
                for fixture in (first,receiver): reject_lifecycle(fixture,cfg,policy_path,root,pending=True)
                checks += ['pending-public-and-private-start-refused','pending-backup-refused-before-provider',
                           'pending-maintenance-upgrade-retirement-resource-refused','pending-enroll-release-refused']
                preflight(peer.deployment); peer.compose('up','-d','--no-build','--pull','never','hub')
                verify_peer(peer,two,two_url,peer_work,peer_learners,peer_hub)
                checks.append('peer-admission-allowed-during-pending')
                applied=cut.resume(first.deployment,destination,forward_plan)
                assert applied['status']=='COMPLETE' and applied['deployment_started'] is False
                assert all(not (path/cut.PENDING).exists() for path in (first.deployment,destination))
                checks.append('exact-forward-resume-complete')
                transferred=allocation.inventory(policy_path)
                assert transferred['active_courses']==2 and transferred['transferred_courses']==1
                assert transferred['memory_reserved_mb']==3072 and transferred['cpu_reserved']==2.5
                checks.append('transfer-preserved-worker-reservation')
                assert_stopped(first); assert_stopped(receiver); checks.append('forward-apply-no-start')
                reject_lifecycle(first,cfg,policy_path,root,pending=False)
                checks.append('replaced-source-start-backup-mutations-refused')
                fence_loss_control(cut,first,policy_path,root/'worker-state/ledger.json')
                checks.append('source-fence-loss-does-not-restore-admission')
                rollback_plan=root/'rollback-plan.json'; rollback=cut.rollback_preview(destination,first.deployment,rollback_plan)
                reverted=cut.apply(destination,first.deployment,rollback_plan)
                assert reverted['status']=='COMPLETE' and reverted['direction']=='rollback' and reverted['deployment_started'] is False
                assert not (first.deployment/cut.FENCE).exists() and (destination/cut.FENCE).exists()
                preflight(first.deployment); rolled_back=allocation.inventory(policy_path)
                checks.append('pre-use-reviewed-rollback-complete')
                assert_stopped(first); assert_stopped(receiver); checks.append('rollback-apply-no-start')
                assert cut.tree_evidence(first.homes,first.instance)['sha256']==cut.tree_evidence(receiver.homes,receiver.instance)['sha256']
                preserved_work(first,expected); preserved_work(receiver,expected)
                checks.append('rollback-retained-full-home-equality')
                reactivation_plan=root/'reactivation-plan.json'; reactivation=cut.preview(first.deployment,destination,reactivation_plan)
                result=cut.apply(first.deployment,destination,reactivation_plan)
                assert result['status']=='COMPLETE' and result['deployment_started'] is False
                assert (first.deployment/cut.FENCE).exists() and not (destination/cut.FENCE).exists()
                preflight(destination); checks.append('reviewed-immediate-receiver-reactivation')
                assert_stopped(first); assert_stopped(receiver); checks.append('reactivation-no-start')
                protected_paths=(first.env_file,receiver.env_file,first.deployment/cut.RECEIPT,destination/cut.RECEIPT,
                                 first.deployment/cut.FENCE,root/'worker-state/ledger.json')
                protected={path:path.read_bytes() for path in protected_paths}
                for left,right,old_plan in ((first.deployment,destination,forward_plan),(destination,first.deployment,rollback_plan)):
                    expect_refusal(lambda:cut.apply(left,right,old_plan),cut.CutoverError,
                                   {'cutover_endpoint_state_changed','cutover_history_changed','cutover_revision_history_mismatch','stale_cutover_approval'})
                assert all(path.read_bytes()==raw for path,raw in protected.items())
                checks.append('old-forward-and-rollback-approvals-refused')
                missing_history_control(cut,first,receiver,root/'missing-history-plan.json',protected_paths[:-3]+protected_paths[-2:])
                checks.append('missing-history-refused-and-restored')
                verify_peer(peer,two,two_url,peer_work,peer_learners,peer_hub)
                checks.append('peer-unchanged-through-reviewed-roundtrip')
                receiver.env=read_environment(receiver.env_file)
                receiver_start_with_fixture_trust(first,receiver); checks.append('bundled-operator-started-receiver')
                start_relay(first,receiver); checks.append('scoped-loopback-relay-original-origin')
                browser=browser_after_cutover(playwright,browser,one,first.origin); checks.append('old-hub-cookie-refused')
                receiver_context=browser.new_context(ignore_https_errors=True)
                new,new_url=login_and_ready_with_current_quotas(receiver,receiver_context.new_page(),'alice')
                preserved_work(receiver,expected)
                for filename,content in expected['alice'].items():
                    response=new.get(new_url+'/api/contents/'+filename,timeout=20)
                    assert response.status_code==200 and response.json()['content']==content
                receiver.kernel(new,new_url,'python3','from pathlib import Path\nassert Path("course.py").read_text()=='+repr(expected['alice']['course.py'])+'\nprint("COURSE_OK")')
                receiver.kernel(new,new_url,'ir','stopifnot(paste(readLines("course-r.txt"),collapse="\\n")=='+json.dumps(expected['alice']['course-r.txt'].rstrip('\n'))+'); cat("COURSE_OK\\n")')
                checks.append('fresh-receiver-login-saved-python-r')
                verify_runtime_policy(receiver,1,512,0.25); checks.append('receiver-docker-resource-limits')
                bob_context=browser.new_context(ignore_https_errors=True)
                login_with_current_quotas(receiver,bob_context.new_page(),'bob'); bob=receiver.http_session(bob_context)
                inspect(receiver.homes,receiver.instance)
                overflow=change(bob,'POST',receiver.origin+'/hub/api/users/'+SUBJECTS['bob']+'/server')
                assert overflow.status_code==429 and len(active_learners(receiver))==1
                checks.append('receiver-capacity-overflow-429')
                verify_peer(peer,two,two_url,peer_work,peer_learners,peer_hub)
                checks.append('peer-unchanged-after-receiver-use')
                added='receiver-only work after explicit activation\n'
                response=new.put(new_url+'/api/contents/receiver-only.txt',json={'type':'file','format':'text','content':added},timeout=20)
                assert response.status_code in (200,201)
                compose(destination,'stop','hub'); assert_stopped(receiver)
                expected['alice']['receiver-only.txt']=added; preserved_work(receiver,expected)
                checks.append('receiver-only-work-preserved')
                protected={path:path.read_bytes() for path in protected_paths}
                rejected_plan=root/'divergent-rollback.json'
                expect_refusal(lambda:cut.rollback_preview(destination,first.deployment,rejected_plan),cut.CutoverError,
                               {'retained_home_trees_differ'})
                assert not rejected_plan.exists() and all(path.read_bytes()==raw for path,raw in protected.items())
                preserved_work(receiver,expected); assert_stopped(first); assert_stopped(receiver)
                checks.append('rollback-refused-full-home-divergence')
                receipt_sha=sha256_file(destination/cut.RECEIPT)
                receiver_snapshot=backup_repository(destination,cfg); assert_stopped(receiver)
                checks.append('stopped-active-receiver-backup')
                verify_history_bundle(cfg,receiver_snapshot,root,receipt_sha)
                checks.append('encrypted-cutover-history-runtime-verified')
                closed_homes=fresh_filesystem(root,'closed-history'); closed_directory=root/'closed-history'
                closed_env=restore_repository(cfg,receiver_snapshot,closed_directory,port(),closed_homes,receiver.env_file)
                closed=restored_view(first,closed_directory,closed_homes,closed_env)
                history=closed_directory/'source-cutover-receipt.json'
                assert sha256_file(history)==receipt_sha and history.stat().st_mode & 0o777==0o600
                assert cut.REVISION not in closed_env and cut.REVISION not in read_environment(closed.env_file)
                assert all(not (closed_directory/name).exists() for name in (cut.RECEIPT,cut.PENDING,cut.FENCE,allocation.MARKER))
                assert (closed_directory/allocation.REQUIRED).is_file()
                checks.append('fourth-namespace-restored-history-only')
                expect_refusal(lambda:preflight(closed_directory))
                result=subprocess.run([sys.executable,str(closed_directory/'runtime/deployment.py'),'start','--directory',str(closed_directory)],capture_output=True,text=True)
                assert result.returncode!=0 and 'enrollment' in result.stderr.lower()
                expect_refusal(lambda:allocation.enroll(closed_directory,policy_path),allocation.AllocationError,
                               {'course_identity_or_storage_collision'})
                assert_stopped(closed); checks.append('closed-history-start-and-enroll-refused')
                preserved_work(closed,expected); checks.append('closed-history-saved-work-preserved')
                verify_peer(peer,two,two_url,peer_work,peer_learners,peer_hub)
                checks.append('peer-unchanged-through-closed-recovery')
                final=allocation.inventory(policy_path)
                for fixture in (first,peer): fixture.verify_manifest_frozen()
                assert _source(ROOT.parent)==source_state and sha256_file(__file__)==fixture_hash
                assert sha256_file(ROOT/'cutover.py')==cutover_hash and sha256_file(ROOT/'allocation.py')==allocator_hash
                report={'waypoint_revision':source_state['revision'],'fixture_sha256':fixture_hash,'cutover_sha256':cutover_hash,
                    'allocator_sha256':allocator_hash,'policy_sha256':initial['policy_sha256'],
                    'source_manifests_sha256':[first.manifest_digest,peer.manifest_digest],'images':{'hub':first.hub_image,'course':first.image},
                    'initial_snapshot_id':initial_snapshot,'receiver_snapshot_id':receiver_snapshot,
                    'forward_plan_sha256':forward['plan_sha256'],'rollback_plan_sha256':rollback['plan_sha256'],
                    'reactivation_plan_sha256':reactivation['plan_sha256'],'receipt_history_sha256':receipt_sha,
                    'initial_allocation':bounded_allocation(initial),'pending_allocation':bounded_allocation(pending),
                    'transferred_allocation':bounded_allocation(transferred),'rollback_allocation':bounded_allocation(rolled_back),
                    'final_allocation':bounded_allocation(final),'checks':sorted(checks)}
            finally: browser.close()
    finally: cleanup_verified=cleanup_cutover(first,peer,root,policy_path)
    publish_pass(root,report,cleanup_verified)
    print('PASS: reviewed stopped cutover and pre-use rollback; explicit receiver Python/R; divergent rollback refused; peer and closed history preserved; cleanup verified.',flush=True)


if __name__=='__main__': main()
