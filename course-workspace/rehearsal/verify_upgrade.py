#!/usr/bin/env python3
"""Stopped image upgrade and explicit snapshot rollback on disposable Linux."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from notebook import Notebook, SUBJECTS, ROOT
from backup import docker, digest
from deployment import read_environment
from homes import inspect, run as xfs_run
from recovery import load_config, restore_repository, restic
from verify import port
from verify_institutional_recovery import cleanup_rehearsal, wait_proxy, browser_after_cutover
from evidence import _source, sha256_file
import upgrade

HASH = re.compile(r'[0-9a-f]{64}\Z')
IMAGE = re.compile(r'sha256:[0-9a-f]{64}\Z')
CHECKS = frozenset({'full-course-candidate-canary', 'fresh-preupgrade-backup',
    'changed-canary-refused', 'wrong-image-canary-refused', 'changed-environment-refused',
    'changed-manifest-refused', 'changed-saved-file-refused', 'interrupted-write-resumed',
    'stopped-after-apply', 'unchanged-roster-policy', 'candidate-pins-started',
    'candidate-python-r', 'saved-work-preserved', 'prior-images-retained',
    'snapshot-integrity', 'fresh-namespace-rollback', 'prior-pins-restored',
    'rollback-python-r', 'rollback-saved-work', 'fresh-rollback-session'})


def publish_pass(root, report, cleanup_verified):
    """Only publish bounded public identifiers after every control and cleanup."""
    required = {'waypoint_revision', 'canary_sha256', 'upgrade_fixture_sha256',
                'source_manifest_sha256', 'old_images', 'new_images', 'snapshot_id', 'checks'}
    if cleanup_verified is not True or not isinstance(report, dict) or set(report) != required:
        raise ValueError('upgrade acceptance requires complete bounded evidence and cleanup')
    if (not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', str(report['waypoint_revision']))
            or any(not isinstance(report[field], str) or not HASH.fullmatch(report[field]) for field in
                   ('canary_sha256', 'upgrade_fixture_sha256', 'source_manifest_sha256', 'snapshot_id'))):
        raise ValueError('upgrade evidence identity invalid')
    for field in ('old_images', 'new_images'):
        if (not isinstance(report[field], dict) or set(report[field]) != {'hub', 'course'}
                or any(not isinstance(value, str) or not IMAGE.fullmatch(value) for value in report[field].values())):
            raise ValueError('upgrade evidence image identities invalid')
    if (not isinstance(report['checks'], list) or set(report['checks']) != CHECKS
            or len(report['checks']) != len(CHECKS) or report['new_images'] == report['old_images']):
        raise ValueError('upgrade acceptance controls incomplete')
    data = (json.dumps(dict(report, version=1, status='PASS',
            scope='same-worker-synthetic-image-upgrade', cleanup_verified=True),
            indent=2, sort_keys=True, allow_nan=False)+'\n').encode()
    if len(data) > 64*1024:
        raise ValueError('upgrade evidence exceeds output bound')
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077:
        raise ValueError('upgrade evidence directory must be private')
    fd, temporary = tempfile.mkstemp(prefix='.upgrade-evidence-', dir=root)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.link(temporary, root/'UPGRADE-PASS.json')
    finally:
        os.unlink(temporary)


def refused(operation):
    try:
        operation()
    except upgrade.UpgradeError:
        return
    raise AssertionError('stale or mismatched upgrade input was accepted')


def verify_saved_work(fixture, browser, expected, screenshot=None):
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()
    inspect(fixture.homes, fixture.instance)
    fixture.login(page, 'bob')
    session, url = fixture.ready(page, 'bob')
    for name, content in expected.items():
        response = session.get(url+'/api/contents/'+name, timeout=15)
        response.raise_for_status()
        assert response.json()['content'] == content, 'saved coursework changed across image transition'
    fixture.kernel(session, url, 'python3', 'print("COURSE_OK")')
    fixture.kernel(session, url, 'ir', 'stopifnot(sum(c(2,3))==5); cat("COURSE_OK\\n")')
    if screenshot:
        page.reload()
        page.get_by_text('upgrade.txt', exact=True).first.wait_for(state='visible', timeout=60000)
        page.get_by_text('upgrade.txt', exact=True).first.dblclick()
        page.locator('.cm-content').filter(has_text=expected['upgrade.txt'].strip()).wait_for(state='visible', timeout=30000)
        page.screenshot(path=str(screenshot))
    return context, page, session, url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if not __debug__:
        parser.error('optimized Python cannot enforce the synthetic acceptance assertions; run without -O')
    parser.add_argument('--canary', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('use root on a disposable Linux runner; this formats invocation-owned loop images only')
    canary_path = args.canary.resolve(strict=True)
    value, _ = upgrade.read_json(canary_path, private=False)
    images = {role: value['provenance']['image_ids'][role] for role in ('hub', 'course')}
    canary_hash = upgrade.canary(canary_path, images)
    source_state = _source(ROOT.parent)
    assert source_state['dirty'] is False and value['provenance']['waypoint'] == source_state, 'canary source differs from upgrade fixture'
    fixture_hash = sha256_file(__file__)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    root = args.output.resolve()
    source = root/'source'; source.mkdir(mode=0o700)
    fixture = Notebook(source)
    source_instance = fixture.instance
    recovered = None
    checks = []
    try:
        fixture.start()
        old_images = {'hub': fixture.hub_image, 'course': fixture.image}
        assert images != old_images, 'canary must exercise distinct candidate image identities'
        checks.append('full-course-candidate-canary')
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                context = browser.new_context(ignore_https_errors=True); page = context.new_page()
                fixture.login(page, 'bob')
                session, url = fixture.ready(page, 'bob')
                expected = {'upgrade.txt':'saved Python coursework before image upgrade\n',
                            'upgrade-r.txt':'saved R coursework before image upgrade\n'}
                fixture.kernel(session, url, 'python3', 'from pathlib import Path\nPath("upgrade.txt").write_text('+repr(expected['upgrade.txt'])+')\nprint("COURSE_OK")')
                fixture.kernel(session, url, 'ir', 'writeLines("saved R coursework before image upgrade", "upgrade-r.txt"); cat("COURSE_OK\\n")')
                fixture.compose('stop', 'hub')
                original_env = read_environment(fixture.env_file)
                recovery_path = fixture.deployment/'recovery.json'
                cfg = load_config(recovery_path)
                restic(cfg, 'init')
                private_canary = root/'canary.json'
                private_canary.write_bytes(canary_path.read_bytes()); private_canary.chmod(0o600)
                stale_plan_path = root/'stale-plan.json'
                refused(lambda: upgrade.preview(fixture.deployment, old_images['hub'], images['course'],
                                                private_canary, recovery_path, root/'wrong-images-plan.json'))
                checks.append('wrong-image-canary-refused')
                upgrade.preview(fixture.deployment, images['hub'], images['course'], private_canary, recovery_path, stale_plan_path)
                controls = [('changed-canary-refused', private_canary, private_canary.read_bytes()+b'\n'),
                            ('changed-environment-refused', fixture.env_file,
                             fixture.env_file.read_bytes().replace(b'WORKSPACE_IDLE_SECONDS=1800', b'WORKSPACE_IDLE_SECONDS=1801')),
                            ('changed-manifest-refused', fixture.manifest_path, fixture.manifest_path.read_bytes()+b'\n')]
                for check, path, changed in controls:
                    previous = path.read_bytes()
                    assert changed != previous
                    try:
                        path.write_bytes(changed)
                        refused(lambda: upgrade.apply(fixture.deployment, stale_plan_path, private_canary, recovery_path))
                        assert not (fixture.deployment/upgrade.PENDING).exists()
                        assert not docker('ps', '-q', '--filter', 'label=educloud.workspace.instance='+fixture.instance)
                    finally:
                        path.write_bytes(previous)
                    checks.append(check)
                home = fixture.instance+'-home-'+hashlib.sha256(SUBJECTS['bob'].encode()).hexdigest()[:24]
                saved_file = fixture.homes/home/'work/upgrade.txt'
                saved_file.write_text('changed after preview\n')
                refused(lambda: upgrade.apply(fixture.deployment, stale_plan_path, private_canary, recovery_path))
                saved_file.write_text(expected['upgrade.txt'])
                checks.append('changed-saved-file-refused')
                plan_path = root/'upgrade-plan.json'
                plan = upgrade.preview(fixture.deployment, images['hub'], images['course'], private_canary, recovery_path, plan_path)
                checks.append('fresh-preupgrade-backup')
                real_write = upgrade.write
                def interrupted_write(path, *args, **kwargs):
                    result = real_write(path, *args, **kwargs)
                    if Path(path) == fixture.env_file:
                        raise RuntimeError('synthetic interruption after durable environment replacement')
                    return result
                with patch('upgrade.write', side_effect=interrupted_write):
                    try:
                        upgrade.apply(fixture.deployment, plan_path, private_canary, recovery_path)
                    except upgrade.UpgradeError:
                        pass
                    else:
                        raise AssertionError('interrupted upgrade unexpectedly completed')
                assert (fixture.deployment/upgrade.PENDING).is_file()
                assert not (fixture.deployment/upgrade.RECEIPT).exists()
                receipt = upgrade.apply(fixture.deployment, plan_path, private_canary, recovery_path)
                assert receipt['status'] == 'APPLIED' and receipt['images'] == images
                assert receipt['deployment_started'] is False
                assert not (fixture.deployment/upgrade.PENDING).exists()
                checks.append('interrupted-write-resumed')
                assert not docker('ps', '-q', '--filter', 'label=educloud.workspace.instance='+fixture.instance)
                checks.append('stopped-after-apply')
                updated = read_environment(fixture.env_file)
                assert {k:v for k,v in updated.items() if k not in ('WORKSPACE_HUB_IMAGE','WORKSPACE_IMAGE')} == {
                    k:v for k,v in original_env.items() if k not in ('WORKSPACE_HUB_IMAGE','WORKSPACE_IMAGE')}
                fixture.verify_manifest_frozen()
                assert saved_file.read_text() == expected['upgrade.txt']
                checks.append('unchanged-roster-policy')
                fixture.env = updated; fixture.hub_image = images['hub']; fixture.image = images['course']
                fixture.compose('up', '-d', '--force-recreate', '--no-build', '--pull', 'never', 'hub')
                wait_proxy(fixture)
                context.close()
                context, page, session, url = verify_saved_work(fixture, browser, expected, root/'upgraded-notebook.png')
                checks += ['candidate-pins-started', 'candidate-python-r', 'saved-work-preserved']
                for identity in old_images.values():
                    assert json.loads(docker('image', 'inspect', identity))[0]['Id'] == identity
                checks.append('prior-images-retained')
                restic(cfg, 'check', '--read-data')
                checks.append('snapshot-integrity')
                # Rollback restores prior images and saved files into a fresh namespace.
                # Institutional recovery starts fresh Hub sessions under current policy.
                fixture.compose('stop', 'hub')
                disk = root/'replacement.img'
                with disk.open('xb') as out: out.truncate(2*1024**3)
                homes = root/'replacement-homes'; homes.mkdir()
                xfs_run('mkfs.xfs', '-f', str(disk)); xfs_run('mount', '-o', 'loop,prjquota', str(disk), str(homes))
                destination = root/'restored'
                recovered = restore_repository(cfg, plan['snapshot_id'], destination, port(), homes, fixture.env_file)
                assert recovered['WORKSPACE_INSTANCE'] != source_instance
                assert recovered['WORKSPACE_HUB_IMAGE'] == old_images['hub'] and recovered['WORKSPACE_IMAGE'] == old_images['course']
                assert digest(destination/'source-manifest.json') == fixture.manifest_digest
                checks += ['fresh-namespace-rollback', 'prior-pins-restored']
                def bundled_start():
                    import subprocess
                    subprocess.run([sys.executable, str(destination/'runtime/deployment.py'), 'start', '--directory', str(destination)], check=True)
                bundled_start()
                hub = recovered['WORKSPACE_INSTANCE']+'-hub'
                docker('network', 'connect', fixture.network, hub)
                docker('cp', str(source/'ca.pem'), hub+':/usr/local/share/ca-certificates/fixture.crt')
                docker('exec', hub, 'update-ca-certificates')
                from deployment import compose
                compose(destination, 'stop', 'hub'); bundled_start()
                nginx = source/'nginx.conf'
                nginx.write_text(nginx.read_text().replace(source_instance+'-hub', hub))
                docker('restart', source_instance+'-proxy')
                wait_proxy(fixture)
                browser = browser_after_cutover(playwright, browser, session, fixture.origin)
                checks.append('fresh-rollback-session')
                restored = copy.copy(fixture)
                restored.instance = recovered['WORKSPACE_INSTANCE']; restored.env = recovered
                restored.deployment = destination; restored.env_file = destination/'.env'; restored.homes = homes
                restored.hub_image = old_images['hub']; restored.image = old_images['course']
                context, page, session, url = verify_saved_work(restored, browser, expected, root/'recovered-notebook.png')
                checks += ['rollback-python-r', 'rollback-saved-work']
                context.close()
                assert source_state == _source(ROOT.parent) and sha256_file(__file__) == fixture_hash, 'source changed during upgrade rehearsal'
                assert sha256_file(canary_path) == canary_hash, 'candidate canary changed during upgrade rehearsal'
                fixture.verify_manifest_frozen()
                report = {'waypoint_revision':source_state['revision'], 'canary_sha256':canary_hash,
                          'upgrade_fixture_sha256':fixture_hash, 'source_manifest_sha256':fixture.manifest_digest,
                          'old_images':old_images, 'new_images':images, 'snapshot_id':plan['snapshot_id'], 'checks':sorted(checks)}
            finally:
                browser.close()
    finally:
        cleanup_verified = cleanup_rehearsal(fixture, root, source_instance, recovered)
    publish_pass(root, report, cleanup_verified)
    print('PASS: synthetic stopped-image upgrade, resumed apply, and fresh snapshot rollback; cleanup verified.', flush=True)


if __name__ == '__main__': main()
