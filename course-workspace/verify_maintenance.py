#!/usr/bin/env python3
"""Real synthetic maintenance conformance; root/Linux only, new disposable XFS image."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import uuid
from unittest.mock import patch

from backup import docker
import homes
import maintenance as m
from public_course import render


def refused(action, text):
    try:
        action()
    except ValueError as exc:
        if text not in str(exc):
            raise
    else:
        raise AssertionError('unsafe maintenance accepted: ' + text)


def run(output):
    if not __debug__ or os.geteuid() != 0 or platform.system() != 'Linux':
        raise ValueError('requires ordinary Python (without -O), root and a disposable Linux worker')
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    output = output.resolve()
    root, directory, disk = output/'homes', output/'deployment', output/'fixture.img'
    root.mkdir(); directory.mkdir(mode=0o700)
    instance = 'maint-' + uuid.uuid4().hex[:12]
    label = 'educloud.workspace.instance=' + instance
    image = 'busybox:1.37@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e'
    checks = []
    mounted = False
    cleanup_errors = []
    try:
        with disk.open('xb') as file:
            file.truncate(2*1024**3)
        homes.run('mkfs.xfs', '-f', str(disk))
        homes.run('mount', '-o', 'loop,prjquota', str(disk), str(root))
        mounted = True
        docker('pull', image)
        image_id = json.loads(docker('image', 'inspect', image))[0]['Id']
        _, _, env = render('course.example.org', 'https://identity.example.org/realms/test', 'course', instance, ['alice', 'bob'], str(root))
        env.update(WORKSPACE_OIDC_CLIENT_SECRET='synthetic-maintenance-only', WORKSPACE_ENV_FILE=str(directory/'.env'), WORKSPACE_HOME_QUOTA_MB='64', WORKSPACE_HOME_INODE_LIMIT='1000')
        m.write(directory/'.env', ''.join(f'{k}={v}\n' for k,v in env.items()).encode())
        homes.provision(root, instance, ['alice', 'bob', 'carol'], 64, 1000)
        names = {s: instance+'-home-'+hashlib.sha256(s.encode()).hexdigest()[:24] for s in ('alice','bob','carol')}
        for subject, name in names.items():
            (root/name/'saved.txt').write_text(subject+' saved work')
        plan = output/'plan.json'
        m.preview(directory, ['bob','carol'], 96, 2000, plan)
        # Actual foreign Docker reference must block a resize, even if stopped.
        ref = instance+'-foreign-reference'
        docker('create', '--name', ref, '--label', label, '--mount', f'type=volume,source={names["alice"]},target=/work', image, 'true')
        refused(lambda: m.apply(directory, plan), 'references')
        docker('rm', ref)
        checks.append('foreign-stopped-reference-refused')
        # Inject one process failure after real quota change and bind-volume removal.
        original_docker = m.docker
        failed = False
        def interrupted(*args):
            nonlocal failed
            if args[:2] == ('volume','create') and not failed:
                failed = True
                raise OSError('synthetic interruption after volume metadata removal')
            return original_docker(*args)
        with patch('maintenance.docker', interrupted):
            try:
                m.apply(directory, plan)
            except OSError as exc:
                if 'synthetic interruption' not in str(exc):
                    raise
            else:
                raise AssertionError('failure injection did not trigger')
        refused(lambda: homes.inspect(root, instance), 'unfinished maintenance')
        assert (directory/m.PENDING).exists()
        m.apply(directory, plan)
        checks.append('real-quota-and-volume-interruption-resumed')
        report = homes.inspect(root, instance)
        assert m.environment(directory)['WORKSPACE_ALLOWED_SUBJECTS'] == 'bob,carol'
        assert len(report['volumes']) == 3
        assert all(v['quota_bytes'] == 96*1024**2 and v['inode_limit'] == 2000 for v in report['volumes'].values())
        assert all((root/name/'saved.txt').read_text() == subject+' saved work' for subject,name in names.items())
        checks.append('roster-add-remove-retains-all-work-and-labels')
        # Exercise the resized kernel-enforced quota through the actual bind volume.
        result = subprocess.run(['docker','run','--rm','--label',label,'--network','none','--read-only','--cap-drop','ALL','--user','1000:1000','--mount',f'type=volume,source={names["bob"]},target=/work',image,'sh','-c','dd if=/dev/zero of=/work/fill bs=1048576 count=128; sync'], capture_output=True, text=True, timeout=90)
        # sh's final sync succeeds; measured allocation is the independent assertion.
        project = homes.registry(root)['homes'][names['bob']]['project_id']
        used = homes.limits(root,'b')[project]['used']
        assert 64*1024 < used <= 96*1024, (used, result.returncode)
        refused(lambda: m.preview(directory,['bob','carol'],32,2000,output/'too-small.json'), 'below actual')
        (root/names['bob']/'fill').unlink()
        homes.run('sync')
        checks.append('grown-quota-enforced-and-below-used-shrink-refused')
        stale = output/'stale.json'
        m.preview(directory,['bob','carol'],80,1800,stale)
        (root/names['carol']/'later.txt').write_bytes(b'x'*16384)
        homes.run('sync')
        refused(lambda: m.apply(directory,stale),'stale')
        checks.append('stale-quota-usage-plan-refused')
        shrink = output/'shrink.json'
        m.preview(directory,['bob','carol'],80,1800,shrink)
        m.apply(directory,shrink)
        assert all(v['quota_bytes']==80*1024**2 and v['inode_limit']==1800 for v in homes.inspect(root,instance)['volumes'].values())
        checks.append('safe-course-wide-shrink-verified')
    finally:
        # Attempt all fixture cleanup; never publish PASS before independent absence checks.
        for kind, listing, removal in [('containers', ('ps','-a','-q','--filter','label='+label), ('rm','-f')),
                                       ('volumes', ('volume','ls','-q','--filter','label='+label), ('volume','rm'))]:
            try:
                found = docker(*listing).splitlines()
                for item in found:
                    try:
                        docker(*removal,item)
                    except Exception as exc:
                        cleanup_errors.append(kind+': '+str(exc))
                if docker(*listing):
                    cleanup_errors.append(kind+' remain')
            except Exception as exc:
                cleanup_errors.append(kind+' inventory: '+str(exc))
        if mounted or os.path.ismount(root):
            try:
                homes.run('umount',str(root))
            except Exception as exc:
                cleanup_errors.append('unmount: '+str(exc))
        if os.path.ismount(root):
            cleanup_errors.append('fixture mount remains')
        if cleanup_errors:
            raise RuntimeError('fixture cleanup incomplete: '+'; '.join(cleanup_errors))
        disk.unlink(missing_ok=True)
    evidence = {'version':'educloud-maintenance-conformance-v1','scope':'synthetic-linux-xfs-docker','checks':checks,'cleanup_verified':True,'image_id':image_id}
    m.write(output/'PASS.json',evidence,exclusive=True)
    print('PASS: real XFS/Docker maintenance; all fixture resources and mounts cleaned.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    run(args.output)


if __name__=='__main__':
    main()
