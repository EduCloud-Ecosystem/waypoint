#!/usr/bin/env python3
"""Recover synthetic OIDC coursework after destroying source Hub/volumes/mount."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from playwright.sync_api import sync_playwright
from notebook import Notebook, SUBJECTS, ROOT
from backup import docker, digest
from deployment import compose, wait_ready
from homes import inspect, run as xfs_run
from recovery import backup_repository, restore_repository, restic
from verify import cleanup, port
from cairn_journey import run


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
    recovered=None; mounted=False
    try:
        fixture.start()
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
                key=root/'password'; key.write_text('synthetic-institutional-recovery-key-never-use-for-real-data'); key.chmod(0o600)
                cfg={'repository':str(root/'encrypted-repository'),'password_file':str(key)}
                restic(cfg,'init')
                # Exercise the actual deployment checkpoint, not a mocked fixture hook.
                snapshot=backup_repository(source,cfg)
                assert not list(source.glob('.recovery-*'))
                restic(cfg,'check','--read-data')
                source_instance=fixture.instance
                source_images={name:json.loads(docker('image','inspect',ref))[0]['Id'] for name,ref in
                               [('hub',fixture.instance+'-hub:pilot'),('course',fixture.image)]}
                compose(source,'stop','hub')
                cleanup(source_instance,source/'.env')
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
                xfs_run('mount','-o','loop,prjquota',str(disk),str(homes)); mounted=True
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
                fixture.env=recovered; fixture.instance=recovered['WORKSPACE_INSTANCE']; fixture.homes=homes
                # Old Hub auth must not survive restoration even for still-allowed Bob.
                old=sessions['bob'][2].get(fixture.origin+'/hub/api/users/'+SUBJECTS['bob'],timeout=15,allow_redirects=False)
                assert old.status_code in (401,403), f'old session accepted: {old.status_code}'
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
                # Quotas remain enforced on the newly created filesystem.
                inspect(homes,fixture.instance)
                try:
                    restore_repository(cfg,snapshot,destination,port(),homes,current)
                except ValueError: pass
                else: raise AssertionError('restore overwrote existing destination')
                for ctx,*_ in sessions.values(): ctx.close()
                context.close()
                report={'snapshot_id':snapshot,'source_destroyed':True,'same_worker_rehearsal':True,
                        'images':source_images,'waypoint_revision':run('git','-c','safe.directory='+str(ROOT.parent),'-C',str(ROOT.parent),'rev-parse','HEAD'),
                        'checks':['encrypted-backup','source-resumed','wrong-key-denied','current-roster-required',
                                  'fresh-hub-sessions','removed-and-unrostered-login-denied','retained-removed-home',
                                  'preserved-images','bundled-runtime-start','restored-python-r','saved-file-visible',
                                  'peer-file-denied','restored-hard-quotas','existing-destination-denied']}
                (root/'PASS.json').write_text(json.dumps(report,indent=2)+'\n')
                print('PASS: encrypted OIDC recovery with current roster, fresh sessions, retained files, Python/R and quotas.',flush=True)
            finally: browser.close()
    finally:
        if recovered:
            cleanup(recovered['WORKSPACE_INSTANCE'],root/'restored/.env')
        # Notebook.close owns only the original fixture; source homes already unmounted.
        # Remove fixture-owned proxy/IdP using their original generated prefix.
        fixture.instance=fixture.network.removesuffix('-fixture')
        fixture.close()
        if mounted: xfs_run('umount',str(root/'replacement-homes'))


if __name__=='__main__': main()
