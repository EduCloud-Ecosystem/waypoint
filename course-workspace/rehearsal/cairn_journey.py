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
import uuid

import requests
from playwright.sync_api import sync_playwright

FORGEJO_IMAGE = 'codeberg.org/forgejo/forgejo:16.0.5@sha256:cf5f5ae6acf2ababca0ee3d255705b83a47f35b25e07fc931d694d60664053fe'
SOLUTION = 'def answer():\n    return 5\n'


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
    def __init__(self, root, binary, workspace, image):
        self.root, self.binary, self.workspace, self.image = root, binary, workspace, image
        self.name = 'rehearsal-' + uuid.uuid4().hex[:10]
        self.forge = 'http://127.0.0.1:' + str(port())
        self.url = 'http://127.0.0.1:' + str(port())
        self.password = secrets.token_urlsafe(32)
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
        run('docker', 'volume', 'create', '--label', 'educloud.rehearsal=' + self.name, self.name)
        args = ['docker', 'run', '-d', '--name', self.name, '--label', 'educloud.rehearsal=' + self.name,
                '--memory', '512m', '--cpus', '1', '-p', self.forge.removeprefix('http://') + ':3000',
                '-v', self.name + ':/data']
        config = {'FORGEJO__security__INSTALL_LOCK': 'true', 'FORGEJO__database__DB_TYPE': 'sqlite3',
                  'FORGEJO__server__ROOT_URL': self.forge + '/', 'FORGEJO__server__DISABLE_SSH': 'true',
                  'FORGEJO__service__DISABLE_REGISTRATION': 'true', 'FORGEJO__service__REQUIRE_SIGNIN_VIEW': 'true',
                  'FORGEJO__oauth2__JWT_SIGNING_ALGORITHM': 'RS256'}
        for key, value in config.items():
            args += ['-e', key + '=' + value]
        run(*args, FORGEJO_IMAGE)
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
        spec = {'version': '1', 'image': self.image, 'tests': [
            {'name': 'instructor-answer', 'run': 'python /cairn-policy/check.py', 'points': 10}],
            'limits': {'memory_mb': 256, 'cpus': 1, 'network': 'none'}}
        self.put('course-test/template', 'grading.json', json.dumps(spec))
        self.put('course-test/template', 'check.py', "import sys\nsys.path.insert(0, '/work')\nfrom solution import answer\nassert answer() == 5\n")
        revision = self.api('GET', '/repos/course-test/template/commits?limit=1')[0]['sha']
        clean = {k: v for k, v in os.environ.items() if not k.startswith(('CAIRN_', 'GIT_'))}
        temp = self.root / 'grading'
        temp.mkdir(mode=0o755)
        self.env = dict(clean, CAIRN_LISTEN_ADDR=self.url.removeprefix('http://'), CAIRN_STORE='sqlite',
            CAIRN_SQLITE_PATH=str(self.root / 'cairn.db'), CAIRN_ADMIN_USERS='teacher', CAIRN_OPERATOR_HOST='forgejo',
            CAIRN_FORGEJO_BASE_URL=self.forge, CAIRN_FORGEJO_TOKEN=token, CAIRN_FORGEJO_GIT_USERNAME='teacher',
            CAIRN_FORGEJO_OAUTH_CLIENT_ID=client['client_id'], CAIRN_FORGEJO_OAUTH_CLIENT_SECRET=client['client_secret'],
            CAIRN_OAUTH_REDIRECT_URL=self.url + '/auth/callback', CAIRN_GRADER='container',
            CAIRN_GRADER_IMAGE=self.image, CAIRN_GRADER_MAX_TIMEOUT='20s', CAIRN_GRADER_MAX_MEMORY_MB='256',
            CAIRN_GRADER_MAX_CPUS='1', TMPDIR=str(temp))
        self.restart()
        return revision

    def restart(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=15)
            self.log.close()
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
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=15)
            self.log.close()
        subprocess.run(['docker', 'rm', '-f', self.name], capture_output=True)
        subprocess.run(['docker', 'volume', 'rm', self.name], capture_output=True)


def journey(root, binary, workspace, image, solution, browser, notebook=None, after_grading=None):
    course = Course(root, binary, workspace, image)
    try:
        revision = course.start()
        assert requests.get(course.url + '/classrooms', timeout=5).status_code == 401
        teacher, page = course.login(browser, 'teacher', '/auth/login')
        classroom = course.request(teacher, 'POST', '/classrooms', {'name': 'Synthetic Python/R course',
            'host': 'forgejo', 'host_namespace': 'course-test', 'join_policy': 'roster'}, 201)
        cid = classroom['id']
        for user in ('alice', 'bob'):
            course.request(teacher, 'POST', f'/classrooms/{cid}/roster', {'username': user}, 201)
        assignment = course.request(teacher, 'POST', f'/classrooms/{cid}/assignments', {
            'title': 'Python answer', 'slug': 'python-answer', 'type': 'individual', 'grading_spec': 'grading.json',
            'template': {'namespace': 'course-test', 'name': 'template', 'ref': revision}}, 201)
        aid = assignment['id']
        course.env['CAIRN_WORKSPACE_URLS'] = json.dumps({cid: workspace})
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
        course.put(repo, 'solution.py', solution, 'alice')
        # A learner-controlled rubric must never replace the pinned instructor policy.
        course.put(repo, 'grading.json', json.dumps({'tests': [{'name': 'forged', 'run': 'true', 'points': 999}]}), 'alice')
        course.request(alice, 'PATCH', f'/assignments/{aid}/grading-policy', {'template_commit': revision}, status=401)
        course.request(teacher, 'POST', f'/assignments/{aid}/grade')
        for _ in range(60):
            detail = course.request(alice, 'GET', '/me/work/' + item['submission_id'])
            if detail.get('latest_grade'):
                break
            time.sleep(1)
        else:
            raise AssertionError('grade did not arrive; inspect private cairn.log')
        assert detail['latest_grade']['score'] == 10 and detail['latest_grade']['max_score'] == 10
        assert detail['tests'][0]['name'] == 'instructor-answer' and detail['tests'][0]['passed']
        # Change the actual answer; a forged rubric must not turn it into a pass.
        course.put(repo, 'solution.py', 'def answer():\n    return 999\n', 'alice')
        course.request(teacher, 'POST', f'/assignments/{aid}/grade')
        for _ in range(60):
            detail = course.request(alice, 'GET', '/me/work/' + item['submission_id'])
            if len(detail['history']) == 2:
                break
            time.sleep(1)
        assert len(detail['history']) == 2 and detail['latest_grade']['score'] == 0
        assert detail['latest_grade']['max_score'] == 10
        page.goto(course.url + '/me')
        page.screenshot(path=str(root / 'student-grade.png'))
        report = {'cairn_revision': run('git', '-C', str(binary.parent), 'rev-parse', 'HEAD') if (binary.parent / '.git').exists() else None,
                  'cairn_binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                  'handoff_source': 'notebook' if notebook else 'synthetic-file-fixture',
                  'policy_revision': revision, 'solution_sha256': hashlib.sha256(solution.encode()).hexdigest(),
                  'passed_score': 10, 'failed_score': 0, 'max_score': 10,
                  'checks': ['operator-auth', 'roster-denial', 'student-admin-denial', 'own-grade-only',
                             'workspace-link', 'real-repository-upload', 'pinned-policy', 'pass-and-fail-grading']}
        (root / 'course-PASS.json').write_text(json.dumps(report, indent=2) + '\n')
        if after_grading:
            after_grading(browser)
        for ctx in (teacher, alice, bob, denied):
            ctx.close()
        print('PASS: real Forgejo OAuth, roster/role/grade isolation, file transfer, pinned 10/10 and 0/10 grading.', flush=True)
        return report
    finally:
        course.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cairn-binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--image', default='educloud-python-r:2026-10-06')
    args = parser.parse_args()
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            journey(args.output.resolve(), args.cairn_binary.resolve(), 'http://127.0.0.1:18000',
                    args.image, SOLUTION, browser)
        finally:
            browser.close()


if __name__ == '__main__':
    main()
