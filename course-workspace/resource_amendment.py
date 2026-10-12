#!/usr/bin/env python3
"""Review and apply stopped managed-course RAM/CPU/admission limit changes.

Never starts services. An interrupted transaction retains a conservative peak
reservation and blocks target admission until durable state is verified.
"""
import argparse
from contextlib import redirect_stdout
import io
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import uuid
import re

import allocation as a
from deployment import validate_institutional, require_active_course
from pilot import locked

VERSION = 'educloud-resource-amendment-v1'
PENDING = '.resource-amendment-pending.json'
RECEIPT = '.resource-amendment-receipt.json'
MAX_ENV = 256 * 1024
REVISION = 'WORKSPACE_RESOURCE_REVISION'
REVISION_PATTERN = re.compile(r'[0-9a-f]{32}\Z')
MUTABLE = {'WORKSPACE_MEMORY_MB','WORKSPACE_CPU_LIMIT','WORKSPACE_ACTIVE_LIMIT'}
PLAN_FIELDS = {'version','directory_sha256','tool_sha256','old_env','new_env','old_marker','new_marker',
               'old_claim','new_claim','peak_claim','ledger_binding','ledger_sha256','runtime_binding','prior_receipt_sha256','digest'}


class AmendmentError(ValueError):
    def __init__(self, code):
        self.code=code
        super().__init__(code)


def guarded(fn):
    @wraps(fn)
    def run(*args,**kwargs):
        try:
            return fn(*args,**kwargs)
        except AmendmentError:
            raise
        except a.AllocationError as exc:
            raise AmendmentError(exc.code) from None
        except (OSError,ValueError,TypeError,KeyError,RuntimeError,OverflowError,
                UnicodeError,RecursionError,subprocess.SubprocessError):
            raise AmendmentError('resource_amendment_refused_or_incomplete') from None
    return run


def tool_hash():
    root=Path(__file__).resolve().parent
    return a.digest({name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
                     ('resource_amendment.py','allocation.py','deployment.py','pilot.py','homes.py','hub/settings.py')})


def read_env(directory):
    if a.exists(directory/'pilot.env'):
        raise AmendmentError('institutional_deployment_required')
    path=directory/'.env'
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        value=os.fstat(source.fileno())
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != a.ROOT_UID or value.st_mode & 0o077
                or value.st_size > MAX_ENV):
            raise AmendmentError('private_environment_required')
        raw=source.read(MAX_ENV+1)
    if len(raw)>MAX_ENV:
        raise AmendmentError('private_environment_required')
    text=raw.decode('utf-8')
    return text,parse_env(directory,text)


def parse_env(directory,text):
    import re
    if not isinstance(text,str) or len(text.encode())>MAX_ENV:
        raise AmendmentError('invalid_environment_binding')
    env={}
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        key,separator,value=line.partition('=')
        if (not separator or not re.fullmatch(r'WORKSPACE_[A-Z_]+',key) or key in env
                or any(c.isspace() for c in value) or any(c in value for c in ('$','"',"'",'#','\\','\x00'))):
            raise AmendmentError('invalid_environment_binding')
        env[key]=value
    validate_institutional(env)
    if REVISION in env and not REVISION_PATTERN.fullmatch(env[REVISION]):
        raise AmendmentError('invalid_resource_revision')
    if env.get('WORKSPACE_ENV_FILE')!=str(directory/'.env'):
        raise AmendmentError('environment_directory_mismatch')
    return env


def validate_limits(memory_mb,cpu,active_limit):
    if (type(memory_mb) is not int or not 128<=memory_mb<=1048576 or type(active_limit) is not int or not 1<=active_limit<=1000
            or not a.number(cpu,1024) or cpu<0.1):
        raise AmendmentError('invalid_resource_limits')


def env_target(raw,memory_mb,cpu,active_limit):
    validate_limits(memory_mb,cpu,active_limit)
    values={'WORKSPACE_MEMORY_MB':str(memory_mb),'WORKSPACE_CPU_LIMIT':format(cpu,'.17g'),
            'WORKSPACE_ACTIVE_LIMIT':str(active_limit),REVISION:uuid.uuid4().hex}
    if any(line.partition('=')[0]==REVISION and line.partition('=')[2]==values[REVISION] for line in raw.splitlines()):
        raise AmendmentError('fresh_resource_revision_required')
    lines=[];seen=set()
    for line in raw.splitlines():
        key=line.partition('=')[0]
        if key in MUTABLE|{REVISION}:
            line=key+'='+values[key];seen.add(key)
        lines.append(line)
    lines.extend(key+'='+values[key] for key in sorted((MUTABLE|{REVISION})-seen))
    return '\n'.join(lines)+'\n'


def write_environment(path,text):
    directory=path.parent
    # JSON record writer intentionally refuses literal .env data; use the same
    # no-follow/private checks and atomic/fsync publication with bounded bytes.
    read_env(directory)
    raw=text.encode('utf-8')
    if len(raw)>MAX_ENV:
        raise AmendmentError('private_environment_required')
    fd,name=tempfile.mkstemp(prefix='.resource-env-',dir=directory)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(raw);out.flush();os.fsync(out.fileno())
        os.replace(name,directory/'.env');a.sync(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def publish_plan(path,plan):
    raw=(json.dumps(plan,sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    if len(raw)>a.MAX_JSON:
        raise AmendmentError('resource_plan_limit_exceeded')
    fd,name=tempfile.mkstemp(prefix='.resource-plan-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as out:
            out.write(raw);out.flush();os.fsync(out.fileno())
        try:
            os.link(name,path)
        except FileExistsError:
            raise AmendmentError('new_private_resource_plan_required') from None
        a.sync(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def no_other_pending(directory,root):
    require_active_course(directory)
    for path in (directory/a.REQUIRED,directory/'.maintenance-pending.json',directory/'.upgrade-pending.json',
                 directory/'.retirement-pending.json',Path(root)/'.educloud-maintenance.json'):
        if a.exists(path):
            raise AmendmentError('unfinished_course_operation')


def runtime_binding(directory,env):
    from deployment import docker_json
    cfg=validate_institutional(env)
    info,_=a.daemon()
    if not isinstance(info.get('Architecture'),str):
        raise AmendmentError('worker_architecture_binding_invalid')
    architecture={'x86_64':'amd64','aarch64':'arm64'}.get(info['Architecture'],info['Architecture'])
    images={}
    for role,reference in [('hub',cfg['hub_image']),('course',cfg['image'])]:
        rows=docker_json('image','inspect',reference)
        if (not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict)
                or rows[0].get('Os')!='linux' or rows[0].get('Architecture')!=architecture
                or not isinstance(rows[0].get('Id'),str)
                or not rows[0]['Id'].startswith('sha256:') or not a.HASH.fullmatch(rows[0]['Id'][7:])):
            raise AmendmentError('runtime_image_binding_invalid')
        images[role]=rows[0]['Id']
    setup={}
    for name in ('nginx.conf','keycloak-client.json','cairn-binding.json','source-manifest.json',
                 'manifest.json','recovery.json','health.json','.maintenance-identity.json'):
        path=directory/name
        if not a.exists(path):
            setup[name]=None
            continue
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(fd,'rb') as source:
            value=os.fstat(source.fileno())
            if (not stat.S_ISREG(value.st_mode) or value.st_uid!=a.ROOT_UID or value.st_mode&0o077
                    or value.st_size>1024*1024):
                raise AmendmentError('private_setup_binding_required')
            raw=source.read(1024*1024+1)
        if len(raw)>1024*1024:
            raise AmendmentError('private_setup_binding_required')
        setup[name]=hashlib.sha256(raw).hexdigest()
    from homes import inspect
    with redirect_stdout(io.StringIO()):
        observed=inspect(Path(cfg['home_root']),cfg['instance'])
    if not isinstance(observed,dict) or not isinstance(observed.get('volumes'),dict) or len(observed['volumes'])>10000:
        raise AmendmentError('retained_home_binding_invalid')
    expected={cfg['instance']+'-home-'+hashlib.sha256(subject.encode()).hexdigest()[:24] for subject in cfg['subjects']}
    if not expected.issubset(observed['volumes']):
        raise AmendmentError('retained_home_binding_invalid')
    homes={}
    for name,value in observed['volumes'].items():
        if (not isinstance(name,str) or not isinstance(value,dict)
                or value.get('quota_bytes')!=cfg['home_quota_mb']*1024**2 or value.get('inode_limit')!=cfg['home_inode_limit']
                or type(value.get('project_id')) is not int or value['project_id']<=0):
            raise AmendmentError('retained_home_binding_invalid')
        homes[name]={key:value[key] for key in ('quota_bytes','inode_limit','project_id')}
    return {'images':images,'worker_architecture':architecture,'private_setup_sha256':setup,'retained_homes_sha256':a.digest(homes)}


def entry(claim):
    return {'status':'ACTIVE','claim':claim,'claim_sha256':a.digest(claim)}


def peak_claim(old,new):
    value=dict(old)
    for key in ('active_limit','learner_memory_mb','learner_cpu'):
        value[key]=max(old[key],new[key])
    value['memory_mb']=1024+value['active_limit']*value['learner_memory_mb']
    value['cpu']=1+value['active_limit']*value['learner_cpu']
    return value


def profile(claim):
    return {key:claim[key] for key in ('active_limit','learner_memory_mb','learner_cpu','memory_mb','cpu')}


def summary(plan,status):
    return {'version':VERSION,'status':status,'scope':'stopped-course-resource-amendment',
            'plan_sha256':plan['digest'],'old':profile(plan['old_claim']),'new':profile(plan['new_claim']),
            'peak':profile(plan['peak_claim']),'deployment_started':False}


def plan_record(directory,plan_path):
    plan=a.read(plan_path)
    if (set(plan)!=PLAN_FIELDS or plan['version']!=VERSION or plan['directory_sha256']!=a.path_digest(directory)
            or plan['tool_sha256']!=tool_hash() or not isinstance(plan['ledger_binding'],dict)
            or set(plan['ledger_binding'])!=a.BINDING_FIELDS
            or not isinstance(plan['ledger_sha256'],str) or not a.HASH.fullmatch(plan['ledger_sha256'])
            or plan['prior_receipt_sha256'] is not None and (not isinstance(plan['prior_receipt_sha256'],str) or not a.HASH.fullmatch(plan['prior_receipt_sha256']))
            or plan['digest']!=a.digest({key:value for key,value in plan.items() if key!='digest'})):
        raise AmendmentError('invalid_or_stale_resource_plan')
    old_env=parse_env(directory,plan['old_env']);new_env=parse_env(directory,plan['new_env'])
    if any(old_env.get(key)!=new_env.get(key) for key in set(old_env)|set(new_env) if key not in MUTABLE|{REVISION}):
        raise AmendmentError('resource_only_change_required')
    if REVISION not in new_env or new_env[REVISION]==old_env.get(REVISION):
        raise AmendmentError('fresh_resource_revision_required')
    if plan['runtime_binding']!=runtime_binding(directory,old_env):
        raise AmendmentError('runtime_setup_binding_changed')
    old,new=a.claim(directory,old_env),a.claim(directory,new_env)
    validate_limits(new['learner_memory_mb'],new['learner_cpu'],new['active_limit'])
    peak=peak_claim(old,new)
    if (not a.valid_claim(peak) or plan['old_claim']!=old or plan['new_claim']!=new or plan['peak_claim']!=peak
            or old==new):
        raise AmendmentError('resource_claim_binding_changed')
    mutable_claim={'active_limit','learner_memory_mb','learner_cpu','memory_mb','cpu'}
    if any(old[key]!=new[key] for key in a.CLAIM_FIELDS-mutable_claim):
        raise AmendmentError('resource_only_change_required')
    binding=plan['ledger_binding']
    p,ph=a.policy(binding['config_file']);info,dh=a.daemon()
    if binding!=a.binding(binding['config_file'],p,ph,dh):
        raise AmendmentError('worker_binding_changed')
    if (plan['old_marker']!=a.expected_marker(directory,binding['config_file'],p,ph,dh,old)
            or plan['new_marker']!=a.expected_marker(directory,binding['config_file'],p,ph,dh,new)):
        raise AmendmentError('resource_marker_binding_changed')
    return plan,p,info


def validate_receipt(record):
    fields={'version','status','plan_sha256','directory_sha256','claim_sha256','env_sha256','old','new','peak','revision','digest'}
    if (not isinstance(record,dict) or set(record)!=fields or record['version']!=VERSION or record['status']!='COMPLETE'
            or any(not isinstance(record[key],str) or not a.HASH.fullmatch(record[key])
                   for key in ('plan_sha256','directory_sha256','claim_sha256','env_sha256','digest'))
            or not isinstance(record['revision'],str) or not REVISION_PATTERN.fullmatch(record['revision'])
            or record['digest']!=a.digest({key:value for key,value in record.items() if key!='digest'})):
        raise AmendmentError('invalid_resource_amendment_receipt')
    for name in ('old','new','peak'):
        value=record[name]
        if (not isinstance(value,dict) or set(value)!={'active_limit','learner_memory_mb','learner_cpu','memory_mb','cpu'}
                or any(type(value[key]) is not int or not 1<=value[key]<=10**18 for key in ('active_limit','learner_memory_mb','memory_mb'))
                or not a.number(value['learner_cpu'],100000) or not a.number(value['cpu'],100000)
                or value['memory_mb']!=1024+value['active_limit']*value['learner_memory_mb']
                or value['cpu']!=1+value['active_limit']*value['learner_cpu']):
            raise AmendmentError('invalid_resource_amendment_receipt')
    if any(record['peak'][key]!=max(record['old'][key],record['new'][key]) for key in ('active_limit','learner_memory_mb','learner_cpu')):
        raise AmendmentError('invalid_resource_amendment_receipt')
    validate_limits(record['new']['learner_memory_mb'],record['new']['learner_cpu'],record['new']['active_limit'])
    return record


def receipt_record(plan):
    value={'version':VERSION,'status':'COMPLETE','plan_sha256':plan['digest'],
           'directory_sha256':plan['directory_sha256'],'claim_sha256':a.digest(plan['new_claim']),
           'env_sha256':hashlib.sha256(plan['new_env'].encode()).hexdigest(),
           'old':profile(plan['old_claim']),'new':profile(plan['new_claim']),'peak':profile(plan['peak_claim']),
           'revision':parse_env(Path(plan['old_claim']['directory']),plan['new_env'])[REVISION]}
    value['digest']=a.digest(value)
    return validate_receipt(value)


def completed_receipt(directory,pending_plan=None):
    """Read live history; only an exact pending plan may precede its receipt."""
    raw,env=read_env(directory)
    path=directory/RECEIPT
    record=validate_receipt(a.read(path)) if a.exists(path) else None
    if record is not None and record['directory_sha256']!=a.path_digest(directory):
        raise AmendmentError('resource_receipt_directory_mismatch')
    pending_authorized=False
    if pending_plan is not None:
        pending=directory/PENDING
        if (not a.exists(pending) or a.read(pending)!={'version':VERSION,'plan':pending_plan}
                or pending_plan.get('version')!=VERSION or pending_plan.get('directory_sha256')!=a.path_digest(directory)
                or pending_plan.get('tool_sha256')!=tool_hash()
                or pending_plan.get('digest')!=a.digest({key:value for key,value in pending_plan.items() if key!='digest'})
                or raw not in (pending_plan['old_env'],pending_plan['new_env'])):
            raise AmendmentError('resource_resume_state_changed')
        pending_authorized=True
    revision=env.get(REVISION)
    if not pending_authorized:
        if revision is not None and record is None:
            raise AmendmentError('completed_resource_history_required')
        if record is not None and record['revision']!=revision:
            raise AmendmentError('resource_revision_history_mismatch')
    return record


def prior_receipt_matches(plan,record):
    return (a.digest(record) if record is not None else None)==plan['prior_receipt_sha256']


def verify_target(directory,plan,value):
    raw,_=read_env(directory)
    if (raw!=plan['new_env'] or a.read_marker(directory)!=plan['new_marker']
            or value['claims'].get(a.path_digest(directory))!=entry(plan['new_claim'])):
        raise AmendmentError('durable_resource_target_unverified')


@guarded
def preview(directory,plan_path,memory_mb,cpu,active_limit):
    from deployment import _inspect_course
    directory,plan_path=a.canonical(directory),a.canonical(plan_path)
    a.root_required();a.private_directory(directory);a.private_directory(plan_path.parent)
    if a.exists(plan_path) or not plan_path.name.endswith('.json'):
        raise AmendmentError('new_private_resource_plan_required')
    with locked(directory),a.worker_lock():
        if a.exists(plan_path):
            raise AmendmentError('new_private_resource_plan_required')
        raw,env=read_env(directory)
        no_other_pending(directory,env['WORKSPACE_HOME_ROOT'])
        if a.exists(directory/PENDING):
            raise AmendmentError('unfinished_resource_amendment')
        selected=a.current(directory,env)
        if selected is None:
            raise AmendmentError('active_managed_course_required')
        p,value,old=selected
        historical=completed_receipt(directory)
        if historical is not None and historical['claim_sha256']!=a.digest(old):
            raise AmendmentError('resource_receipt_claim_mismatch')
        protected_paths=[directory,Path(old['home_root']),Path(p['state_directory']),a.AUTHORITY]
        protected_paths.extend(Path(e['claim'][key]) for e in value['claims'].values() for key in ('directory','home_root'))
        for protected in protected_paths:
            if a.overlap(str(plan_path),str(protected)):
                raise AmendmentError('resource_plan_overlaps_retained_state')
        if plan_path==Path(value['config_file']):
            raise AmendmentError('resource_plan_overlaps_worker_policy')
        _inspect_course(directory)
        a.stopped(old,value['claims'])
        new_raw=env_target(raw,memory_mb,cpu,active_limit)
        new=a.claim(directory,parse_env(directory,new_raw))
        if old==new:
            raise AmendmentError('resource_change_required')
        peak=peak_claim(old,new)
        temporary=dict(value['claims']);temporary[a.path_digest(directory)]=entry(peak)
        info,_=a.daemon();a.check_capacity(p,info,temporary)
        marker=a.read_marker(directory)
        plan={'version':VERSION,'directory_sha256':a.path_digest(directory),'tool_sha256':tool_hash(),
              'old_env':raw,'new_env':new_raw,'old_marker':marker,
              'new_marker':dict(marker,claim_sha256=a.digest(new)),
              'old_claim':old,'new_claim':new,'peak_claim':peak,
              'ledger_binding':{key:value[key] for key in a.BINDING_FIELDS},'ledger_sha256':a.digest(value),
              'runtime_binding':runtime_binding(directory,env),
              'prior_receipt_sha256':a.digest(historical) if historical is not None else None}
        plan['digest']=a.digest(plan)
        publish_plan(plan_path,plan)
        return summary(plan,'PLAN_READY')


@guarded
def apply(directory,plan_path):
    directory,plan_path=a.canonical(directory),a.canonical(plan_path)
    a.root_required();a.private_directory(directory)
    with locked(directory),a.worker_lock():
        plan,p,info=plan_record(directory,plan_path)
        no_other_pending(directory,plan['old_claim']['home_root'])
        key=a.path_digest(directory);pending=directory/PENDING
        value=a.ledger(plan['ledger_binding'])
        historical=completed_receipt(directory,plan if a.exists(pending) else None)
        if not a.exists(pending) and historical is not None:
            if historical==receipt_record(plan):
                verify_target(directory,plan,value);a.check_capacity(p,info,value['claims'])
                a.stopped(plan['new_claim'],value['claims'])
                return summary(plan,'COMPLETE')
        raw,_=read_env(directory);marker=a.read_marker(directory)
        target=value['claims'].get(key)
        states=[(entry(plan['old_claim']),plan['old_env'],plan['old_marker']),
                (entry(plan['peak_claim']),plan['old_env'],plan['old_marker']),
                (entry(plan['peak_claim']),plan['new_env'],plan['old_marker']),
                (entry(plan['peak_claim']),plan['new_env'],plan['new_marker']),
                (entry(plan['new_claim']),plan['new_env'],plan['new_marker'])]
        actual=(target,raw,marker)
        if a.exists(pending):
            if a.read(pending)!= {'version':VERSION,'plan':plan} or actual not in states:
                raise AmendmentError('resource_resume_state_changed')
            if not prior_receipt_matches(plan,historical):
                if historical!=receipt_record(plan) or actual!=states[-1]:
                    raise AmendmentError('resource_receipt_history_changed')
        else:
            if not prior_receipt_matches(plan,historical):
                raise AmendmentError('resource_receipt_history_changed')
            if actual!=states[0] or a.digest(value)!=plan['ledger_sha256']:
                raise AmendmentError('stale_resource_approval')
            a.check_capacity(p,info,value['claims'])
            proposed_peak=dict(value['claims']);proposed_peak[key]=entry(plan['peak_claim'])
            a.check_capacity(p,info,proposed_peak)
            a.stopped(plan['old_claim'],value['claims'])
            a.write(pending,{'version':VERSION,'plan':plan})
        a.stopped(plan['old_claim'],value['claims'])
        a.check_capacity(p,info,value['claims'])
        final=actual==states[-1]
        if not final:
            peak=dict(value['claims']);peak[key]=entry(plan['peak_claim'])
            a.check_capacity(p,info,peak)
            if target!=entry(plan['peak_claim']):
                value=dict(value,claims=peak);a.write(Path(p['state_directory'])/'ledger.json',value)
            if raw!=plan['new_env']:
                write_environment(directory/'.env',plan['new_env'])
            if marker!=plan['new_marker']:
                a.write(directory/a.MARKER,plan['new_marker'])
            # Verify new environment+enrollment against the retained peak lease
            # before releasing any reservation to the target profile.
            observed=a.ledger(plan['ledger_binding']);observed_raw,_=read_env(directory)
            if (observed['claims'].get(key)!=entry(plan['peak_claim']) or observed_raw!=plan['new_env']
                    or a.read_marker(directory)!=plan['new_marker']):
                raise AmendmentError('durable_peak_target_unverified')
            claims=dict(observed['claims']);claims[key]=entry(plan['new_claim'])
            a.check_capacity(p,info,claims)
            value=dict(observed,claims=claims);a.write(Path(p['state_directory'])/'ledger.json',value)
        value=a.ledger(plan['ledger_binding']);verify_target(directory,plan,value)
        a.check_capacity(p,info,value['claims'])
        a.write(directory/RECEIPT,receipt_record(plan))
        if a.read(directory/RECEIPT)!=receipt_record(plan):
            raise AmendmentError('durable_resource_receipt_unverified')
        pending.unlink();a.sync(directory)
        return summary(plan,'COMPLETE')


resume=apply


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['preview','apply','resume'])
    parser.add_argument('--directory',type=Path,required=True)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--memory-mb',type=int)
    parser.add_argument('--cpu',type=float)
    parser.add_argument('--active-limit',type=int)
    args=parser.parse_args()
    try:
        if args.operation=='preview':
            value=preview(args.directory,args.plan,args.memory_mb,args.cpu,args.active_limit)
        else:
            value=apply(args.directory,args.plan)
        print(json.dumps(value,sort_keys=True,indent=2))
    except AmendmentError as exc:
        parser.exit(1,f'Resource amendment refused or incomplete: {exc.code}\n')


if __name__=='__main__':main()
