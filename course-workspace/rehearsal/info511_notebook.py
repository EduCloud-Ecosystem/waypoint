"""Opt-in native synthetic Jupyter/Bob journey; operator owns all runtimes.

No provisioning, SDK calls, hidden execution or background submission. Native
Jupyter authentication is deliberately distinct from Hub/institutional SSO.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK_OPERATOR = ROOT / 'classroom/info511-lab03-q4-v1/operator'
sys.path[:0] = [str(ROOT / 'classroom'), str(ROOT / 'course')]
from info511_package import FILES
from waypoint_cairn import open_root, open_beneath, read_saved


class NotebookFailure(RuntimeError):
    """Fixed content-free failure codes, never raw browser/API exceptions."""


def require(value, code):
    if not value:
        raise NotebookFailure(code)


def private_bytes(path, limit=32768):
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'private_path_absolute_required')
    root = open_root(path.parent)
    try:
        fd = open_beneath(root, [path.name])
    finally:
        os.close(root)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                not info.st_mode & 0o077 and info.st_size <= limit, 'private_input_permissions_or_size')
        with os.fdopen(fd,'rb',closefd=False) as stream:
            body = stream.read(limit+1)
        require(len(body) <= limit,'private_input_size')
        return body
    finally:
        os.close(fd)


def origin(value):
    require(isinstance(value,str) and bool(re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}',value)), 'native_loopback_origin_required')
    require(1024 <= urlsplit(value).port <= 65535,'native_loopback_port')
    return value


def reviewed_authored_files():
    return {name:(NOTEBOOK_OPERATOR / 'notebook-bob' / name).read_bytes()
            for name in ('assistance.md','explanation.md')}


def require_notebook_fixture(fixture):
    """Keep the declared live-help scope source-authored, before any live work."""
    expected = manifest(reviewed_authored_files())
    try:
        actual = fixture['learners']['bob']['artifacts']
    except (KeyError, TypeError):
        raise NotebookFailure('reviewed_notebook_authored_fixture_required') from None
    require(isinstance(actual,dict) and all(actual.get(name)==entry for name,entry in expected.items()),
            'reviewed_notebook_authored_fixture_required')


def require_saved_authored_files(directory):
    actual = saved_files(directory)
    require(all(actual.get(name)==body for name,body in reviewed_authored_files().items()),
            'reviewed_notebook_saved_authored_files_required')


def read_contract(path, workspace, *, device_only=False, fixture=None, derivative=None):
    raw_contract = private_bytes(path)
    document = json.loads(raw_contract)
    require(isinstance(document,dict) and set(document)=={'version','bob','alice','turn_file'} and
            type(document['version']) is int and document['version']==1,'notebook_contract_shape')
    for user, fields in (('bob',{'origin','token_file','work_directory'}),('alice',{'origin','token_file'})):
        item = document[user]
        require(isinstance(item,dict) and set(item)==fields,'notebook_learner_contract')
        origin(item['origin'])
        token = private_bytes(item['token_file'],8192).decode().strip()
        require(bool(re.fullmatch(r'[A-Za-z0-9_-]{32,4096}',token)),'native_token_shape')
    require(not device_only and workspace==document['bob']['origin'],'notebook_workspace_binding')
    require(document['alice']['origin'] != document['bob']['origin'],'notebook_origins_separate')
    directory = Path(document['bob']['work_directory'])
    fd = open_root(directory); os.close(fd)
    turn = json.loads(private_bytes(document['turn_file']))
    require(isinstance(turn,dict) and set(turn)=={'source','question'} and
            all(isinstance(turn[key],str) and turn[key].strip() for key in turn) and
            len(turn['source'].encode())<=8192 and len(turn['question'].encode())<=8192,'reviewed_turn_shape')
    if fixture is not None:
        require(derivative is not None,'explicit_derived_fixture_output_required')
        require_notebook_fixture(fixture)
    if derivative is not None:
        target = Path(derivative)
        require(target.is_absolute() and not target.exists() and not target.is_symlink() and
                target.parent.is_dir() and fixture is not None,'fresh_derived_fixture_output_required')
        require(not target.with_name(target.name+'.provenance.json').exists(),'fresh_derived_fixture_provenance_required')
    return {**document,'turn':turn,'contract_sha256':hashlib.sha256(raw_contract).hexdigest()}


def saved_files(directory):
    return {entry['path']:base64.b64decode(entry['content_base64'],validate=True)
            for entry in read_saved(Path(directory),list(FILES))}


def manifest(files):
    return {name:{'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()} for name,raw in sorted(files.items())}


def derive_fixture(original, files, destination, contract_digest, original_digest):
    """Explicitly rebind nondeterministic executed bytes; keep judgments exact."""
    derived = copy.deepcopy(original)
    actual = manifest(files)
    before = copy.deepcopy(original['learners']['bob']['artifacts'])
    require(set(actual)==set(FILES) and set(before)==set(FILES),'derived_fixture_four_artifacts')
    for name in ('explanation.md','assistance.md'):
        require(actual[name]==before[name],'derived_fixture_authored_text_drift')
    for name in ('lab3-q4.ipynb','tidy-inflation.csv'):
        derived['learners']['bob']['artifacts'][name]=actual[name]
    raw = (json.dumps(derived,indent=2)+'\n').encode()
    previous = hashlib.sha256((json.dumps(original,sort_keys=True,separators=(',',':'))).encode()).hexdigest()
    provenance = {'kind':'declared-derived-automated-synthetic-fixture','human_review_performed':False,
        'assessment_quality_evaluated':False,'original_fixture_sha256':original_digest,
        'original_fixture_canonical_sha256':previous,
        'derived_fixture_sha256':hashlib.sha256(raw).hexdigest(),'contract_sha256':contract_digest,
        'bob_artifacts_before':before,'bob_actual_saved_manifest':actual,
        'changed_fields':['learners.bob.artifacts.lab3-q4.ipynb','learners.bob.artifacts.tidy-inflation.csv'],
        'points_quotes_rubric_actor_unchanged':True}
    target = Path(destination)
    for path, content in ((target,raw),(target.with_name(target.name+'.provenance.json'),
                                      (json.dumps(provenance,indent=2)+'\n').encode())):
        with open(path,'xb',opener=lambda name,flags:os.open(name,flags,0o600)) as stream: stream.write(content)
    return derived,provenance


def contents(page, origin_url, path='lab3-q4.ipynb'):
    return page.evaluate("""async ({origin,path}) => {
      const cookie=document.cookie.split(';').map(x=>x.trim()).find(x=>x.startsWith('_xsrf='));
      const response=await fetch(origin+'/api/contents/'+path,{credentials:'same-origin',cache:'no-store',redirect:'error',
        headers:{'X-XSRFToken':decodeURIComponent(cookie?.slice(6)||'')}});
      if(!response.ok) return {ok:false,status:response.status};
      return {ok:true,body:await response.json()};
    }""",{'origin':origin_url,'path':path})


def native_login(context, item, *, expect_belay=True):
    page=context.new_page()
    page.goto(item['origin']+('/lab/tree/lab3-q4.ipynb' if expect_belay else '/lab'),wait_until='domcontentloaded')
    field=page.locator('#password_input')
    if field.is_visible():
        field.fill(private_bytes(item['token_file'],8192).decode().strip())
        page.get_by_role('button',name='Log in',exact=True).click()
    page.locator('.jp-Notebook' if expect_belay else '.jp-LabShell').wait_for(timeout=60000)
    if expect_belay:
        page.get_by_role('button',name='Ask Belay about this code',exact=True).wait_for(timeout=60000)
    return page


def journey(course, learner, peer, item, contract):
    from playwright.sync_api import expect
    bob=contract['bob']; turn=contract['turn']; origin_url=bob['origin']
    require_saved_authored_files(bob['work_directory'])
    page=native_login(learner,bob)
    report={'scope':'standalone-synthetic-native-Jupyter-auth-not-Hub-or-institutional-SSO',
        'contract_sha256':contract['contract_sha256'],'real_students':False,
        'source_sha256':hashlib.sha256(turn['source'].encode()).hexdigest(),
        'job_posts':[],'accepted_job_submits':0,'prepared_fallback':False,
        'provider_attempts_independently_verified':False,'wrong_source_exercise_runtime_probe':'NOT_IMPLEMENTED'}
    current=contents(page,origin_url); require(current.get('ok'),'native_contents_unavailable')
    cells=current['body']['content']['cells']
    ids={cell.get('id'):index for index,cell in enumerate(cells)}
    require(all(name in ids for name in ('load-input','learner-work')),'stable_code_cells_required')
    require(all(cells[ids[name]].get('cell_type')=='code' and
                cells[ids[name]].get('execution_count') is None for name in ('load-input','learner-work')),
            'fresh_unexecuted_code_cells_required')
    widgets=page.locator('.jp-Notebook .jp-Cell')
    expect(widgets).to_have_count(len(cells),timeout=60000)
    load=widgets.nth(ids['load-input']); editor=load.locator('.cm-content')
    editor.fill(turn['source']); expect(editor).to_have_text(turn['source'])
    require_saved_authored_files(bob['work_directory'])
    with page.expect_popup() as opened:
        page.get_by_role('button',name='Ask Belay about this code',exact=True).click()
    tutor=opened.value
    expect(tutor.locator('#notebook-status')).to_contain_text('Unsaved notebook code captured',timeout=30000)
    expect(tutor.locator('#notebook-source')).to_have_text(turn['source'])
    expect(tutor.locator('#notebook-hash')).to_contain_text(report['source_sha256'])
    require(tutor.locator('#question').input_value()==turn['question'],'reviewed_question_mismatch')
    guidance=tutor.locator('#classroom-guidance'); guidance.wait_for(timeout=30000)
    granted=tutor.evaluate('window.BELAY_CLASSROOM_DEMO.request_id')
    require(bool(re.fullmatch(r'[A-Za-z0-9_-]{16,64}',granted)),'live_request_id_shape')
    report['request_id']=granted
    phase='prediction_required'
    def intercept(route):
        request=route.request
        if request.method!='POST': return route.continue_()
        payload=request.post_data_json
        require(isinstance(payload,dict) and payload.get('request_id')==granted and
                report['accepted_job_submits']==0,'unexpected_job_submit')
        response=route.fetch(timeout=30000,max_redirects=0)
        entry={'phase':phase,'status':response.status}
        report['job_posts'].append(entry)
        if response.status==202:
            require(phase=='permitted' and payload.get('stance')=='peer','unexpected_job_admission')
            report['accepted_job_submits']+=1
            return route.abort('failed')
        require(response.status=={'prediction_required':409,'hints_only':403}.get(phase),'policy_refusal_http')
        entry['code']=response.json().get('detail',{}).get('code')
        require(entry['code']==phase,'policy_refusal_code')
        route.fulfill(response=response)
    tutor.route('**/belay/api/api/sol/jobs',intercept)
    tutor.locator('#ask').click()
    expect(tutor.locator('.feedback-state').first).to_contain_text('Before asking for hints',timeout=30000)
    require(report['accepted_job_submits']==0,'prediction_admitted_job')
    def action(label):
        with tutor.expect_response(lambda response:response.request.method=='POST' and
                '/api/classroom/' in urlsplit(response.url).path) as result:
            guidance.get_by_role('button',name=label,exact=True).click()
        require(result.value.status==200,'private_action_not_confirmed')
    for label in ('I wrote my schema and observational-unit prediction','Declare: I used AI assistance',
                  'Enable optional tracking with this notice'):
        action(label)
    tutor.locator('#presenter-policy').click(); phase='hints_only'; tutor.locator('#ask').click()
    guidance.get_by_role('button',name='I opened this guidance',exact=True).wait_for(timeout=30000)
    require(report['accepted_job_submits']==0,'oracle_admitted_job')
    for label in ('I opened this guidance','Challenge this alert','I will retry with permitted hints'):
        action(label)
    phase='permitted'; tutor.locator('#ask').click()
    expect(tutor.locator('#ask')).to_have_text('Check request status',timeout=45000)
    require(report['accepted_job_submits']==1,'actual_lost_202_missing')
    tutor.reload(wait_until='domcontentloaded')
    for attempt in range(4):
        try:
            tutor.locator('.bubble-tutor').first.wait_for(timeout=90000)
            break
        except Exception:
            require(attempt<3 and tutor.locator('#ask').inner_text()=='Check request status',
                    'published_hint_not_recovered')
            tutor.locator('#ask').click()
    answer=tutor.locator('.request-record[data-request-id="'+granted+'"] .bubble-tutor')
    expect(answer).to_have_count(1); expect(answer).to_contain_text('Live AI')
    text=answer.inner_text(); require(not re.search(r'prepared\s+hint\s+card',text,re.I),'prepared_reply_rejected')
    require(len(report['job_posts'])==3 and report['accepted_job_submits']==1,'unexpected_repeat_dispatch')
    report['hint_sha256']=hashlib.sha256(text.encode()).hexdigest()
    tutor.screenshot(path=str(course.root/'bob-native-live-hint.png'),full_page=True)
    action('Pause optional tracking'); action('Revoke optional tracking and clear its records')
    for name in ('load-input','learner-work'):
        cell=widgets.nth(ids[name]); cell.locator('.cm-content').click(); page.keyboard.press('Shift+Enter')
        expect(cell.locator('.jp-InputPrompt')).to_have_text(re.compile(r'\[\s*\d+\s*\]\s*:'),timeout=60000)
    with page.expect_response(lambda response:response.request.method=='PUT' and
            urlsplit(response.url).path=='/api/contents/lab3-q4.ipynb') as saved:
        page.keyboard.press('Control+s')
    require(saved.value.status==200,'explicit_native_save_failed')
    state=contents(page,origin_url); require(state.get('ok'),'saved_notebook_unavailable')
    actual_cells={cell['id']:cell for cell in state['body']['content']['cells']}
    captured=actual_cells['load-input']['source']
    require((''.join(captured) if isinstance(captured,list) else captured)==turn['source'],
            'saved_source_differs_from_granted_capture')
    for name in ('load-input','learner-work'):
        cell=actual_cells[name]
        require(type(cell.get('execution_count')) is int and cell['execution_count']>0 and
                not any(output.get('output_type')=='error' for output in cell.get('outputs',[])),
                'actual_kernel_execution_not_established')
    files=saved_files(bob['work_directory']); report['saved_manifest']=manifest(files)
    marker=turn['source']+'\n# UNSAVED_CONNECTION_CONTROL\n'
    editor.fill(marker)
    failed=[]
    def offline(route):
        if route.request.method=='PUT': failed.append(True); route.abort('internetdisconnected')
        else: route.continue_()
    page.route('**/api/contents/lab3-q4.ipynb',offline)
    page.keyboard.press('Control+s')
    page.wait_for_function('document.querySelector(".jp-Dialog") !== null',timeout=30000)
    require(failed and saved_files(bob['work_directory'])==files,'failed_save_changed_saved_bytes')
    # Reconnect read requests; keep the explicit failed-save dialog/unsaved cell
    # intact. No retry/overwrite or hidden save is issued by this harness.
    page.unroute('**/api/contents/lab3-q4.ipynb',offline)
    dialog=page.locator('.jp-Dialog'); cancel=dialog.get_by_role('button',name='Cancel',exact=True)
    if cancel.count(): cancel.click()
    else: dialog.get_by_role('button',name='OK',exact=True).click()
    require(saved_files(bob['work_directory'])==files,'reconnect_changed_saved_bytes')
    report.update(actual_python_kernel=True,explicit_saved_execution_counts=True,
                  unsaved_snapshot_captured=True,failed_save_preserved_saved_bytes=True,
                  interruption_scope='one_blocked_notebook_save_request',
                  lost_202_same_request_recovered=True,policy_refusals_zero_admissions=True)
    peer_page=native_login(peer,contract['alice'],expect_belay=False)
    peer_token=private_bytes(contract['alice']['token_file'],8192).decode().strip()
    for path in ('/api/contents/lab3-q4.ipynb','/api/kernels','/belay/config'):
        response=peer.request.get(origin_url+path,headers={'Authorization':'token '+peer_token},max_redirects=0)
        require(response.status in (302,403,404),'cross_student_native_route_not_denied')
    report['cross_student_denied']=True
    peer_page.close()
    def bridge_page(context, assignment):
        require(saved_files(bob['work_directory'])==files,'saved_snapshot_changed_before_transfer')
        with page.expect_popup() as bridge:
            page.get_by_role('link',name='Submit saved files to Cairn',exact=True).click()
        selected=bridge.value
        for name in FILES: selected.get_by_role('checkbox',name=name,exact=False).check()
        selected.get_by_role('button',name='Prepare saved files',exact=True).click()
        with context.expect_page() as receiver:
            selected.get_by_role('button',name='Continue in Cairn',exact=True).click()
        received=receiver.value
        received.get_by_label('Assignment',exact=True).select_option(assignment['submission_id'])
        received.get_by_role('button',name='Receive saved files from Waypoint',exact=True).click()
        expect(received.locator('#bridge-status')).to_contain_text('Nothing has been submitted yet.')
        require(saved_files(bob['work_directory'])==files,'unsaved_marker_entered_transfer')
        report['actual_saved_four_file_bridge']=True
        return received
    return report,bridge_page,files
