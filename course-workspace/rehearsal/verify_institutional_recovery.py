#!/usr/bin/env python3
"""Recover synthetic OIDC coursework after destroying source Hub/volumes/mount."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import re
import tempfile
import sys
import time

import requests

from playwright.sync_api import sync_playwright
from notebook import Notebook, SUBJECTS, ROOT
from backup import docker, digest
from deployment import compose, wait_ready, read_environment
from homes import inspect, provision, run as xfs_run
from recovery import backup_repository, restore_repository, restic, load_config
from verify import cleanup, port
from cairn_journey import run


def wait_proxy(fixture):
    for _ in range(60):
        try:
            if fixture.session.get(fixture.origin+'/hub/login',timeout=2).status_code==200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise AssertionError('replacement TLS proxy did not become ready')



def browser_after_cutover(playwright, browser, old_session, origin):
    """Keep old auth refusal, then launch after disposable host bridges changed.

    Docker creates the replacement bridge and attaches the restored Hub while
    the first Chromium process is alive. Chromium's network-change notification can abort
    its first subsequent navigation even after the proxy is ready. A fresh
    process starts with the settled network; it receives no source cookies.
    The independent old-cookie HTTP session remains the authorization control.
    """
    old = old_session.get(origin+'/hub/api/users/'+SUBJECTS['bob'],
                          timeout=15, allow_redirects=False)
    assert old.status_code in (401,403), f'old session accepted: {old.status_code}'
    browser.close()
    return playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])


def cleanup_rehearsal(fixture, root, source_instance, recovered):
    """Keep original and replacement resource/mount ownership separate."""
    failures=[]
    root=Path(root)
    source_homes=root/'source/homes'
    replacement_homes=root/'replacement-homes'

    def attempt(action):
        try:
            return action()
        except Exception as exc:
            failures.append(type(exc).__name__)
            return None

    # restore_repository writes its private env before creating volumes. Recover
    # that invocation-owned identity even if restore raised before returning it.
    if recovered is None and (root/'restored/.env').exists():
        recovered=attempt(lambda: read_environment(root/'restored/.env'))
    if recovered:
        instance=recovered.get('WORKSPACE_INSTANCE','')
        if not re.fullmatch(r'recovered-[0-9a-f]{12}',instance) or instance==source_instance:
            failures.append('invalid-replacement-ownership')
        else:
            replacement=copy.copy(fixture)
            replacement.instance=instance
            replacement.deployment=root/'restored'
            replacement.env_file=replacement.deployment/'.env'
            replacement.homes=replacement_homes
            replacement.mounted=os.path.ismount(replacement_homes)
            # The shared IdP/client belongs to the original fixture, not the
            # restored workspace. Only the original view closes that session.
            if hasattr(replacement,'session'):
                del replacement.session
            result=attempt(replacement.close)
            if result is not True or not replacement.cleanup_verified:
                failures.append('replacement-cleanup-unverified')
    fixture.instance=source_instance
    fixture.homes=source_homes
    fixture.mounted=os.path.ismount(source_homes)
    result=attempt(fixture.close)
    if result is not True or not fixture.cleanup_verified:
        failures.append('source-cleanup-unverified')
    # A close failure may have happened before unmount. Attempt both remaining
    # mounts, but never unmount twice using a stale Boolean from earlier phases.
    for homes in (replacement_homes,source_homes):
        if os.path.ismount(homes):
            attempt(lambda homes=homes: subprocess.run(['umount',str(homes)],
                    capture_output=True,text=True,timeout=30,check=True))
        if os.path.ismount(homes):
            failures.append('owned-filesystem-still-mounted')
    if failures:
        raise RuntimeError('institutional recovery cleanup unverified: '+', '.join(sorted(set(failures))))
    return True


def publish_pass(root, report, cleanup_verified):
    """Publish private bounded evidence only after verified cleanup."""
    if cleanup_verified is not True:
        raise ValueError('cleanup must be verified before institutional PASS')
    data=(json.dumps(dict(report,status='PASS',scope='same-worker-institutional-recovery',
                          cleanup_verified=True),indent=2,allow_nan=False)+'\n').encode()
    if len(data)>64*1024:
        raise ValueError('institutional evidence exceeds output bound')
    root=Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('evidence directory must be private')
    fd,temporary=tempfile.mkstemp(prefix='.institutional-evidence-',dir=root)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchmod(output.fileno(),0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary,root/'PASS.json')
    finally:
        os.unlink(temporary)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if sys.platform!='linux' or os.geteuid()!=0:
        parser.error('use root on a disposable Linux runner')
    args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
    root=args.output.resolve()
    source=root/'source'; source.mkdir(mode=0o700)
    fixture=Notebook(source)
    recovered=None
    source_instance=fixture.instance
    try:
        fixture.start()
        deployment=fixture.deployment
        with sync_playwright() as p:
            browser=p.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                sessions={}
                for user in ('alice','bob'):
                    inspect(fixture.homes,fixture.instance)
                    context=browser.new_context(ignore_https_errors=True)
                    page=context.new_page()
                    fixture.login(page,user)
                    session,url=fixture.ready(page,user)
                    content=f'{user} saved coursework before backup\n'
                    fixture.kernel(session,url,'python3','from pathlib import Path\nPath("recovered.txt").write_text('+repr(content)+')\nprint("COURSE_OK")')
                    sessions[user]=(context,page,session,url,content)
                cfg=load_config(deployment/'recovery.json')
                assert cfg['repository']==fixture.manifest['backup']['repository']
                assert cfg['password_file']==fixture.manifest['backup']['password_file']
                restic(cfg,'init')
                # Exercise the actual deployment checkpoint, not a mocked fixture hook.
                fixture.verify_manifest_frozen()
                snapshot=backup_repository(deployment,cfg)
                assert not list(deployment.glob('.recovery-*'))
                restic(cfg,'check','--read-data')
                source_instance=fixture.instance
                source_images={name:json.loads(docker('image','inspect',ref))[0]['Id'] for name,ref in
                               [('hub',fixture.hub_image),('course',fixture.image)]}
                compose(deployment,'stop','hub')
                cleanup(source_instance,deployment/'.env')
                xfs_run('umount',str(fixture.homes)); fixture.mounted=False
                # The source filesystem no longer exists; recovery must use Restic.
                (source/'synthetic.img').unlink()
                assert not docker('volume','ls','-q','--filter','label=educloud.workspace.instance='+source_instance)
                # Keep the independent fixture IdP/proxy alive, like an external realm.
                # Current operator roster excludes Alice even though the snapshot allowed her.
                current=root/'current.env'
                env=dict(fixture.env,WORKSPACE_ALLOWED_SUBJECTS=SUBJECTS['bob'])
                current.write_text(''.join(f'{k}={v}\n' for k,v in env.items())); current.chmod(0o600)
                disk=root/'replacement.img'
                with disk.open('xb') as out: out.truncate(2*1024**3)
                homes=root/'replacement-homes'; homes.mkdir()
                xfs_run('mkfs.xfs','-f',str(disk))
                xfs_run('mount','-o','loop,prjquota',str(disk),str(homes))
                destination=root/'restored'
                wrong=root/'wrong-key'; wrong.write_text('incorrect-synthetic-recovery-password'); wrong.chmod(0o600)
                for options,expected in [({'password_file':str(wrong)},RuntimeError)]:
                    try:
                        restore_repository(dict(cfg,**options),snapshot,destination,port(),homes,current)
                    except expected:
                        assert not destination.exists()
                    else: raise AssertionError('wrong repository password accepted')
                try:
                    restore_repository(cfg,snapshot,destination,port(),homes)
                except ValueError as exc:
                    assert 'current-env' in str(exc) and not destination.exists()
                else: raise AssertionError('old snapshot roster accepted without current configuration')
                recovered=restore_repository(cfg,snapshot,destination,port(),homes,current)
                assert not docker('ps','-q','--filter','label=educloud.workspace.instance='+recovered['WORKSPACE_INSTANCE'])
                assert not docker('volume','ls','-q','--filter','name=^'+recovered['WORKSPACE_INSTANCE']+'-hub-data$')
                assert recovered['WORKSPACE_ALLOWED_SUBJECTS']==SUBJECTS['bob']
                assert digest(destination/'runtime/deployment.py')==digest(ROOT/'deployment.py')
                assert digest(destination/'source-manifest.json')==fixture.manifest_digest
                assert not (destination/'manifest.json').exists()
                for user in ('alice','bob'):
                    home=recovered['WORKSPACE_INSTANCE']+'-home-'+hashlib.sha256(SUBJECTS[user].encode()).hexdigest()[:24]
                    assert (homes/home/'work/recovered.txt').read_text()==sessions[user][4]
                def bundled_start():
                    subprocess.run([sys.executable,str(destination/'runtime/deployment.py'),'start','--directory',str(destination)],check=True)
                bundled_start()
                hub=recovered['WORKSPACE_INSTANCE']+'-hub'
                assert json.loads(docker('inspect',hub))[0]['Image']==source_images['hub']
                assert recovered['WORKSPACE_IMAGE']==source_images['course']
                # Fixture-only trust/routing setup remains outside the recovery package.
                # Real workers require their institution's trusted TLS/DNS before cutover.
                docker('network','connect',fixture.network,hub)
                docker('cp',str(source/'ca.pem'),hub+':/usr/local/share/ca-certificates/fixture.crt')
                docker('exec',hub,'update-ca-certificates')
                compose(destination,'stop','hub'); bundled_start()
                nginx=source/'nginx.conf'
                nginx.write_text(nginx.read_text().replace(source_instance+'-hub',hub))
                docker('restart',source_instance+'-proxy')
                wait_proxy(fixture)
                fixture.env=recovered; fixture.instance=recovered['WORKSPACE_INSTANCE']; fixture.homes=homes; fixture.deployment=destination; fixture.env_file=destination/'.env'
                # Old Hub auth must not survive restoration even for still-allowed Bob.
                browser=browser_after_cutover(p,browser,sessions['bob'][2],fixture.origin)
                for user in ('alice','mallory'):
                    ctx=browser.new_context(ignore_https_errors=True); page=ctx.new_page()
                    fixture.login(page,user)
                    assert '403' in page.inner_text('body') or 'not allowed' in page.inner_text('body').lower()
                    ctx.close()
                context=browser.new_context(ignore_https_errors=True); page=context.new_page()
                inspect(homes,fixture.instance)
                fixture.login(page,'bob')
                session,url=fixture.ready(page,'bob')
                response=session.get(url+'/api/contents/recovered.txt',timeout=15); response.raise_for_status()
                assert response.json()['content']==sessions['bob'][4]
                fixture.kernel(session,url,'python3','print("COURSE_OK")')
                fixture.kernel(session,url,'ir','stopifnot(2+3==5); cat("COURSE_OK\\n")')
                page.reload(); page.get_by_text('recovered.txt',exact=True).first.wait_for(state='visible',timeout=60000)
                page.get_by_text('recovered.txt',exact=True).first.dblclick()
                page.locator('.cm-content').filter(has_text='bob saved coursework').wait_for(state='visible')
                page.screenshot(path=str(root/'recovered-notebook.png'))
                # Peer file and a retained removed-user home are not reachable by Bob.
                peer=session.get(fixture.origin+'/user/'+SUBJECTS['alice']+'/api/contents/recovered.txt',timeout=15)
                assert peer.status_code in (403,404)
                # Revoke an active learner on the replacement using its own runtime.
                compose(destination,'stop','hub')
                updated=dict(recovered,WORKSPACE_ALLOWED_SUBJECTS='future-approved-id')
                (destination/'.env').write_text(''.join(f'{k}={v}\n' for k,v in updated.items()))
                # New approved subjects must have a provisioned home before startup.
                # Keep the missing-home refusal as a negative control, then perform
                # the operator prerequisite rather than bypassing startup preflight.
                denied_start=subprocess.run([sys.executable,str(destination/'runtime/deployment.py'),
                    'start','--directory',str(destination)],capture_output=True,text=True)
                assert denied_start.returncode != 0 and 'unprovisioned homes' in denied_start.stderr
                assert not docker('ps','-q','--filter','label=educloud.workspace.instance='+fixture.instance)
                provision(homes,fixture.instance,['future-approved-id'],
                          int(updated['WORKSPACE_HOME_QUOTA_MB']),int(updated['WORKSPACE_HOME_INODE_LIMIT']))
                # Recreate reads the changed env; fixture CA/routing must be reattached.
                bundled_start()
                docker('network','connect',fixture.network,hub)
                docker('cp',str(source/'ca.pem'),hub+':/usr/local/share/ca-certificates/fixture.crt')
                docker('exec',hub,'update-ca-certificates')
                compose(destination,'stop','hub'); bundled_start()
                docker('restart',source_instance+'-proxy')
                wait_proxy(fixture)
                denied=session.get(url+'/api/contents/recovered.txt',timeout=15,allow_redirects=False)
                assert denied.status_code in (302,303,403,404,503)
                page.goto(fixture.origin+'/hub/spawn/'+SUBJECTS['bob'])
                page.get_by_text('Your account is not in this course roster. Contact your instructor.',exact=False).wait_for(state='visible',timeout=60000)
                assert not docker('ps','-q','--filter','label=educloud.workspace.instance='+fixture.instance,
                                  '--filter','label=educloud.workspace.kind=learner')
                bob_home=fixture.instance+'-home-'+hashlib.sha256(SUBJECTS['bob'].encode()).hexdigest()[:24]
                assert (homes/bob_home/'work/recovered.txt').read_text()==sessions['bob'][4]
                # Quotas remain enforced on the newly created filesystem.
                inspect(homes,fixture.instance)
                try:
                    restore_repository(cfg,snapshot,destination,port(),homes,current)
                except ValueError: pass
                else: raise AssertionError('restore overwrote existing destination')
                context.close()
                report={'snapshot_id':snapshot,'source_destroyed':True,'same_worker_rehearsal':True,
                        'images':source_images,'course_manifest_sha256':fixture.manifest_digest,'waypoint_revision':run('git','-c','safe.directory='+str(ROOT.parent),'-C',str(ROOT.parent),'rev-parse','HEAD'),
                        'checks':fixture.report+['manifest-backup-config-consumed','historical-manifest-preserved','encrypted-backup','source-resumed','wrong-key-denied','current-roster-required',
                                  'fresh-hub-sessions','browser-relaunched-after-network-cutover','removed-and-unrostered-login-denied','retained-removed-home',
                                  'preserved-images','bundled-runtime-start','restored-python-r','saved-file-visible',
                                  'peer-file-denied','unprovisioned-roster-start-refused','replacement-active-revocation','revocation-retains-files','restored-hard-quotas','existing-destination-denied']}
            finally: browser.close()
    finally:
        cleanup_verified=cleanup_rehearsal(fixture,root,source_instance,recovered)
    publish_pass(root,report,cleanup_verified)
    print('PASS: encrypted OIDC recovery with current roster, fresh sessions, retained files, Python/R, quotas and verified cleanup.',flush=True)


if __name__=='__main__': main()
