"""Finite synthetic NHANES no-AI desktop qualification; never student admission."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'classroom'), str(ROOT / 'course')]
from assignment_profiles import NHANES
from info511_policy import template_files
from cairn_journey import Course, port, wait_http
from info511_demo import req, active_work, await_grade
from info511_notebook import contents, manifest, saved_files
from classroom_transfer import submit_info511_attempt
from roster_presenter import select_python_kernel, private, command
from submission_handoff import verify_receipt_after_restart
from operator_review import wait_for_review, validate
from nhanes_tutoring import NhanesTutoring, validate_tutoring
from playwright.sync_api import sync_playwright, expect

PINS = {'cairn': 'a6322abfa687a206e659e8e874134ad8effad8b1',
        'belay': 'c5a27073e74b9eeb7ab697190915378b50132883'}
STUB_CAIRN_PIN = 'a6322abfa687a206e659e8e874134ad8effad8b1'


def restart_receipt(course, browser, learner, submission, receipt):
    """Restart invalidates Cairn sessions: authenticate again before readback."""
    course.restart()
    fresh, _ = course.login(browser, 'alice', '/student/login?host=forgejo')
    try:
        verify_receipt_after_restart(course, fresh, submission, receipt)
        # Keep the already-open Jupyter page usable in the optional review;
        # copy only the newly authenticated synthetic learner's Cairn cookies.
        learner.add_cookies(fresh.cookies(course.url))
    finally:
        fresh.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cairn', 'belay', 'kernel-python', 'kernel-lock', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    for name in ('grader-image', 'checkout-image'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--operator-review-seconds', type=int, default=0)
    parser.add_argument('--tutor-mode', choices=('none', 'stub', 'live'), default='none')
    parser.add_argument('--live-portage', type=Path)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--execute-approved', action='store_true')
    parser.add_argument('--live-review-seconds', type=int, default=0,
                        help='After no-AI controls, open one prepared live hint for at most 240 seconds')
    parser.add_argument('--execute-synthetic', action='store_true')
    parser.add_argument('--verify-studio-ui', action='store_true',
                        help='Exercise reflow, keyboard focus and natural credential expiry with the local stub')
    args = parser.parse_args()
    if not args.execute_synthetic: parser.error('explicit synthetic execution required')
    args.kernel_python = args.kernel_python.absolute()  # Preserve the venv symlink path.
    validate(args.operator_review_seconds, live_portage=args.live_portage,
             env_file=args.env_file, execute_approved=args.execute_approved)
    validate_tutoring(args.tutor_mode, args.live_portage, args.env_file,
                      args.execute_approved, args.live_review_seconds)
    if args.verify_studio_ui and (args.tutor_mode != 'stub' or args.operator_review_seconds):
        parser.error('Studio verification requires the local stub without an operator review window')
    for name in ('cairn', 'belay', 'live_portage', 'env_file'):
        if getattr(args, name) is not None: setattr(args, name, getattr(args, name).resolve())
    os.umask(0o077)
    root = args.output.resolve(); root.mkdir(mode=0o700)
    report = {'scope': 'synthetic-local-NHANES-no-AI-native-notebook-submission',
        'real_students': False, 'institutional_sso': False, 'human_acceptance': False,
        'model_transports': 0, 'tutoring_tested': False, 'passed': [], 'failed': [], 'cleanup': False}
    processes = []; logs = []; course = None; tutor = None; started = time.monotonic()
    if args.tutor_mode != 'none':
        report['scope'] = 'synthetic-local-NHANES-' + args.tutor_mode + '-notebook-submission'
    def expired(*_): raise TimeoutError('finite native journey deadline')
    signal.signal(signal.SIGALRM, expired); signal.alarm(600)
    def launch(name, argv, env):
        log = (root / (name + '.log')).open('w'); logs.append(log)
        proc = subprocess.Popen([str(v) for v in argv], env=env, cwd=root,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(proc); return proc
    try:
        pins = PINS if args.tutor_mode == 'live' else dict(PINS, cairn=STUB_CAIRN_PIN)
        for role, pin in pins.items():
            repo = getattr(args, role).resolve()
            assert command(['git', 'rev-parse', 'HEAD'], cwd=repo) == pin
            assert not command(['git', 'status', '--porcelain'], cwd=repo)
        report['sources'] = dict(pins, waypoint=command(['git','rev-parse','HEAD'],cwd=ROOT),
            waypoint_dirty=bool(command(['git','status','--porcelain'],cwd=ROOT)))
        for image in (args.grader_image, args.checkout_image):
            assert image.startswith('sha256:') and len(image) == 71
            assert command(['docker','image','inspect',image,'--format','{{.Id}}']) == image
        report['images'] = {'grader':args.grader_image,'checkout':args.checkout_image}
        report['kernel'] = json.loads(command([args.kernel_python,'-c',
            'import sys,importlib.metadata,json; print(json.dumps({"python":sys.version,"packages":sorted((d.metadata["Name"],d.version) for d in importlib.metadata.distributions())}))']))
        report['kernel']['lock_sha256'] = hashlib.sha256(args.kernel_lock.read_bytes()).hexdigest()
        private(root/'kernel-requirements.lock',args.kernel_lock.read_bytes())
        binding = json.loads((NHANES.package/'operator/cairn-source-binding.json').read_text())
        for name,h in binding['cairn_files'].items(): assert hashlib.sha256((args.cairn/name).read_bytes()).hexdigest()==h
        policy = template_files(args.belay, args.grader_image, profile=NHANES)
        report['task_sha256'] = hashlib.sha256((NHANES.package/'task.json').read_bytes()).hexdigest()
        report['checker_binding'] = json.loads((NHANES.package/'operator/checker-binding.json').read_text())
        work = root/'saved-work'; shutil.copytree(NHANES.package/'operator/attempts/alice',work)
        shutil.copytree(NHANES.package/'learner/data',work/'data')
        notebook = json.loads((work/NHANES.files[0]).read_text())
        authored = {c['id']:''.join(c['source']) for c in notebook['cells'] if c['cell_type']=='code'}
        authored['learner-work'] += "\nprint('NHANES_NATIVE_OK', len(tidy_nhanes))\n"
        for cell in notebook['cells']:
            if cell['cell_type']=='code':
                cell.update(execution_count=None,outputs=[])
                if cell['id']=='learner-work': cell['source']='# Enter the operator-authored synthetic solution here.\n'
        (work/NHANES.files[0]).write_text(json.dumps(notebook))
        (work/NHANES.files[1]).unlink()  # The actual kernel must create the CSV.
        notebook_url = 'http://127.0.0.1:'+str(port())
        course_dir=root/'course';course_dir.mkdir()
        binary=root/'cairn'
        private(root/'build.log',command(['go','build','-o',binary,'./cmd/cairn'],cwd=args.cairn))
        report['cairn_binary_sha256']=hashlib.sha256(binary.read_bytes()).hexdigest()
        course=Course(course_dir,binary,notebook_url,args.grader_image,args.cairn,
                      template_files=policy,checkout_image=args.checkout_image)
        clean={k:os.environ[k] for k in ('PATH','HOME','LANG','DOCKER_HOST','DOCKER_CONTEXT') if k in os.environ}
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=not bool(args.operator_review_seconds or args.live_review_seconds))
            try:
                revision=course.start();teacher,_=course.login(browser,'teacher','/auth/login')
                cid=req(course,teacher,'POST','/classrooms',{'name':'Disposable NHANES synthetic qualification',
                    'host':'forgejo','host_namespace':'course-test','join_policy':'roster'},201)['id']
                for user in ('alice','bob'):req(course,teacher,'POST',f'/classrooms/{cid}/roster',{'username':user},201)
                aid=req(course,teacher,'POST',f'/classrooms/{cid}/assignments',{'title':NHANES.title,'slug':NHANES.task,
                    'type':'individual','grading_spec':'grading.json','template':{'namespace':'course-test','name':'template','ref':revision}},201)['id']
                req(course,teacher,'PATCH',f'/assignments/{aid}/grading-policy',{'template_commit':revision,'grading_spec':'grading.json'})
                course.env.update(CAIRN_SUBMISSIONS='true',CAIRN_SUBMISSION_CLASSROOMS=cid,
                    CAIRN_WORKSPACE_URLS=json.dumps({cid:notebook_url}));course.restart()
                alice,_=course.login(browser,'alice',f'/assignments/{aid}/accept')
                bob,_=course.login(browser,'bob',f'/assignments/{aid}/accept')
                item=active_work(course,alice,profile=NHANES);peer=active_work(course,bob,profile=NHANES)
                report['binding']={'assignment_id':aid,'classroom_id':cid,'template_commit':revision,
                                   'kind':'disposable-synthetic-only','profile':'nhanes-v1'}
                if args.tutor_mode != 'none':
                    tutor = NhanesTutoring(args, root, course, cid, aid, clean, launch, notebook_url, NHANES)
                data=root/'jupyter-data';ext=data/'labextensions/@educloud';ext.mkdir(parents=True)
                shutil.copytree(ROOT/'course/labextension',ext/'waypoint-cairn')
                if tutor:
                    shutil.copytree(ROOT/'course/labextension-belay', ext/'waypoint-belay')
                kernels=data/'kernels/python3';kernels.mkdir(parents=True)
                private(kernels/'kernel.json',{'argv':[str(args.kernel_python),'-m','ipykernel_launcher','-f','{connection_file}'],
                    'display_name':'Python 3 (ipykernel)','language':'python'})
                config=root/'jupyter-config';config.mkdir()
                extensions = {'waypoint_cairn': True}
                if tutor: extensions['waypoint_belay'] = True
                private(config/'jupyter_server_config.json',{'ServerApp':{'jpserver_extensions':extensions}})
                settings = root/'jupyter-settings'
                notifications = settings/'@jupyterlab/apputils-extension'
                notifications.mkdir(parents=True)
                private(notifications/'notification.jupyterlab-settings', {'fetchNews':'false','checkForUpdates':False})
                token=secrets.token_urlsafe(32)
                env=dict(clean,PYTHONPATH=str(ROOT/'course'),JUPYTER_PATH=str(data),JUPYTER_DATA_DIR=str(data),
                    JUPYTER_CONFIG_DIR=str(config),JUPYTER_RUNTIME_DIR=str(root/'jupyter-runtime'),JUPYTER_TOKEN=token,
                    JUPYTERLAB_SETTINGS_DIR=str(settings),
                    WORKSPACE_CAIRN_ORIGIN=course.url,WORKSPACE_CAIRN_LOCAL_TEST='1')
                if tutor: env.update(tutor.environment)
                launch('notebook',[sys.executable,'-m','jupyterlab','--no-browser','--ip=127.0.0.1',
                    '--port='+notebook_url.rsplit(':',1)[1],'--ServerApp.port_retries=0','--ServerApp.root_dir='+str(work)],env)
                wait_http(notebook_url+'/login')
                page=alice.new_page();page.goto(notebook_url+'/lab/tree/'+NHANES.files[0]+'?token='+token)
                expect(page.get_by_role('link',name='Submit saved files to Cairn',exact=True)).to_be_visible(timeout=60000)
                select_python_kernel(page,root,'nhanes')
                widgets=page.locator('.jp-Notebook .jp-Cell');expect(widgets).to_have_count(len(notebook['cells']),timeout=60000)
                if tutor and not args.live_review_seconds:
                    tutor.verify(page, report)
                for index,cell in enumerate(notebook['cells']):
                    if cell['cell_type']!='code':continue
                    widget=widgets.nth(index);editor=widget.locator('.cm-content')
                    editor.fill(authored[cell['id']]);editor.click()
                    page.get_by_role('button',name='Run this cell and advance (⇧ ⏎)',exact=True).click()
                    expect(widget.locator('.jp-InputPrompt')).to_have_text(__import__('re').compile(r'\[\s*\d+\s*\]\s*:'),timeout=60000)
                    expect(page.get_by_role('button',name='Python 3 (ipykernel) | Idle',exact=True)).to_be_visible(timeout=60000)
                page.keyboard.press('ControlOrMeta+s')
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    actual=json.loads((work/NHANES.files[0]).read_text())
                    codes=[c for c in actual['cells'] if c['cell_type']=='code']
                    if all(type(c['execution_count']) is int for c in codes):break
                    page.wait_for_timeout(100)
                assert all(type(c['execution_count']) is int and not any(o['output_type']=='error' for o in c['outputs']) for c in codes)
                assert all(''.join(c['source'])==authored[c['id']] for c in codes)
                assert any('NHANES_NATIVE_OK 192' in ''.join(o.get('text',[])) for c in codes for o in c['outputs'])
                report['observed_output']='NHANES_NATIVE_OK 192'
                files=saved_files(work,profile=NHANES);report['saved_manifest']=manifest(files)
                if args.verify_studio_ui:
                    saved_at = (work / NHANES.files[0]).stat().st_mtime
                    assert saved_at >= tutor.expires
                    report['studio_ui']['notebook_saved_after_expiry'] = True
                report['passed'].append('native-python-NHANES-execution-and-save')
                page.screenshot(path=str(root/'notebook-saved.png'),full_page=True)
                # Wrong numeric output must fail the real network-off Cairn checker.
                wrong=root/'wrong-output';shutil.copytree(work,wrong)
                lines=(wrong/NHANES.files[1]).read_text().splitlines();cells=lines[1].split(',');cells[3]='9999';lines[1]=','.join(cells)
                (wrong/NHANES.files[1]).write_text('\n'.join(lines)+'\n')
                bad=submit_info511_attempt(course,bob,alice,peer,wrong,learner_username='bob',
                    expected_actions={n:'create' for n in NHANES.files},profile=NHANES)
                end=time.monotonic()+90
                while time.monotonic()<end:
                    detail=req(course,bob,'GET','/me/work/'+peer['submission_id'])
                    grades=[g for g in detail.get('history') or [] if g.get('submission_revision')==bad['commit']]
                    if grades:break
                    time.sleep(.25)
                assert len(grades)==1 and grades[0]['score']==0 and grades[0]['max_score']==4, 'wrong-output grading must execute and return 0/4'
                assert len(detail['tests'])==1 and detail['tests'][0]['passed'] is False
                assert 'source_value' in json.dumps(detail['tests']), 'wrong output must fail for its changed source value'
                report['wrong_output']={'grade':grades[0],'tests':detail['tests']}
                report['passed'].append('wrong-numeric-output-fails-real-pinned-checker')
                def bridge(context,selected):
                    with page.expect_popup() as opened:page.get_by_role('link',name='Submit saved files to Cairn',exact=True).click()
                    picker=opened.value
                    if args.verify_studio_ui:
                        expect(picker.locator('html')).to_have_attribute('data-studio-theme', 'dark')
                    for name in NHANES.files:picker.get_by_role('checkbox',name=name,exact=False).check()
                    picker.screenshot(path=str(root/'saved-file-picker.png'),full_page=True)
                    if args.verify_studio_ui:
                        picker.set_viewport_size({'width':320,'height':820})
                        assert picker.evaluate('document.documentElement.scrollWidth === innerWidth')
                    picker.get_by_role('button',name='Prepare saved files',exact=True).click()
                    if args.verify_studio_ui:
                        expect(picker.get_by_role('button',name='Continue in Cairn')).to_be_focused()
                        assert picker.evaluate('document.documentElement.scrollWidth === innerWidth')
                    with context.expect_page() as received:picker.get_by_role('button',name='Continue in Cairn',exact=True).click()
                    receiver=received.value
                    if args.verify_studio_ui:
                        expect(receiver.locator('html')).to_have_attribute('data-studio-theme', 'dark')
                        report['passed'].append('appearance-preserved-through-cross-origin-handoff')
                    receiver.get_by_label('Assignment',exact=True).select_option(selected['submission_id'])
                    receiver.get_by_role('button',name='Receive saved files from Waypoint',exact=True).click()
                    expect(receiver.locator('#bridge-status')).to_contain_text('Nothing has been submitted yet.')
                    return receiver
                receipt=submit_info511_attempt(course,alice,bob,item,work,learner_username='alice',
                    expected_actions={n:'create' for n in NHANES.files},notebook_page=bridge,profile=NHANES,
                    verify_reflow=args.verify_studio_ui)
                report['receipt']=receipt;report['correctness']=await_grade(course,alice,item['submission_id'],receipt['commit'],profile=NHANES)
                if args.verify_studio_ui:
                    assert time.time() >= tutor.expires
                    assert tutor.counts() == report['studio_ui']['control_counts_at_expiry']
                    report['passed'] += ['studio-320-keyboard-pane-switch',
                                         'cairn-320-keyboard-preview-and-committed-receipt',
                                         'workspace-save-and-cairn-submit-after-tutor-expiry']
                for name,raw in files.items():
                    repository=item['repo_web_url'].removeprefix(course.forge+'/')
                    saved=course.api('GET','/repos/'+repository+'/contents/'+name+'?ref='+receipt['commit'],user='alice')
                    assert base64.b64decode(saved['content'])==raw
                report['passed']+=['explicit-four-file-native-bridge-and-byte-exact-receipt','changed-preview-payload-refused-409']
                restart_receipt(course,browser,alice,item['submission_id'],receipt)
                report['passed'].append('receipt-persists-after-restart')
                if args.operator_review_seconds:
                    if tutor:
                        report['operator_review'] = tutor.review(browser,page,args.operator_review_seconds,started+600)
                    else:
                        page.bring_to_front();report['operator_review']=wait_for_review(browser,page,args.operator_review_seconds)
                if args.live_review_seconds:
                    report['live_review'] = tutor.review(browser, page, args.live_review_seconds, started+600)
            finally:browser.close()
    except (Exception, KeyboardInterrupt) as exc:
        report['failed'].append(type(exc).__name__+': '+str(exc)[:500]);private(root/'failure.txt',traceback.format_exc())
    finally:
        signal.alarm(0)
        for proc in reversed(processes):
            if proc.poll() is None:
                os.killpg(proc.pid,signal.SIGTERM)
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=5)
        for log in logs:log.close()
        if tutor:
            try:
                tutor.close()
                report['tutoring'] = tutor.evidence()
                if tutor.live:
                    accounting = report['tutoring']['accounting']
                    report['model_transports'] = accounting['dispatch_claims']
                    if accounting['unknown_liability_usd']:
                        report['failed'].append('live provider usage remains unresolved; do not retry this window')
            except Exception as exc:
                report['failed'].append('tutoring cleanup: ' + type(exc).__name__)
        try:report['cleanup']=course.close() if course else True
        except Exception as exc:report['failed'].append('cleanup: '+type(exc).__name__)
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        private(root/'result.json',report);print(json.dumps(report,indent=2))
    return int(bool(report['failed']) or not report['cleanup'])


if __name__=='__main__':raise SystemExit(main())
