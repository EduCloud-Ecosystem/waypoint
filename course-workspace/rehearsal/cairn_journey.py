"""Real disposable Forgejo OAuth/repositories and Cairn grading; no production accounts."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import sys
from datetime import datetime, timezone
import uuid

import requests
from playwright.sync_api import sync_playwright
from webhook_relay import WebhookRelay

FORGEJO_IMAGE = 'codeberg.org/forgejo/forgejo:16.0.5@sha256:cf5f5ae6acf2ababca0ee3d255705b83a47f35b25e07fc931d694d60664053fe'
SOLUTION = 'def answer():\n    return 5\n'

if not __debug__:
    raise RuntimeError('pilot requires Python assertions; do not use optimization flags')


def run(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed with exit {result.returncode}')
    return result.stdout.strip()


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_http(url):
    for _ in range(120):
        try:
            if requests.get(url, timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError('local fixture did not become ready')


class Course:
    def __init__(self, root, binary, workspace, image, cairn_source, cairn_origin=None, *, template_files=None,
                 forgejo_image=FORGEJO_IMAGE, checkout_image='cairn-checkout:local'):
        self.root, self.binary, self.workspace, self.image = root, binary, workspace, image
        self.cairn_source = cairn_source
        self.template_files = template_files
        self.forgejo_image = forgejo_image
        self.checkout_image = checkout_image
        self.name = 'rehearsal-' + uuid.uuid4().hex[:10]
        self.forge = 'http://127.0.0.1:' + str(port())
        self.url = cairn_origin or 'http://127.0.0.1:' + str(port())
        self.password = secrets.token_urlsafe(32)
        self.relay = None
        self.network = self.name + "-webhook"
        self.process = None
        self.log = None
        self.env = None
        self.admin = requests.Session()
        self.admin.trust_env = False
        self.admin.auth = ('teacher', self.password)

    def api(self, method, path, data=None, user=None):
        response = self.admin.request(method, self.forge + '/api/v1' + path, json=data,
                                    auth=(user or 'teacher', self.password), timeout=30)
        if not response.ok:
            raise RuntimeError(f'Forgejo {method} {path} returned {response.status_code}')
        return response.json() if response.content else None

    def start(self):
        run('docker','network','create','--label','educloud.rehearsal='+self.name,self.network)
        gateway=json.loads(run('docker','network','inspect',self.network))[0]['IPAM']['Config'][0]['Gateway']
        self.relay=WebhookRelay(self.url,gateway)
        run('docker', 'volume', 'create', '--label', 'educloud.rehearsal=' + self.name, self.name)
        args = ['docker', 'run', '-d', '--name', self.name, '--label', 'educloud.rehearsal=' + self.name,
                '--network', self.network, '--memory', '512m', '--cpus', '1', '-p', self.forge.removeprefix('http://') + ':' + self.forge.rsplit(':', 1)[1],
                '-v', self.name + ':/data']
        config = {'FORGEJO__security__INSTALL_LOCK': 'true', 'FORGEJO__database__DB_TYPE': 'sqlite3',
                  'FORGEJO__server__ROOT_URL': self.forge + '/', 'FORGEJO__server__HTTP_PORT': self.forge.rsplit(':', 1)[1], 'FORGEJO__server__DISABLE_SSH': 'true',
                  'FORGEJO__service__DISABLE_REGISTRATION': 'true', 'FORGEJO__service__REQUIRE_SIGNIN_VIEW': 'true',
                  'FORGEJO__oauth2__JWT_SIGNING_ALGORITHM': 'RS256',
                  'FORGEJO__webhook__ALLOWED_HOST_LIST': gateway+',host.docker.internal'}
        for key, value in config.items():
            args += ['-e', key + '=' + value]
        if self.template_files is not None:
            args += ['--pull=never']  # CW1 uses pre-existing inspected images only.
        run(*args, self.forgejo_image)
        wait_http(self.forge + '/api/healthz')
        for user in ('teacher', 'alice', 'bob', 'mallory'):
            args = ['docker', 'exec', '--user', 'git', self.name, 'forgejo', '--config', '/data/gitea/conf/app.ini',
                    'admin', 'user', 'create', '--username', user, '--password', self.password,
                    '--email', user + '@synthetic.invalid', '--must-change-password=false']
            if user == 'teacher':
                args += ['--admin']
            run(*args)
        token = self.api('POST', '/users/teacher/tokens', {'name': 'rehearsal', 'scopes': ['all']})['sha1']
        client = self.api('POST', '/user/applications/oauth2', {
            'name': 'Cairn rehearsal', 'redirect_uris': [self.url + '/auth/callback'], 'confidential_client': True})
        self.api('POST', '/orgs', {'username': 'course-test', 'visibility': 'private'})
        self.api('POST', '/orgs/course-test/repos', {'name': 'template', 'private': True, 'template': True,
                                                  'auto_init': True, 'default_branch': 'main'})
        if self.template_files is None:
            spec = {'version': '1', 'image': self.image, 'tests': [
                {'name': 'instructor-answer', 'run': 'python /cairn-policy/check.py', 'points': 10}],
                'limits': {'memory_mb': 256, 'cpus': 1, 'network': 'none'}}
            self.put('course-test/template', 'grading.json', json.dumps(spec))
            self.put('course-test/template', 'check.py', "import sys\nsys.path.insert(0, '/work')\nfrom solution import answer\nassert answer() == 5\n")
        else:
            # Explicit synthetic fixture input, never browser-supplied policy.
            if not isinstance(self.template_files, dict) or 'grading.json' not in self.template_files:
                raise ValueError('instructor template requires a grading policy')
            for name, content in sorted(self.template_files.items()):
                if not isinstance(name, str) or not isinstance(content, str):
                    raise ValueError('text instructor template files required')
                self.put('course-test/template', name, content)
        revision = self.api('GET', '/repos/course-test/template/commits?limit=1')[0]['sha']
        clean = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'DOCKER_HOST', 'DOCKER_CONTEXT') if k in os.environ}
        # Fixture-only routing: bounded checkout shares the disposable Forgejo
        # network namespace, so the same loopback clone URL works on macOS/Linux.
        # All production acquisition limits and filesystem isolation remain in force.
        runtime = self.root / 'fixture-docker'
        runtime.write_text('#!' + sys.executable + '\nimport os, sys\na = sys.argv[1:]\n'
            + 'if a and a[0] == \"run\" and any(a[i:i+2] == [\"--label\", \"io.educloud.cairn.kind=acquisition\"] for i in range(len(a)-1)):\n'
            + '    a[1:1] = [\"--network\", ' + repr('container:' + self.name) + ']\n'
            + 'os.execvp(\"docker\", [\"docker\"] + a)\n')
        runtime.chmod(0o700)
        temp = self.root / 'grading'
        temp.mkdir(mode=0o755)
        self.env = dict(clean, CAIRN_LISTEN_ADDR=self.url.removeprefix('http://'), CAIRN_STORE='sqlite',
            CAIRN_SQLITE_PATH=str(self.root / 'cairn.db'), CAIRN_ADMIN_USERS='teacher', CAIRN_OPERATOR_HOST='forgejo',
            CAIRN_FORGEJO_BASE_URL=self.forge, CAIRN_FORGEJO_TOKEN=token, CAIRN_FORGEJO_GIT_USERNAME='teacher',
            CAIRN_FORGEJO_OAUTH_CLIENT_ID=client['client_id'], CAIRN_FORGEJO_OAUTH_CLIENT_SECRET=client['client_secret'],
            CAIRN_OAUTH_REDIRECT_URL=self.url + '/auth/callback', CAIRN_GRADER='container',
            CAIRN_WEB_DIR=str(self.cairn_source / 'web' / 'dist'), CAIRN_GRADER_RUNTIME=str(runtime),
            CAIRN_CHECKOUT_IMAGE=self.checkout_image, CAIRN_GRADER_IMAGE=self.image, CAIRN_GRADER_MAX_TIMEOUT='20s', CAIRN_GRADER_MAX_MEMORY_MB='256',
            CAIRN_GRADER_MAX_CPUS='1', TMPDIR=str(temp),
            CAIRN_WEBHOOK_BASE_URL=self.relay.origin, CAIRN_FORGEJO_WEBHOOK_SECRET=secrets.token_urlsafe(32))
        self.restart()
        return revision

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log:
            self.log.close()

    def restart(self):
        self.stop()
        self.log = (self.root / 'cairn.log').open('a')
        self.process = subprocess.Popen([str(self.binary)], env=self.env, cwd=self.root,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        wait_http(self.url + '/healthz')

    def put(self, repo, name, content, user='teacher'):
        path = '/repos/' + repo + '/contents/' + name
        existing = self.admin.get(self.forge + '/api/v1' + path, auth=(user, self.password), timeout=30)
        data = {'content': base64.b64encode(content.encode()).decode(), 'message': 'Synthetic coursework'}
        if existing.status_code == 200:
            data['sha'] = existing.json()['sha']
        return self.api('PUT' if 'sha' in data else 'POST', path, data, user)

    def login(self, browser, user, entry):
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.goto(self.forge + '/user/login')
        page.locator('input[name="user_name"]').fill(user)
        page.locator('input[name="password"]').fill(self.password)
        page.locator('button').filter(has_text='Sign in').click()
        page.wait_for_url(lambda u: '/user/login' not in u)
        page.goto(self.url + entry)
        if '/login/oauth/authorize' in page.url:
            page.get_by_role('button', name='Authorize Application').click()
        if user != 'mallory':
            page.wait_for_url(self.url + ('/' if user == 'teacher' else '/me'))
        else:
            page.wait_for_url(self.url + '/**')
        (self.root / (user + '-login.txt')).write_text(page.locator('body').inner_text())
        page.screenshot(path=str(self.root / (user + '-login.png')))
        return context, page

    def request(self, context, method, path, data=None, status=200):
        response = context.request.fetch(self.url + path, method=method, data=data)
        assert response.status == status, f'Cairn {method} {path}: {response.status}'
        return response.json() if response.body() else None

    def close(self):
        errors = []
        def attempt(action):
            try:
                return action()
            except Exception as exc:
                errors.append(type(exc).__name__)
                return None
        def command(*args):
            return run(*args, timeout=45)
        attempt(self.stop)
        def logs():
            result = subprocess.run(['docker', 'logs', self.name], capture_output=True, text=True, timeout=15)
            (self.root / 'forgejo.log').write_text(result.stdout + result.stderr)
        attempt(logs)
        if self.relay:
            attempt(self.relay.close)
            if self.relay.thread.is_alive():
                errors.append('remaining-relay')
        # Recover only Cairn resources scoped to this private work directory.
        if self.env:
            plan = self.root / 'runtime-recovery.json'
            attempt(lambda: command(str(self.binary), 'recover-runtime', '--work-dir', str(self.root / 'grading'), '--plan', str(plan)))
            if plan.exists():
                attempt(lambda: command(str(self.binary), 'recover-runtime', '--work-dir', str(self.root / 'grading'), '--apply', str(plan)))
            def verify_runtime_empty():
                observed = json.loads(command(str(self.binary), 'recover-runtime', '--work-dir', str(self.root / 'grading')))
                if not isinstance(observed, dict) or type(observed.get('resources')) is not list:
                    raise ValueError('runtime absence inventory is missing or invalid')
                if observed['resources']:
                    raise RuntimeError('Cairn runtime resources remain after recovery')
            attempt(verify_runtime_empty)
        def inventory(kind):
            listing = ('ps', '-aq') if kind == 'container' else (kind, 'ls', '-q')
            value = attempt(lambda: command('docker', *listing, '--filter', 'label=educloud.rehearsal=' + self.name))
            return value.splitlines() if value is not None else []
        for kind in ('container', 'volume', 'network'):
            for identity in inventory(kind):
                remove = ('rm', '-f') if kind == 'container' else (kind, 'rm')
                attempt(lambda remove=remove, identity=identity: command('docker', *remove, identity))
        for kind in ('container', 'volume', 'network'):
            if inventory(kind):
                errors.append('remaining-' + kind)
        if self.process and self.process.poll() is None:
            errors.append('remaining-process')
        if errors:
            raise RuntimeError('fixture cleanup failed: ' + ', '.join(errors))
        return True


def journey(root, binary, workspace, image, solution, browser, cairn_source, lti_binary, notebook=None, after_grading=None,
            submission_page=None, cairn_origin=None, classroom_ready=None):
    from assessment_passback import AssessmentPassback
    from course_load import run_load, verify_restart
    course = Course(root, binary, workspace, image, cairn_source, cairn_origin)
    assessment = None
    try:
        revision = course.start()
        assert requests.get(course.url + '/classrooms', timeout=5).status_code == 401
        teacher, page = course.login(browser, 'teacher', '/auth/login')
        classroom = course.request(teacher, 'POST', '/classrooms', {'name': 'Synthetic Python/R course',
            'host': 'forgejo', 'host_namespace': 'course-test', 'join_policy': 'roster'}, 201)
        cid = classroom['id']
        assessment = AssessmentPassback(course, cid, lti_binary)
        for user in ('alice', 'bob'):
            course.request(teacher, 'POST', f'/classrooms/{cid}/roster', {'username': user}, 201)
        assignment = course.request(teacher, 'POST', f'/classrooms/{cid}/assignments', {
            'title': 'Python answer', 'slug': 'python-answer', 'type': 'individual', 'grading_spec': 'grading.json',
            'template': {'namespace': 'course-test', 'name': 'template', 'ref': revision}}, 201)
        aid = assignment['id']
        course.env['CAIRN_WORKSPACE_URLS'] = json.dumps({cid: workspace})
        course.env['CAIRN_SUBMISSIONS'] = 'true'
        course.env['CAIRN_SUBMISSION_CLASSROOMS'] = cid
        if classroom_ready:
            operator_environment = classroom_ready(cid, workspace)
            assert operator_environment == {
                'CAIRN_SUBMISSIONS': 'true', 'CAIRN_SUBMISSION_CLASSROOMS': cid}
            assert json.loads(course.env['CAIRN_WORKSPACE_URLS']) == {cid: workspace}
            course.env.update(operator_environment)
        course.restart()
        teacher.close()
        teacher, _ = course.login(browser, 'teacher', '/auth/login')
        alice, page = course.login(browser, 'alice', f'/assignments/{aid}/accept')
        assert course.request(alice, 'GET', '/classrooms', status=401)['error']
        for _ in range(60):
            work = course.request(alice, 'GET', '/me/work')['work']
            if work and work[0]['status'] == 'active':
                break
            if work and work[0]['status'] == 'failed':
                raise AssertionError('repository provisioning failed; inspect private cairn.log')
            time.sleep(1)
        else:
            raise AssertionError('repository provisioning timed out')
        item = work[0]
        assert item['workspace_url'] == workspace
        page.reload()
        link = page.get_by_role('link', name='Open Python/R workspace')
        assert link.get_attribute('href') == workspace
        page.screenshot(path=str(root / 'student-workspace-link.png'))
        if notebook:
            # The browser follows Cairn's real navigation link into independent Hub auth.
            solution = notebook(alice, page, link)
        assert solution == SOLUTION, 'notebook handoff content differs'
        bob, _ = course.login(browser, 'bob', '/student/login?host=forgejo')
        course.request(bob, 'GET', '/me/work/' + item['submission_id'], status=404)
        denied, denied_page = course.login(browser, 'mallory', f'/assignments/{aid}/accept')
        assert 'not on roster' in denied_page.text_content('body')
        sub = course.request(teacher, 'GET', f'/assignments/{aid}/submissions')[0]
        repo = sub['repo']['namespace'] + '/' + sub['repo']['name']
        from submission_handoff import first_submission, verify_receipt_after_restart
        handoff = first_submission(course, alice, bob, item, solution, submission_page)
        first = handoff['commit']
        # Clone and push as the learner; Forgejo must deliver the registered hook.
        checkout=root/'student-repo'
        git_env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        basic=base64.b64encode(('alice:'+course.password).encode()).decode()
        git_env.update(GIT_TERMINAL_PROMPT='0',GIT_CONFIG_COUNT='1',
                       GIT_CONFIG_KEY_0='http.extraHeader',GIT_CONFIG_VALUE_0='Authorization: Basic '+basic)
        run('git','clone',course.forge+'/'+repo+'.git',str(checkout),env=git_env)
        def git(*args): return run('git','-C',str(checkout),*args,env=git_env)
        git('config','user.name','Synthetic Alice'); git('config','user.email','alice@synthetic.invalid')
        def push(content, message):
            (checkout/'solution.py').write_text(content)
            (checkout/'grading.json').write_text(json.dumps({'tests':[{'name':'forged','run':'true','points':999}]}))
            git('add','solution.py','grading.json'); git('commit','-m',message)
            revision=git('rev-parse','HEAD'); git('push','origin','HEAD')
            return revision
        course.request(alice,'PATCH',f'/assignments/{aid}/grading-policy',{'template_commit':revision},status=401)
        expected={}; started=time.monotonic()
        expected[first]=10
        # A rapid sequence creates a bounded queue; each score must match its own commit.
        for n in range(5):
            answer=999 if n%2==0 else 5
            commit=push(f'def answer():\n    return {answer}\n# attempt {n}\n',f'Synthetic attempt {n}')
            expected[commit]=10 if answer==5 else 0
        for _ in range(120):
            detail=course.request(alice,'GET','/me/work/'+item['submission_id'])
            if len(detail['history'] or [])==len(expected): break
            time.sleep(1)
        else: raise AssertionError('automatic push grading did not drain the bounded queue')
        history={grade['submission_revision']:grade['score'] for grade in detail['history']}
        assert history==expected, 'a push was graded against the wrong submission revision'
        assert all(grade['max_score']==10 for grade in detail['history'])
        elapsed=time.monotonic()-started
        deliveries=course.relay.for_revision(commit)
        assert deliveries and deliveries[-1][2]==202, 'no actual Forgejo push delivery was accepted'
        body,headers,_=deliveries[-1]
        # Repeat a real signed delivery concurrently, including after Cairn restarts.
        from concurrent.futures import ThreadPoolExecutor
        def replay(_):
            return requests.post(course.url+'/webhooks/forgejo',data=body,headers=headers,timeout=15).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(replay,range(12)))==[202]*12
        bad=dict(headers)
        for key in list(bad):
            if 'signature' in key.lower(): bad[key]='invalid'
        assert requests.post(course.url+'/webhooks/forgejo',data=body,headers=bad,timeout=15).status_code==401
        # A non-default branch is real Git activity but must not publish a course grade.
        git('checkout','-b','draft')
        (checkout/'solution.py').write_text('def answer():\n    return -1\n')
        git('add','solution.py'); git('commit','-m','Draft branch')
        draft=git('rev-parse','HEAD'); git('push','origin','draft')
        for _ in range(30):
            if course.relay.for_revision(draft): break
            time.sleep(1)
        assert course.relay.for_revision(draft)[-1][2]==204
        old_alice=alice
        course.restart()
        alice,page=course.login(browser,'alice','/student/login?host=forgejo')
        verify_receipt_after_restart(course, alice, item['submission_id'], handoff)
        assert replay(0)==202
        time.sleep(3)
        detail=course.request(alice,'GET','/me/work/'+item['submission_id'])
        assert len(detail['history'])==len(expected), 'duplicate delivery produced an extra grade'
        assert detail['latest_grade']['score']==0 and detail['latest_grade']['submission_revision']==commit
        assert detail['tests'][0]['name']=='instructor-answer' and not detail['tests'][0]['passed']
        page.goto(course.url + '/me')
        page.get_by_text('Per-test results & history', exact=True).click()
        page.get_by_text('instructor-answer', exact=False).wait_for(state='visible')
        page.screenshot(path=str(root / 'student-grade.png'),full_page=True)
        report = {'cairn_revision': run('git', '-C', str(binary.parent), 'rev-parse', 'HEAD') if (binary.parent / '.git').exists() else None,
                  'cairn_binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                  'handoff_source': 'notebook' if notebook else 'synthetic-file-fixture',
                  'policy_revision': revision, 'solution_sha256': hashlib.sha256(solution.encode()).hexdigest(),
                  'push_count':len(expected),'queue_drain_seconds':round(elapsed,3),'duplicate_deliveries':13,
                  'passed_score': 10, 'failed_score': 0, 'max_score': 10,
                  'checks': ['operator-auth', 'roster-denial', 'student-admin-denial', 'own-grade-only',
                             'workspace-link', 'real-git-push-webhook', 'pinned-policy', 'pinned-submission-revisions', 'pass-and-fail-grading',
                             'six-revision-grading', 'duplicate-replay-after-restart', 'bad-signature-denied', 'draft-branch-ignored'] + handoff['checks']}
        if after_grading:
            after_grading(browser)
        report['assessment'] = assessment.run(browser, aid, item['submission_id'])
        load_teacher, _ = course.login(browser, 'teacher', '/auth/login')
        try:
            report['load'] = run_load(course, load_teacher, browser, cid, aid)
        finally:
            load_teacher.close()
        course.restart()
        verify_restart(course, browser, report['load'])
        for ctx in (teacher, alice, old_alice, bob, denied):
            ctx.close()
        return report
    finally:
        try:
            if assessment:
                assessment.close()
        finally:
            course.close()


def public_metrics(report):
    from course_load import public_observability_metrics
    metrics = {'push_count': report['push_count'], 'duplicate_deliveries': 13,
               'course_learners': 15, 'model_calls': 0,
               'queue_drain_seconds': report['queue_drain_seconds']}
    for phase in report['load']['phases']:
        prefix = 'load_' + str(phase['concurrent_learners']) + '_'
        metrics.update({prefix + key: value for key, value in public_observability_metrics(phase).items()})
        for key in ('requests', 'errors', 'elapsed_seconds', 'p50_ms', 'p95_ms', 'max_ms'):
            metrics[prefix + key] = phase[key]
        for route, result in phase['routes'].items():
            for key in ('requests', 'errors', 'p50_ms', 'p95_ms', 'max_ms'):
                metrics[prefix + route + '_' + key] = result[key]
        for point in ('before', 'after'):
            resources = phase['resources_' + point]
            for key in ('cairn_rss_kib_point_sample', 'sqlite_bytes'):
                if type(resources[key]) is int:
                    metrics[prefix + point + '_' + key] = resources[key]
            queue = resources['queue_status_counts']
            allowed = {'provisioning_jobs': ('pending', 'in_progress', 'succeeded', 'failed'),
                       'grading_runs': ('pending', 'running', 'completed', 'failed')}
            if not isinstance(queue, dict) or set(queue) != set(allowed):
                raise ValueError('queue snapshot is missing expected tables')
            for table, statuses in allowed.items():
                counts = queue[table]
                if not isinstance(counts, dict) or not set(counts) <= set(statuses):
                    raise ValueError('queue snapshot contains an unknown status')
                for status in statuses:
                    count = counts.get(status, 0)
                    if type(count) is not int or count < 0:
                        raise ValueError('queue counts must be nonnegative integers')
                    metrics[prefix + point + '_' + table + '_' + status] = count
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cairn-binary', type=Path, required=True)
    parser.add_argument('--cairn-source', type=Path, required=True)
    parser.add_argument('--lti-binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', default='educloud-python-r:2026-10-06')
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    from evidence import collect_provenance, write_pass, LOCAL_PHASES
    started = datetime.now(timezone.utc).isoformat()
    timer = time.monotonic()
    images = {'course': args.image, 'checkout': 'cairn-checkout:local', 'forgejo': FORGEJO_IMAGE}
    run('docker', 'pull', FORGEJO_IMAGE)
    provenance = collect_provenance(args.cairn_source, args.cairn_binary, args.lti_binary, images=images)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            report = journey(args.output.resolve(), args.cairn_binary.resolve(), 'http://127.0.0.1:18000',
                    args.image, SOLUTION, browser, args.cairn_source.resolve(), args.lti_binary.resolve())
        finally:
            browser.close()
    assert provenance == collect_provenance(args.cairn_source, args.cairn_binary, args.lti_binary, images=images), 'inputs changed during pilot'
    write_pass(args.output, {'provenance': provenance, 'phases': {p: True for p in LOCAL_PHASES},
        'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
        'duration_seconds': time.monotonic() - timer, 'checks': report['checks'] + report['assessment']['checks'] + report['load']['checks'],
        'metrics': public_metrics(report)},
        scope='local-subset', cleanup_verified=True)
    print('PASS: local synthetic course pilot; all phases and cleanup verified.', flush=True)


if __name__ == '__main__':
    main()
