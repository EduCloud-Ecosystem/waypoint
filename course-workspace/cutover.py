#!/usr/bin/env python3
"""Reviewed stopped same-worker replacement and rollback; never starts services."""
import argparse
from contextlib import ExitStack,redirect_stdout
from functools import wraps
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import uuid

import allocation as a
import resource_amendment as r
from deployment import validate_institutional
from pilot import locked

VERSION='educloud-cutover-v1'
PENDING='.cutover-pending.json'
FENCE='.course-replaced.json'
RECEIPT='.cutover-receipt.json'
REVISION='WORKSPACE_CUTOVER_REVISION'
MAX_ENTRIES=200000
MAX_BYTES=100*1024**3
MAX_FILE=2*1024**3
PLAN_FIELDS={'version','direction','tool_sha256','source','destination','revision','binding','ledger_sha256',
             'before_entries','after_entries','old_envs','new_envs','old_markers','new_markers','old_fences',
             'old_required','prior_receipts','prior_receipt_records','runtime','trees','recovery','digest'}


class CutoverError(ValueError):
    def __init__(self,code):self.code=code;super().__init__(code)


def guarded(fn):
    @wraps(fn)
    def run(*args,**kwargs):
        try:return fn(*args,**kwargs)
        except CutoverError:raise
        except (a.AllocationError,r.AmendmentError) as exc:raise CutoverError(exc.code) from None
        except (OSError,ValueError,KeyError,TypeError,RuntimeError,OverflowError,UnicodeError,RecursionError,subprocess.SubprocessError):
            raise CutoverError('cutover_refused_or_incomplete') from None
    return run


def tool_hash():
    root=Path(__file__).resolve().parent
    return a.digest({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
                     ('cutover.py','allocation.py','resource_amendment.py','deployment.py','pilot.py','homes.py','hub/settings.py')})


def environment(directory):
    raw,env=r.read_env(directory)
    if REVISION in env and not re.fullmatch(r'[0-9a-f]{32}',env[REVISION]):raise CutoverError('invalid_cutover_revision')
    return raw,env


def revised(raw,revision):
    lines=[];found=False
    for line in raw.splitlines():
        if line.partition('=')[0]==REVISION:line=REVISION+'='+revision;found=True
        lines.append(line)
    if not found:lines.append(REVISION+'='+revision)
    return '\n'.join(lines)+'\n'


def no_other_pending(directory,env):
    for name in ('.course-retired.json','.retirement-pending.json','.maintenance-pending.json',
                 '.upgrade-pending.json','.resource-amendment-pending.json'):
        if a.exists(directory/name):raise CutoverError('unfinished_or_retired_course')
    if a.exists(Path(env['WORKSPACE_HOME_ROOT'])/'.educloud-maintenance.json'):raise CutoverError('unfinished_home_maintenance')


def optional(path):return a.read(path) if a.exists(path) else None


def validate_receipt(value):
    fields={'version','status','role','endpoint_sha256','counterpart_sha256','active_sha256','claim_sha256',
            'tree_sha256','plan_sha256','revision','digest'}
    if (not isinstance(value,dict) or set(value)!=fields or value['version']!=VERSION or value['status']!='COMPLETE'
            or value['role'] not in ('ACTIVE','REPLACED') or not isinstance(value['revision'],str)
            or not re.fullmatch(r'[0-9a-f]{32}',value['revision'])
            or any(not isinstance(value[key],str) or not a.HASH.fullmatch(value[key]) for key in fields-{'version','status','role','revision'})
            or value['endpoint_sha256']==value['counterpart_sha256']
            or value['active_sha256']!=(value['endpoint_sha256'] if value['role']=='ACTIVE' else value['counterpart_sha256'])
            or value['digest']!=a.digest({k:v for k,v in value.items() if k!='digest'})):
        raise CutoverError('invalid_cutover_receipt')
    return value


def completed_receipt(directory,pending_plan=None):
    raw,env=environment(directory);value=optional(directory/RECEIPT)
    if value is not None:
        validate_receipt(value)
        if value['endpoint_sha256']!=a.path_digest(directory):raise CutoverError('cutover_receipt_endpoint_mismatch')
    authorized=False
    if pending_plan is not None:
        if (optional(directory/PENDING)!={'version':VERSION,'plan':pending_plan}
                or pending_plan.get('digest')!=a.digest({k:v for k,v in pending_plan.items() if k!='digest'})
                or pending_plan.get('tool_sha256')!=tool_hash() or str(directory) not in (pending_plan['source'],pending_plan['destination'])
                or raw not in (pending_plan['old_envs'][str(directory)],pending_plan['new_envs'][str(directory)])):
            raise CutoverError('cutover_pending_history_changed')
        authorized=True
    if not authorized:
        if REVISION in env and value is None:raise CutoverError('completed_cutover_history_required')
        if value is not None and value['revision']!=env.get(REVISION):raise CutoverError('cutover_revision_history_mismatch')
    return value


def metadata(info):return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


def bounded_names(descriptor,remaining):
    names=[]
    with os.scandir(descriptor) as entries:
        for item in entries:
            if len(names)>=remaining:raise CutoverError('retained_home_entry_limit')
            names.append(item.name)
    return sorted(names)


def retained_membership(root,instance,volumes):
    descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        names=bounded_names(descriptor,MAX_ENTRIES)
        physical={name for name in names if name.startswith(instance+'-home-')}
        if physical!=set(volumes):raise CutoverError('retained_home_membership_mismatch')
        for name in physical:
            if not stat.S_ISDIR(os.stat(name,dir_fd=descriptor,follow_symlinks=False).st_mode):raise CutoverError('retained_home_membership_mismatch')
    finally:os.close(descriptor)
    filters=[['name=^'+instance+'-home-'],['label=educloud.workspace.instance='+instance,'label=educloud.workspace.kind=home']]
    for selected in filters:
        args=['docker','volume','ls','-q']
        for value in selected:args.extend(['--filter',value])
        result=subprocess.run(args,capture_output=True,text=True,timeout=20,check=True)
        if len(result.stdout)>1024*1024:raise CutoverError('retained_home_volume_inventory_limit')
        rows=result.stdout.splitlines()
        if len(rows)>10000 or len(rows)!=len(set(rows)) or set(rows)!=set(volumes):raise CutoverError('retained_home_volume_membership_mismatch')


def tree_evidence(root,instance):
    """Hash every retained home using descriptor-relative, no-follow reads."""
    from homes import inspect
    with redirect_stdout(io.StringIO()):report=inspect(root,instance)
    volumes=report['volumes'];content=hashlib.sha256();drift=hashlib.sha256();count=0;total=0
    device=os.stat(root,follow_symlinks=False).st_dev
    def record(hasher,value):
        hasher.update(json.dumps(value,sort_keys=True,separators=(',',':')).encode()+b'\n')
    if not isinstance(volumes,dict) or not volumes or len(volumes)>10000:raise CutoverError('invalid_retained_homes')
    retained_membership(root,instance,volumes)
    def visit(fd,relative):
        nonlocal count,total
        before=os.fstat(fd)
        if before.st_dev!=device:raise CutoverError('retained_home_mount_boundary')
        count+=1
        if count>MAX_ENTRIES:raise CutoverError('retained_home_entry_limit')
        record(content,[relative,'directory',stat.S_IMODE(before.st_mode),before.st_uid,before.st_gid])
        record(drift,[relative,metadata(before)])
        for name in bounded_names(fd,MAX_ENTRIES-count):
            item=os.stat(name,dir_fd=fd,follow_symlinks=False);path=relative+'/'+name
            if item.st_dev!=device:raise CutoverError('retained_home_mount_boundary')
            if stat.S_ISDIR(item.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    if metadata(os.fstat(child))!=metadata(item):raise CutoverError('retained_home_changed')
                    visit(child,path)
                finally:os.close(child)
            else:
                count+=1
                if count>MAX_ENTRIES:raise CutoverError('retained_home_entry_limit')
                if stat.S_ISREG(item.st_mode):
                    if item.st_size>MAX_FILE or total+item.st_size>MAX_BYTES:raise CutoverError('retained_home_byte_limit')
                    file=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
                    try:
                        if metadata(os.fstat(file))!=metadata(item):raise CutoverError('retained_home_changed')
                        digest=hashlib.sha256();seen=0
                        while True:
                            data=os.read(file,1024*1024)
                            if not data:break
                            seen+=len(data)
                            if seen>MAX_FILE or total+seen>MAX_BYTES:raise CutoverError('retained_home_byte_limit')
                            digest.update(data)
                        if seen!=item.st_size or metadata(os.fstat(file))!=metadata(item):raise CutoverError('retained_home_changed')
                    finally:os.close(file)
                    total+=seen;record(content,[path,'file',stat.S_IMODE(item.st_mode),item.st_uid,item.st_gid,seen,digest.hexdigest()])
                elif stat.S_ISLNK(item.st_mode):
                    target=os.readlink(name,dir_fd=fd)
                    if len(os.fsencode(target))>4096:raise CutoverError('retained_home_link_limit')
                    record(content,[path,'symlink',stat.S_IMODE(item.st_mode),item.st_uid,item.st_gid,target])
                else:raise CutoverError('retained_home_special_file')
                if metadata(os.stat(name,dir_fd=fd,follow_symlinks=False))!=metadata(item):raise CutoverError('retained_home_changed')
                record(drift,[path,metadata(item)])
        if metadata(os.fstat(fd))!=metadata(before):raise CutoverError('retained_home_changed')
    for name in sorted(volumes):
        if not re.fullmatch(re.escape(instance)+r'-home-[a-f0-9]{24}',name):raise CutoverError('invalid_retained_home_name')
        fd=os.open(root/name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            initial=os.fstat(fd)
            visit(fd,name[len(instance)+1:])
            if metadata(os.stat(root/name,follow_symlinks=False))!=metadata(initial):raise CutoverError('retained_home_changed')
        finally:os.close(fd)
    retained_membership(root,instance,volumes)
    return {'sha256':content.hexdigest(),'stamp_sha256':drift.hexdigest(),'home_count':len(volumes),'entry_count':count,'bytes':total}


def snapshot(directory):
    raw,env=environment(directory);no_other_pending(directory,env)
    cfg=validate_institutional(env);claim=a.claim(directory,env)
    runtime=r.runtime_binding(directory,env)
    tree=tree_evidence(Path(cfg['home_root']),cfg['instance'])
    return raw,env,claim,runtime,tree


def receipts(plan):
    result={};active=a.path_digest(Path(plan['destination']))
    for path,role,other in ((plan['source'],'REPLACED',plan['destination']),(plan['destination'],'ACTIVE',plan['source'])):
        key=a.path_digest(Path(path));claim=plan['after_entries'][key]['claim']
        value={'version':VERSION,'status':'COMPLETE','role':role,'endpoint_sha256':key,'counterpart_sha256':a.path_digest(Path(other)),
               'active_sha256':active,'claim_sha256':a.digest(claim),'tree_sha256':plan['trees'][path]['sha256'],
               'plan_sha256':plan['digest'],'revision':plan['revision']}
        value['digest']=a.digest(value);result[path]=validate_receipt(value)
    return result


def source_fence(plan):
    return {'version':VERSION,'status':'REPLACED','source_sha256':a.path_digest(Path(plan['source'])),
            'successor_sha256':a.path_digest(Path(plan['destination'])),'revision':plan['revision'],'plan_sha256':plan['digest']}


def entry(claim,status='ACTIVE',successor=None):
    value={'status':status,'claim':claim,'claim_sha256':a.digest(claim)}
    if successor is not None:value['successor']=successor
    return value


def transfer_entries(value,source,destination,c_source,c_destination,direction):
    ks,kd=a.path_digest(source),a.path_digest(destination)
    source_entry=value['claims'].get(ks);destination_entry=value['claims'].get(kd)
    if source_entry!=entry(c_source):raise CutoverError('active_cutover_source_required')
    if direction=='forward':
        if destination_entry is not None and destination_entry!=entry(c_destination,'TRANSFERRED',ks):
            raise CutoverError('fresh_or_immediate_replaced_receiver_required')
    elif direction=='rollback':
        if destination_entry!=entry(c_destination,'TRANSFERRED',ks):raise CutoverError('immediate_replaced_receiver_required')
    else:raise CutoverError('invalid_cutover_direction')
    after={ks:entry(c_source,'TRANSFERRED',kd),kd:entry(c_destination)}
    proposed=dict(value['claims']);proposed.update(after);a.validate_claims(proposed)
    return {ks:source_entry,kd:destination_entry},after


def ensure_equal(source_cfg,destination_cfg,source_claim,destination_claim,source_runtime,destination_runtime,source_tree,destination_tree):
    if a.logical_identity(source_claim)!=a.logical_identity(destination_claim):raise CutoverError('exact_logical_identity_required')
    if source_claim['port']==destination_claim['port'] or a.physical_collides(source_claim,destination_claim):raise CutoverError('distinct_replacement_storage_and_port_required')
    if any(source_claim[k]!=destination_claim[k] for k in ('active_limit','learner_memory_mb','learner_cpu')):raise CutoverError('matching_cutover_resources_required')
    if source_cfg['subjects']!=destination_cfg['subjects']:raise CutoverError('current_roster_mismatch')
    if any(source_cfg[k]!=destination_cfg[k] for k in ('home_quota_mb','home_inode_limit')):raise CutoverError('matching_home_quota_policy_required')
    if source_runtime['images']!=destination_runtime['images']:raise CutoverError('matching_immutable_images_required')
    if source_tree['sha256']!=destination_tree['sha256']:raise CutoverError('retained_home_trees_differ')


def summary(plan,status):
    tree=plan['trees'][plan['source']]
    return {'version':VERSION,'status':status,'direction':plan['direction'],'plan_sha256':plan['digest'],
            'retained_homes':tree['home_count'],'file_entries':tree['entry_count'],'retained_bytes':tree['bytes'],'deployment_started':False}


def validate_recovery(source,destination,marker,source_instance):
    value=a.read(destination/'recovery-source.json',64*1024)
    if (set(value)!={'snapshot_id','source_instance','fresh_hub_sessions','current_roster_required'}
            or not isinstance(value['snapshot_id'],str) or not a.HASH.fullmatch(value['snapshot_id'])
            or value['source_instance']!=source_instance or value['fresh_hub_sessions'] is not True or value['current_roster_required'] is not True
            or a.read(destination/'source-worker-allocation.json',64*1024)!=marker):
        raise CutoverError('exact_source_recovery_required')
    return {'recovery_source':value,'source_worker_sha256':a.digest(marker)}


@guarded
def preview(source,destination,plan_path,direction='forward'):
    source,destination,plan_path=map(a.canonical,(source,destination,plan_path));a.root_required()
    for path in (source,destination,plan_path.parent):a.private_directory(path)
    if source==destination:raise CutoverError('distinct_cutover_endpoints_required')
    with ExitStack() as stack:
        for path in sorted((source,destination)):stack.enter_context(locked(path))
        stack.enter_context(a.worker_lock())
        if a.exists(plan_path) or not plan_path.name.endswith('.json'):raise CutoverError('new_private_cutover_plan_required')
        if any(a.exists(path/PENDING) for path in (source,destination)):raise CutoverError('unfinished_cutover')
        if a.exists(source/FENCE):raise CutoverError('active_cutover_source_required')
        rs0,es0=environment(source);rd0,ed0=environment(destination)
        marker0=a.read_marker(source);p0,ph0=a.policy(marker0['config_file']);info0,dh0=a.daemon()
        value0=a.ledger(a.binding(marker0['config_file'],p0,ph0,dh0))
        for directory,env in ((source,es0),(destination,ed0)):a.stopped(a.claim(directory,env),value0['claims'])
        rs,es,cs,us,ts=snapshot(source);rd,ed,cd,ud,td=snapshot(destination)
        marker=a.read_marker(source);p,ph=a.policy(marker['config_file']);info,dh=a.daemon()
        if marker!=a.expected_marker(source,marker['config_file'],p,ph,dh,cs):raise CutoverError('source_enrollment_changed')
        binding=a.binding(marker['config_file'],p,ph,dh);value=a.ledger(binding)
        ensure_equal(validate_institutional(es),validate_institutional(ed),cs,cd,us,ud,ts,td)
        before,after=transfer_entries(value,source,destination,cs,cd,direction)
        old_markers={str(source):marker,str(destination):optional(destination/a.MARKER)}
        old_fences={str(source):None,str(destination):optional(destination/FENCE)}
        old_required={str(source):optional(source/a.REQUIRED),str(destination):optional(destination/a.REQUIRED)}
        if old_required[str(source)] is not None:raise CutoverError('active_cutover_source_required')
        if direction=='forward' and before[a.path_digest(destination)] is None:
            if old_markers[str(destination)] is not None or old_fences[str(destination)] is not None or not a.required_gate(destination):raise CutoverError('fresh_recovery_receiver_required')
            recovery=validate_recovery(source,destination,marker,cs['instance'])
        else:
            if old_required[str(destination)] is not None or old_markers[str(destination)]!=a.expected_marker(destination,marker['config_file'],p,ph,dh,cd) or old_fences[str(destination)] is None:raise CutoverError('replaced_receiver_binding_changed')
            recovery=None
        history={};history_records={}
        for path,c in ((source,cs),(destination,cd)):
            r.completed_receipt(path)
            previous=completed_receipt(path)
            if previous is not None and previous['role']!=('ACTIVE' if path==source else 'REPLACED'):raise CutoverError('cutover_history_role_mismatch')
            history_records[str(path)]=previous
            history[str(path)]=a.digest(previous) if previous is not None else None
            a.stopped(c,value['claims'])
        if before[a.path_digest(destination)] is not None:
            hs,hd=history_records[str(source)],history_records[str(destination)]
            if (hs is None or hd is None or hs['counterpart_sha256']!=a.path_digest(destination)
                    or hd['counterpart_sha256']!=a.path_digest(source) or hs['plan_sha256']!=hd['plan_sha256']
                    or hs['revision']!=hd['revision'] or old_fences[str(destination)]!={'version':VERSION,'status':'REPLACED',
                    'source_sha256':a.path_digest(destination),'successor_sha256':a.path_digest(source),
                    'revision':hd['revision'],'plan_sha256':hd['plan_sha256']}):
                raise CutoverError('immediate_cutover_history_required')
        protected=[a.AUTHORITY,Path(p['state_directory']),Path(marker['config_file'])]
        protected.extend(Path(e['claim'][key]) for e in value['claims'].values() for key in ('directory','home_root'))
        protected.extend((destination,Path(cd['home_root'])))
        if any(a.overlap(str(plan_path),str(path)) for path in protected):raise CutoverError('cutover_plan_overlaps_retained_state')
        revision=uuid.uuid4().hex
        if not isinstance(revision,str) or not re.fullmatch(r'[a-f0-9]{32}',revision) or revision in (es.get(REVISION),ed.get(REVISION)):raise CutoverError('fresh_cutover_revision_required')
        proposed=dict(value['claims']);proposed.update(after);a.check_capacity(p,info,proposed)
        paths=(str(source),str(destination))
        plan={'version':VERSION,'direction':direction,'tool_sha256':tool_hash(),'source':paths[0],'destination':paths[1],'revision':revision,
              'binding':binding,'ledger_sha256':a.digest(value),'before_entries':before,'after_entries':after,
              'old_envs':dict(zip(paths,(rs,rd))),'new_envs':dict(zip(paths,(revised(rs,revision),revised(rd,revision)))),
              'old_markers':old_markers,'new_markers':{paths[0]:marker,paths[1]:a.expected_marker(destination,marker['config_file'],p,ph,dh,cd)},
              'old_fences':old_fences,'old_required':old_required,'prior_receipts':history,'prior_receipt_records':history_records,'runtime':dict(zip(paths,(us,ud))),
              'trees':dict(zip(paths,(ts,td))),'recovery':recovery}
        plan['digest']=a.digest(plan);r.publish_plan(plan_path,plan)
        return summary(plan,'PLAN_READY')


def validate_plan(source,destination,path):
    plan=a.read(path)
    if (set(plan)!=PLAN_FIELDS or plan['version']!=VERSION or plan['direction'] not in ('forward','rollback')
            or (plan['source'],plan['destination'])!=(str(source),str(destination)) or plan['tool_sha256']!=tool_hash()
            or not isinstance(plan['revision'],str) or not re.fullmatch(r'[a-f0-9]{32}',plan['revision'])
            or plan['digest']!=a.digest({k:v for k,v in plan.items() if k!='digest'})):
        raise CutoverError('invalid_or_stale_cutover_plan')
    paths={str(source),str(destination)};keys={a.path_digest(source),a.path_digest(destination)}
    for name in ('old_envs','new_envs','old_markers','new_markers','old_fences','old_required','prior_receipts','prior_receipt_records','runtime','trees'):
        if not isinstance(plan[name],dict) or set(plan[name])!=paths:raise CutoverError('invalid_cutover_endpoint_bindings')
    for name in ('before_entries','after_entries'):
        if not isinstance(plan[name],dict) or set(plan[name])!=keys:raise CutoverError('invalid_cutover_endpoint_bindings')
    p,ph=a.policy(plan['binding']['config_file']);info,dh=a.daemon()
    if plan['binding']!=a.binding(plan['binding']['config_file'],p,ph,dh):raise CutoverError('cutover_worker_binding_changed')
    old_claims=[];configs=[]
    for directory in (source,destination):
        key=str(directory);old=r.parse_env(directory,plan['old_envs'][key]);new=r.parse_env(directory,plan['new_envs'][key])
        if (new.get(REVISION)!=plan['revision'] or old.get(REVISION)==plan['revision']
                or any(old.get(k)!=new.get(k) for k in set(old)|set(new) if k!=REVISION)):
            raise CutoverError('cutover_revision_only_change_required')
        prior=plan['prior_receipt_records'][key]
        if prior is not None:
            validate_receipt(prior)
            if (prior['endpoint_sha256']!=a.path_digest(directory) or prior['revision']!=old.get(REVISION)
                    or prior['role']!=('ACTIVE' if directory==source else 'REPLACED')):
                raise CutoverError('cutover_prior_history_binding_changed')
        elif REVISION in old:raise CutoverError('completed_cutover_history_required')
        if plan['prior_receipts'][key]!=(a.digest(prior) if prior is not None else None):raise CutoverError('cutover_prior_history_binding_changed')
        c=a.claim(directory,old);old_claims.append(c);configs.append(validate_institutional(old))
        if plan['new_markers'][key]!=a.expected_marker(directory,plan['binding']['config_file'],p,ph,dh,c):raise CutoverError('cutover_marker_binding_changed')
        no_other_pending(directory,old)
    cs,cd=old_claims
    ensure_equal(*configs,cs,cd,plan['runtime'][str(source)],plan['runtime'][str(destination)],plan['trees'][str(source)],plan['trees'][str(destination)])
    ks,kd=a.path_digest(source),a.path_digest(destination)
    fake={'claims':{ks:plan['before_entries'][ks]}}
    if plan['before_entries'][kd] is not None:fake['claims'][kd]=plan['before_entries'][kd]
    before,after=transfer_entries(fake,source,destination,cs,cd,plan['direction'])
    if before!=plan['before_entries'] or after!=plan['after_entries']:raise CutoverError('cutover_transition_binding_changed')
    ps,pd=str(source),str(destination)
    if plan['old_markers'][ps]!=plan['new_markers'][ps] or plan['old_fences'][ps] is not None or plan['old_required'][ps] is not None:
        raise CutoverError('active_cutover_source_required')
    if before[kd] is None:
        if (plan['direction']!='forward' or plan['old_markers'][pd] is not None or plan['old_fences'][pd] is not None
                or plan['old_required'][pd]!={'version':'educloud-worker-allocation-required-v1','source':'snapshot_restore'}
                or plan['prior_receipt_records'][pd] is not None):raise CutoverError('fresh_recovery_receiver_required')
    else:
        hs,hd=plan['prior_receipt_records'][ps],plan['prior_receipt_records'][pd]
        if (plan['old_markers'][pd]!=plan['new_markers'][pd] or plan['old_required'][pd] is not None
                or hs is None or hd is None or hs['counterpart_sha256']!=kd or hd['counterpart_sha256']!=ks
                or hs['revision']!=hd['revision'] or hs['plan_sha256']!=hd['plan_sha256']
                or plan['old_fences'][pd]!={'version':VERSION,'status':'REPLACED','source_sha256':kd,'successor_sha256':ks,
                    'revision':hd['revision'],'plan_sha256':hd['plan_sha256']}):raise CutoverError('immediate_cutover_history_required')
    if plan['direction']=='forward' and plan['before_entries'][kd] is None and validate_recovery(source,destination,plan['old_markers'][str(source)],cs['instance'])!=plan['recovery']:raise CutoverError('cutover_recovery_binding_changed')
    return plan,p,info


def verify_approval_evidence(plan):
    for rawpath in (plan['source'],plan['destination']):
        directory=Path(rawpath);old=r.parse_env(directory,plan['old_envs'][rawpath])
        if (r.runtime_binding(directory,old)!=plan['runtime'][rawpath]
                or tree_evidence(Path(old['WORKSPACE_HOME_ROOT']),old['WORKSPACE_INSTANCE'])!=plan['trees'][rawpath]):
            raise CutoverError('cutover_retained_state_changed')


def write_ledger(path,value):a.write(path,value)


def remove(path):
    if a.exists(path):path.unlink();a.sync(path.parent)


def verify_final(plan,value):
    expected_receipts=receipts(plan)
    for rawpath in (plan['source'],plan['destination']):
        path=Path(rawpath);key=a.path_digest(path)
        if (environment(path)[0]!=plan['new_envs'][rawpath] or a.read_marker(path)!=plan['new_markers'][rawpath]
                or value['claims'].get(key)!=plan['after_entries'][key] or optional(path/RECEIPT)!=expected_receipts[rawpath]):
            raise CutoverError('durable_cutover_unverified')
    if optional(Path(plan['source'])/FENCE)!=source_fence(plan):raise CutoverError('durable_source_fence_unverified')
    for rawpath in (plan['source'],plan['destination']):
        _,_,claim,runtime,tree=snapshot(Path(rawpath))
        a.stopped(claim,value['claims'])
        if runtime!=plan['runtime'][rawpath] or tree!=plan['trees'][rawpath]:raise CutoverError('cutover_retained_state_changed')


@guarded
def apply(source,destination,plan_path):
    source,destination,plan_path=map(a.canonical,(source,destination,plan_path));a.root_required()
    for path in (source,destination):a.private_directory(path)
    with ExitStack() as stack:
        for path in sorted((source,destination)):stack.enter_context(locked(path))
        stack.enter_context(a.worker_lock())
        plan,p,info=validate_plan(source,destination,plan_path);value=a.ledger(plan['binding'])
        for directory in (source,destination):
            a.stopped(plan['after_entries'][a.path_digest(directory)]['claim'],value['claims'])
            r.completed_receipt(directory)
        keys=(a.path_digest(source),a.path_digest(destination));paths=(str(source),str(destination));done_receipts=receipts(plan)
        pending=any(a.exists(path/PENDING) for path in (source,destination))
        if not pending:
            for directory in (source,destination):completed_receipt(directory)
        if not pending and all(optional(Path(path)/RECEIPT)==done_receipts[path] for path in paths):
            verify_final(plan,value)
            if a.exists(destination/FENCE) or a.exists(destination/a.REQUIRED):raise CutoverError('cutover_receiver_still_closed')
            a.check_capacity(p,info,value['claims'])
            for path in paths:a.stopped(plan['after_entries'][a.path_digest(Path(path))]['claim'],value['claims'])
            return summary(plan,'COMPLETE')
        # Accept only bound partial states while both endpoints remain closed.
        # Peer leases may evolve; target claims and approval may not.
        actual_entries={key:value['claims'].get(key) for key in keys}
        switched=actual_entries==plan['after_entries']
        if actual_entries not in (plan['before_entries'],plan['after_entries']):raise CutoverError('cutover_lease_state_changed')
        for path in paths:
            directory=Path(path);journal=optional(directory/PENDING)
            if journal is not None and journal!={'version':VERSION,'plan':plan}:raise CutoverError('cutover_journal_changed')
            raw=environment(directory)[0];marker=optional(directory/a.MARKER);fence=optional(directory/FENCE);required=optional(directory/a.REQUIRED)
            if (raw not in (plan['old_envs'][path],plan['new_envs'][path]) or marker not in (plan['old_markers'][path],plan['new_markers'][path])
                    or fence not in (plan['old_fences'][path],source_fence(plan) if path==str(source) else None)
                    or required not in (plan['old_required'][path],None)):
                raise CutoverError('cutover_endpoint_state_changed')
            history=completed_receipt(directory,plan if journal is not None else None)
            historical_hash=a.digest(history) if history is not None else None
            if historical_hash!=plan['prior_receipts'][path]:
                if history!=done_receipts[path] or not switched or raw!=plan['new_envs'][path] or marker!=plan['new_markers'][path]:raise CutoverError('cutover_history_changed')
            if switched and (environment(source)[0]!=plan['new_envs'][str(source)] or environment(destination)[0]!=plan['new_envs'][str(destination)]
                             or optional(source/FENCE)!=source_fence(plan)):
                raise CutoverError('cutover_transfer_order_changed')
            if not switched and (marker!=plan['old_markers'][path] or required!=plan['old_required'][path]
                                 or history==done_receipts[path]):raise CutoverError('cutover_transfer_order_changed')
        if not pending:
            if (a.digest(value)!=plan['ledger_sha256'] or any(environment(Path(path))[0]!=plan['old_envs'][path] for path in paths)
                    or any(optional(Path(path)/FENCE)!=plan['old_fences'][path] for path in paths)):
                raise CutoverError('stale_cutover_approval')
        for path in paths:a.stopped(plan['after_entries'][a.path_digest(Path(path))]['claim'],value['claims'])
        verify_approval_evidence(plan)
        proposed=dict(value['claims']);proposed.update(plan['after_entries']);a.validate_claims(proposed);a.check_capacity(p,info,proposed)
        for path in paths:
            directory=Path(path)
            if not a.exists(directory/PENDING):a.write(directory/PENDING,{'version':VERSION,'plan':plan})
        if optional(source/FENCE)!=source_fence(plan):a.write(source/FENCE,source_fence(plan))
        for path in paths:
            if environment(Path(path))[0]!=plan['new_envs'][path]:r.write_environment(Path(path)/'.env',plan['new_envs'][path])
        if not switched:
            value=dict(value,claims=proposed);write_ledger(Path(p['state_directory'])/'ledger.json',value)
        for path in paths:
            a.write(Path(path)/a.MARKER,plan['new_markers'][path])
        value=a.ledger(plan['binding'])
        for path in paths:
            if (environment(Path(path))[0]!=plan['new_envs'][path] or a.read_marker(Path(path))!=plan['new_markers'][path]
                    or value['claims'].get(a.path_digest(Path(path)))!=plan['after_entries'][a.path_digest(Path(path))]):raise CutoverError('durable_cutover_target_unverified')
        for path in paths:a.write(Path(path)/RECEIPT,done_receipts[path])
        verify_final(plan,value)
        remove(destination/a.REQUIRED);remove(destination/FENCE)
        verify_final(plan,a.ledger(plan['binding']))
        for path in paths:remove(Path(path)/PENDING)
        return summary(plan,'COMPLETE')


resume=apply

def rollback_preview(source,destination,plan_path):return preview(source,destination,plan_path,'rollback')
rollback_apply=apply
rollback_resume=apply


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['preview','rollback-preview','apply','resume'])
    parser.add_argument('--source',type=Path,required=True);parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True);args=parser.parse_args()
    try:
        fn={'preview':preview,'rollback-preview':rollback_preview,'apply':apply,'resume':resume}[args.operation]
        print(json.dumps(fn(args.source,args.destination,args.plan),sort_keys=True,indent=2))
    except CutoverError as exc:parser.exit(1,f'Cutover refused or incomplete: {exc.code}\n')


if __name__=='__main__':main()
