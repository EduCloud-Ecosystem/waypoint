#!/usr/bin/env python3
"""Retire a disposable course, export every retained work directory, restore closed."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from notebook import Notebook, SUBJECTS, ROOT
from backup import docker, digest
from deployment import preflight, compose, read_environment
from homes import inspect, run as xfs_run
from recovery import load_config, restore_repository, restic, RETIRED_TAG
from verify import port
from verify_institutional_recovery import cleanup_rehearsal
from evidence import _source, sha256_file
from maintenance import digest as record_digest
import maintenance
import upgrade
import retirement
import retirement_export as exports

CHECKS=frozenset({'python-r-saved-coursework','removed-learner-home-retained',
    'protected-pre-retirement-backup','protected-package-v3','legacy-restore-refused','changed-environment-refused','changed-backup-config-refused',
    'changed-saved-file-refused','changed-plan-refused','sealed-interruption-resumed',
    'pending-retirement-blocks-start','retired-lifecycle-refused','old-session-offline',
    'all-retained-work-exported','outside-work-auth-state-excluded','independent-export-verification',
    'corrupt-export-refused','missing-export-refused','traversal-export-refused',
    'source-work-preserved','protected-snapshot-retained','snapshot-integrity',
    'fresh-namespace-restored-closed','restored-start-refused','restored-work-exported'})
HASH=re.compile(r'[0-9a-f]{64}\Z')


def refused(operation, error=ValueError):
    try: operation()
    except error: return
    raise AssertionError('retired, stale, or corrupt state was accepted')


def publish_pass(root, report, cleanup_verified):
    keys={'waypoint_revision','fixture_sha256','exporter_sha256','source_manifest_sha256',
          'snapshot_id','source_export_sha256','restored_export_sha256','images','counts','checks'}
    if cleanup_verified is not True or not isinstance(report,dict) or set(report)!=keys:
        raise ValueError('retirement success requires complete bounded evidence and verified cleanup')
    if (not isinstance(report['waypoint_revision'],str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',report['waypoint_revision'])
            or any(not isinstance(report[k],str) or not HASH.fullmatch(report[k]) for k in
                   ('fixture_sha256','exporter_sha256','source_manifest_sha256','snapshot_id','source_export_sha256','restored_export_sha256'))
            or not isinstance(report['images'],dict) or set(report['images'])!={'hub','course'}
            or any(not isinstance(v,str) or not exports.IMAGE.fullmatch(v) for v in report['images'].values())
            or not isinstance(report['counts'],dict) or set(report['counts'])!={'retained_homes','work_files','work_bytes','outside_work_entries'}
            or any(type(v) is not int or not 0<=v<=exports.MAX_TOTAL_BYTES for v in report['counts'].values())
            or report['counts']['retained_homes']!=2 or report['counts']['work_files']<4
            or not isinstance(report['checks'],list) or set(report['checks'])!=CHECKS or len(report['checks'])!=len(CHECKS)):
        raise ValueError('retirement evidence schema or completed controls invalid')
    data=(json.dumps(dict(report,version=1,status='PASS',scope='same-worker-synthetic-course-retirement',
                         export_scope=exports.SCOPE,cleanup_verified=True),sort_keys=True,indent=2,allow_nan=False)+'\n').encode()
    if len(data)>64*1024: raise ValueError('retirement evidence budget exceeded')
    root=Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode&0o077:
        raise ValueError('retirement evidence directory must be private')
    fd,temporary=tempfile.mkstemp(prefix='.retirement-evidence-',dir=root)
    try:
        with os.fdopen(fd,'wb') as out:
            os.fchmod(out.fileno(),0o600); out.write(data); out.flush(); os.fsync(out.fileno())
        os.link(temporary,root/'RETIREMENT-PASS.json')
    finally: os.unlink(temporary)


def verify_work(export_directory, expected):
    result=exports.verify_export(export_directory)
    assert result['retained_homes']==2 and result['work_files']>=4
    manifest=json.loads((export_directory/'manifest.json').read_text())
    for user,files in expected.items():
        suffix='home-'+hashlib.sha256(SUBJECTS[user].encode()).hexdigest()[:24]
        item=next(home for home in manifest['homes'] if home['suffix']==suffix)
        assert item['work_present'] is True
        with tarfile.open(export_directory/(suffix+'.tar.gz'),'r:gz') as archive:
            for name,content in files.items():
                member=archive.extractfile('work/'+name)
                assert member is not None and member.read()==content.encode(), 'retained coursework differed in portable export'
            assert all(member.name=='work' or member.name.startswith('work/') for member in archive.getmembers())
    return result


def verify_legacy_refusal(cfg, snapshot_id, root):
    """The pinned previously shipped validator must refuse protected v3 bundles."""
    baseline='fd5b9956df827bdb9fe667be9de53814339b5fd8'
    source=subprocess.check_output(['git','-c','safe.directory='+str(ROOT.parent),'-C',str(ROOT.parent),
                                    'show',baseline+':course-workspace/recovery.py'],text=True)
    namespace={'__file__':str(ROOT/'recovery.py'),'__name__':'legacy_recovery_reference'}
    exec(compile(source,'trusted-legacy-recovery-reference','exec'),namespace)
    with tempfile.TemporaryDirectory(prefix='.legacy-proof-',dir=root) as scratch:
        restic(cfg,'restore',snapshot_id,'--target',scratch,'--verify')
        bundle=Path(scratch)/'bundle'
        assert json.loads((bundle/'bundle.json').read_text())['version']==3, 'protected package did not upgrade its compatibility boundary'
        try: namespace['validate_bundle'](bundle)
        except ValueError as exc:
            assert 'unsupported recovery bundle' in str(exc)
        else: raise AssertionError('legacy recovery accepted a protected retired package')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    if not __debug__: parser.error('optimized Python cannot enforce retirement acceptance assertions')
    parser.add_argument('--output',type=Path,required=True); args=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0:
        parser.error('use root on a disposable Linux runner; only invocation-owned loop images are formatted')
    source_state=_source(ROOT.parent)
    assert source_state['dirty'] is False, 'retirement acceptance needs clean source'
    fixture_hash=sha256_file(__file__); exporter_hash=sha256_file(ROOT/'retirement_export.py')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False); root=args.output.resolve()
    source=root/'source'; source.mkdir(mode=0o700)
    fixture=Notebook(source); source_instance=fixture.instance; recovered=None; checks=[]
    review_after=(datetime.now(timezone.utc)+timedelta(days=30)).date().isoformat()
    try:
        fixture.start()
        images={'hub':fixture.hub_image,'course':fixture.image}
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                contexts=[]; sessions={}; expected={}
                for user in ('alice','bob'):
                    context=browser.new_context(ignore_https_errors=True); contexts.append(context); page=context.new_page()
                    fixture.login(page,user); session,url=fixture.ready(page,user); sessions[user]=(session,url,page)
                    expected[user]={'retirement.py':user+' saved Python coursework\n', 'retirement-r.txt':user+' saved R coursework\n'}
                    fixture.kernel(session,url,'python3','from pathlib import Path\nPath("retirement.py").write_text('+repr(expected[user]['retirement.py'])+')\nprint("COURSE_OK")')
                    fixture.kernel(session,url,'ir','writeLines("'+user+' saved R coursework", "retirement-r.txt"); cat("COURSE_OK\\n")')
                checks.append('python-r-saved-coursework')
                fixture.compose('stop','hub')
                # Current approved roster removes Alice; her verified home remains.
                fixture.env['WORKSPACE_ALLOWED_SUBJECTS']=SUBJECTS['bob']; fixture.write_env()
                inspect(fixture.homes,fixture.instance)
                home=fixture.instance+'-home-'+hashlib.sha256(SUBJECTS['alice'].encode()).hexdigest()[:24]
                saved=fixture.homes/home/'work/retirement.py'
                assert saved.read_text()==expected['alice']['retirement.py']
                checks.append('removed-learner-home-retained')
                recovery_path=fixture.deployment/'recovery.json'; cfg=load_config(recovery_path); restic(cfg,'init')
                assert cfg['local_test'] is True and Path(cfg['repository']).is_relative_to(root)
                stale_path=root/'stale-retirement-plan.json'
                retirement.preview(fixture.deployment,recovery_path,stale_path,'Synthetic retention operator',review_after)
                controls=[('changed-environment-refused',fixture.env_file,
                           fixture.env_file.read_bytes().replace(b'WORKSPACE_IDLE_SECONDS=1800',b'WORKSPACE_IDLE_SECONDS=1801')),
                          ('changed-backup-config-refused',recovery_path,recovery_path.read_bytes()+b'\n'),
                          ('changed-plan-refused',stale_path,b'{}')]
                for check,path,changed in controls:
                    original=path.read_bytes()
                    try:
                        path.write_bytes(changed)
                        refused(lambda:retirement.apply(fixture.deployment,stale_path,recovery_path),retirement.RetirementError)
                        assert not (fixture.deployment/retirement.MARKER).exists()
                    finally: path.write_bytes(original)
                    checks.append(check)
                saved.write_text('changed after review\n')
                refused(lambda:retirement.apply(fixture.deployment,stale_path,recovery_path),retirement.RetirementError)
                saved.write_text(expected['alice']['retirement.py']); checks.append('changed-saved-file-refused')
                plan_path=root/'retirement-plan.json'
                plan=retirement.preview(fixture.deployment,recovery_path,plan_path,'Synthetic retention operator',review_after)
                rows=json.loads(restic(cfg,'snapshots','--json',plan['snapshot_id']))
                assert RETIRED_TAG in rows[0]['tags']; checks.append('protected-pre-retirement-backup')
                verify_legacy_refusal(cfg,plan['snapshot_id'],root)
                checks += ['protected-package-v3','legacy-restore-refused']
                real_write=retirement.write
                def interrupted(path,*args,**kwargs):
                    result=real_write(path,*args,**kwargs)
                    if Path(path)==fixture.deployment/retirement.MARKER:
                        raise RuntimeError('synthetic interruption after durable retired seal')
                    return result
                with patch('retirement.write',side_effect=interrupted):
                    refused(lambda:retirement.apply(fixture.deployment,plan_path,recovery_path),retirement.RetirementError)
                assert (fixture.deployment/retirement.MARKER).is_file() and (fixture.deployment/retirement.PENDING).is_file()
                refused(lambda:preflight(fixture.deployment))
                refused(lambda:exports.export_course(fixture.deployment,root/'premature-export'),exports.ExportError)
                checks.append('pending-retirement-blocks-start')
                receipt=retirement.apply(fixture.deployment,plan_path,recovery_path)
                assert receipt['status']=='RETIRED' and receipt['deployment_started'] is False and receipt['files_deleted'] is False
                assert not (fixture.deployment/retirement.PENDING).exists()
                checks.append('sealed-interruption-resumed')
                refused(lambda:preflight(fixture.deployment))
                refused(lambda:compose(fixture.deployment,'up','-d','--no-build','--pull','never','hub'))
                refused(lambda:maintenance.preview(fixture.deployment,[SUBJECTS['bob']],256,100000,root/'forbidden-maintenance.json'))
                refused(lambda:upgrade.preview(fixture.deployment,images['hub'],images['course'],root/'missing-canary.json',recovery_path,root/'forbidden-upgrade.json'))
                checks.append('retired-lifecycle-refused')
                assert not docker('ps','-q','--filter','label=educloud.workspace.instance='+fixture.instance)
                old=sessions['bob'][0].get(fixture.origin+'/hub/api/users/'+SUBJECTS['bob'],timeout=15,allow_redirects=False)
                assert old.status_code in (502,503), 'retired Hub answered an old session'
                response=sessions['bob'][2].goto(fixture.origin+'/hub/login')
                assert response.status in (502,503)
                checks.append('old-session-offline')
                export_directory=root/'coursework'
                summary=exports.export_course(fixture.deployment,export_directory)
                checked=verify_work(export_directory,expected)
                assert checked['outside_work_entries']>0 and summary['manifest_sha256']==checked['manifest_sha256']
                checks += ['all-retained-work-exported','outside-work-auth-state-excluded','independent-export-verification']
                for kind in ('corrupt','missing','traversal'):
                    destination=root/(kind+'-export'); shutil.copytree(export_directory,destination)
                    manifest=json.loads((destination/'manifest.json').read_text()); item=manifest['homes'][0]
                    archive_path=destination/(item['suffix']+'.tar.gz')
                    if kind=='corrupt': archive_path.write_bytes(b'corrupted')
                    elif kind=='missing': archive_path.unlink()
                    else:
                        with tarfile.open(archive_path,'w:gz') as archive:
                            member=tarfile.TarInfo('../escaped'); member.size=3; archive.addfile(member,io.BytesIO(b'bad'))
                        item['archive_sha256']=digest(archive_path); item['archive_bytes']=archive_path.stat().st_size
                        manifest['digest']=record_digest({k:v for k,v in manifest.items() if k!='digest'})
                        (destination/'manifest.json').write_text(json.dumps(manifest))
                    refused(lambda:exports.verify_export(destination),exports.ExportError)
                    checks.append(kind+'-export-refused')
                assert not (root/'escaped').exists() and saved.read_text()==expected['alice']['retirement.py']
                checks.append('source-work-preserved')
                # Run real retention against this invocation-owned local repository.
                # Only protected snapshots may survive this synthetic policy.
                control=root/'retention-control.txt'; control.write_text('synthetic retention control')
                output=restic(cfg,'backup','--json','--tag','synthetic-unprotected-control',str(control))
                control_id=next(json.loads(line)['snapshot_id'] for line in output.splitlines() if json.loads(line).get('message_type')=='summary')
                restic(cfg,'forget','--keep-tag',RETIRED_TAG)
                snapshots=json.loads(restic(cfg,'snapshots','--json'))
                assert plan['snapshot_id'] in {item['id'] for item in snapshots} and control_id not in {item['id'] for item in snapshots}
                checks.append('protected-snapshot-retained'); restic(cfg,'check','--read-data'); checks.append('snapshot-integrity')
                disk=root/'replacement.img'
                with disk.open('xb') as out: out.truncate(2*1024**3)
                homes=root/'replacement-homes'; homes.mkdir()
                xfs_run('mkfs.xfs','-f',str(disk)); xfs_run('mount','-o','loop,prjquota',str(disk),str(homes))
                destination=root/'restored'
                recovered=restore_repository(cfg,plan['snapshot_id'],destination,port(),homes,fixture.env_file)
                assert recovered['WORKSPACE_INSTANCE']!=source_instance
                ledger=retirement.read_retirement(destination)
                assert ledger['source']=='snapshot_restore' and ledger['status']=='RETIRED'
                assert not docker('ps','-q','--filter','label=educloud.workspace.instance='+recovered['WORKSPACE_INSTANCE'])
                checks.append('fresh-namespace-restored-closed')
                refused(lambda:preflight(destination))
                result=subprocess.run([sys.executable,str(destination/'runtime/deployment.py'),'start','--directory',str(destination)],capture_output=True,text=True)
                assert result.returncode!=0 and 'retired' in result.stderr.lower()
                checks.append('restored-start-refused')
                restored_export=root/'restored-coursework'; exports.export_course(destination,restored_export)
                restored_checked=verify_work(restored_export,expected); checks.append('restored-work-exported')
                assert _source(ROOT.parent)==source_state and sha256_file(__file__)==fixture_hash and sha256_file(ROOT/'retirement_export.py')==exporter_hash
                fixture.verify_manifest_frozen()
                report={'waypoint_revision':source_state['revision'],'fixture_sha256':fixture_hash,'exporter_sha256':exporter_hash,
                    'source_manifest_sha256':fixture.manifest_digest,'snapshot_id':plan['snapshot_id'],
                    'source_export_sha256':checked['manifest_sha256'],'restored_export_sha256':restored_checked['manifest_sha256'],
                    'images':images,'counts':{k:checked[k] for k in ('retained_homes','work_files','work_bytes','outside_work_entries')},'checks':sorted(checks)}
            finally: browser.close()
    finally: cleanup_verified=cleanup_rehearsal(fixture,root,source_instance,recovered)
    publish_pass(root,report,cleanup_verified)
    print('PASS: synthetic course remains retired; retained coursework verified, protected snapshot retained and restored closed; cleanup verified.',flush=True)


if __name__=='__main__': main()
