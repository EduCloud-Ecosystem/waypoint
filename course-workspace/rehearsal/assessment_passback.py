"""Synthetic assessment review and signed LMS rehearsal using real course identities.

No model calls or university records. Imported proposals are deterministic test data;
a browser-driven instructor decision publishes them. This tests plumbing, not quality.
"""
import copy
import csv
import io
import hashlib
import json
import re
import socket
import subprocess
import time


FEEDBACK = 'Synthetic rubric feedback: the explanation names the expected return value; add a boundary-case test.'
SOURCE = 'The function returns 5. This synthetic explanation omits a boundary-case test.\n'


def proposal(record, points=7):
    """Bind a reproducible imported proposal to the captured bytes and input digest."""
    document = record['document']
    artifact = document['artifacts'][0]
    segment = artifact['segments'][0]
    return {'submission_status': 'relevant_work', 'source': 'fixture',
            'model': 'none-no-model-call', 'prompt_version': 'rehearsal-v1',
            'input_digest': document['input_digest'], 'criteria': [{
                'criterion_id': 'reason', 'points': points, 'feedback': FEEDBACK,
                'uncertainty': 'Deterministic synthetic fixture; not an assessment quality evaluation.',
                'citations': [{'path': artifact['path'], 'sha256': artifact['sha256'],
                               'location': segment['location'], 'quote': segment['text']}]}]}


class AssessmentPassback:
    def __init__(self, course, classroom_id, lti_binary):
        import requests

        self.course = course
        self.classroom_id = classroom_id
        self.process = None
        self.log = None
        self.contexts = []
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{port}'
        self.directory = course.root / 'lti-private'
        self.log = (course.root / 'lti-simulator.log').open('w')
        self.process = subprocess.Popen([str(lti_binary), '--dir', str(self.directory),
            '--port', str(port), '--cairn-url', course.url, '--classroom', classroom_id],
            env={key: course.env[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL') if key in course.env},
            stdout=self.log, stderr=subprocess.STDOUT)
        try:
            for _ in range(100):
                if self.process.poll() is not None:
                    raise RuntimeError('synthetic LMS exited; inspect private simulator log')
                try:
                    if requests.get(self.url, timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('synthetic LMS startup timed out')
            course.env['CAIRN_LTI_CONFIG'] = str(self.directory / 'lti.json')
            course.env['CAIRN_ASSESSMENT_REVIEW'] = '1'
        except Exception:
            self.close()
            raise

    def login(self, browser, user, entry=None):
        context, page = self.course.login(browser, user, entry or (
            '/auth/login' if user == 'teacher' else '/student/login?host=forgejo'))
        self.contexts.append(context)
        return context, page

    def req(self, context, method, path, data=None, status=200):
        response = context.request.fetch(self.course.url + path, method=method, data=data,
                                        headers={'Origin': self.course.url}, timeout=45000)
        assert response.status == status, f'assessment rehearsal {method} {path}: {response.status}'
        return response.json() if response.body() else None

    def capture(self, teacher, sid):
        record = self.req(teacher, 'POST', f'/submissions/{sid}/assessments', {}, 201)
        assert record['status'] == 'collected'
        artifact = record['document']['artifacts'][0]
        assert artifact['path'] == 'response.md' and not artifact.get('issue')
        return record

    def source(self, teacher, learner, aid, sid, content):
        sub = next(s for s in self.req(teacher, 'GET', f'/assignments/{aid}/submissions') if s['id'] == sid)
        repo = sub['repo']['namespace'] + '/' + sub['repo']['name']
        result = self.course.put(repo, 'response.md', content, learner)
        revision = result['commit']['sha']
        for _ in range(90):
            # Wait for grading to finish as well: a later automated grade must not
            # race the reviewed grade that this scenario is about to publish.
            grades = self.req(self.learners[learner], 'GET', f'/me/work/{sid}')['history'] or []
            if any(
                    g.get('submission_revision') == revision for g in grades):
                return revision
            time.sleep(1)
        raise AssertionError('source commit webhook/grading did not settle')

    def panel(self, page, sid):
        page.goto(self.course.url + '/')
        page.get_by_title('Show submissions').click()
        panel = page.get_by_role('region', name='Assessment review', exact=True)
        panel.locator('label').filter(has_text=re.compile(r'^\s*Submission')).locator('select').select_option(sid)
        return panel

    def browser_review(self, teacher, page, sid, record, points=8):
        from playwright.sync_api import expect

        panel = self.panel(page, sid)
        # Find the immutable capture by its displayed revision. Several negative
        # probes may share that revision; only the collected card has an upload.
        card = panel.locator('article').filter(has=page.get_by_role('heading', name='Assessment: collected', exact=True))
        expect(card).to_have_count(1)
        card.get_by_label('Import proposal JSON').set_input_files({
            'name': 'synthetic-proposal.json', 'mimeType': 'application/json',
            'buffer': json.dumps(proposal(record)).encode()})
        pending = panel.locator('article').filter(has=page.get_by_role('heading', name='Assessment: pending', exact=True)).filter(has_text=record['revision'])
        expect(pending).to_have_count(1)
        pending.get_by_text('Captured evidence', exact=True).click()
        expect(pending.locator('pre').filter(has_text=SOURCE.strip())).to_be_visible()
        pending.get_by_label('Reviewed points', exact=True).fill(str(points))
        pending.get_by_label('Instructor review note', exact=True).fill(
            'Synthetic instructor reviewed source and adjusted the proposed score; this is not calibration evidence.')
        pending.get_by_role('button', name='Approve and publish grade', exact=True).click()
        expect(pending).to_have_count(0)
        rows = self.req(teacher, 'GET', f'/submissions/{sid}/assessments')
        approved = next(r for r in rows if r['id'] == record['id'])
        assert approved['status'] == 'approved' and approved['grade_id']
        assert approved['document']['proposal']['criteria'][0]['points'] == 7
        assert approved['document']['review']['criteria'][0]['points'] == points
        page.screenshot(path=str(self.course.root / f'review-{sid}-{points}.png'), full_page=True)
        return approved['grade_id']

    def connect(self, page, role, aid=None):
        from playwright.sync_api import expect

        page.goto(self.url + '/start?user=' + role)
        page.wait_for_url(self.course.url + '/lti/connect')
        if aid:
            page.get_by_label('Cairn assignment', exact=True).select_option(aid)
        page.get_by_role('button', name='Connect assignment' if aid else 'Connect my account', exact=True).click()
        expect(page.get_by_role('status')).to_have_text('Connected. You can close this page.')

    def delivery_panel(self, page, sid):
        page.goto(self.course.url + '/')
        page.get_by_title('Show submissions').click()
        panel = page.get_by_role('region', name='Brightspace grade delivery', exact=True)
        panel.locator('label').filter(has_text=re.compile(r'^\s*Submission')).locator('select').select_option(sid)
        return panel

    def scores(self, page):
        page.goto(self.url)
        return json.loads(page.locator('pre').inner_text())

    def run(self, browser, assignment_id, alice_submission_id):
        import requests
        from playwright.sync_api import expect

        course, aid = self.course, assignment_id
        teacher, page = self.login(browser, 'teacher')
        alice, alice_page = self.login(browser, 'alice')
        bob, bob_page = self.login(browser, 'bob', f'/assignments/{aid}/accept')
        self.learners = {'alice': alice, 'bob': bob}
        sid_a = alice_submission_id
        for _ in range(60):
            work = self.req(bob, 'GET', '/me/work')['work']
            submissions = self.req(teacher, 'GET', f'/assignments/{aid}/submissions')
            bob_ids = {s['id'] for s in submissions if s['username'] == 'bob'}
            matched = [w for w in work if w['submission_id'] in bob_ids]
            if matched and matched[0]['status'] == 'active':
                sid_b = matched[0]['submission_id']
                break
            time.sleep(1)
        else:
            raise AssertionError('Bob repository provisioning did not finish')
        assert sid_a != sid_b
        self.req(bob, 'GET', '/me/work/' + sid_a, status=404)
        capabilities = self.req(teacher, 'GET', '/assessment-capabilities')
        assert capabilities['review'] is True, 'enable CAIRN_ASSESSMENT_REVIEW before starting Cairn'
        assert capabilities['model_provider'] is False
        self.req(teacher, 'PUT', f'/assignments/{aid}/assessment-rubric', {
            'title': 'Synthetic reasoning review', 'paths': ['response.md'],
            'criteria': [{'id': 'reason', 'description': 'Explain the expected return value and boundary-case testing.', 'max_points': 10}]})
        self.connect(page, 'instructor', aid)
        self.connect(alice_page, 'student-a')
        self.connect(bob_page, 'student-b')
        # Signed learner A launch cannot also bind to Bob's existing Cairn identity.
        bob_page.goto(self.url + '/start?user=student-a')
        bob_page.wait_for_url(course.url + '/lti/connect')
        self.req(bob, 'POST', '/lti/connect', {}, status=409)
        self.connect(bob_page, 'student-b')
        revisions = {}
        for user, sid in [('alice', sid_a), ('bob', sid_b)]:
            revisions[user] = self.source(teacher, user, aid, sid, SOURCE)
        self.req(alice, 'POST', f'/submissions/{sid_a}/assessments', {}, status=401)
        # An unreviewed automated grade must never qualify for LMS delivery.
        assert self.req(teacher, 'GET', f'/submissions/{sid_a}/lti')['ready'] is False
        self.req(teacher, 'POST', f'/submissions/{sid_a}/lti', {'grade_id': 'unreviewed'}, status=409)
        unassessable = self.capture(teacher, sid_a)
        unscored = proposal(unassessable, None)
        unscored['criteria'][0]['citations'] = []
        self.req(teacher, 'POST', f"/assessments/{unassessable['id']}/proposal", unscored)
        self.req(teacher, 'POST', f"/assessments/{unassessable['id']}/review", {
            'action': 'approve', 'note': 'Attempt unresolved synthetic criterion',
            'criteria': unscored['criteria']}, status=400)
        assert self.req(teacher, 'GET', f'/submissions/{sid_a}/lti')['ready'] is False
        self.req(teacher, 'POST', f"/assessments/{unassessable['id']}/review", {
            'action': 'reject', 'note': 'Leave unassessable until instructor resolution; fixture rejected.', 'criteria': []})
        stale = self.capture(teacher, sid_a)
        valid = proposal(stale)
        invalid = copy.deepcopy(valid)
        invalid['input_digest'] = '0' * 64
        self.req(teacher, 'POST', f"/assessments/{stale['id']}/proposal", invalid, status=400)
        self.req(teacher, 'POST', f"/assessments/{stale['id']}/proposal", valid)
        revisions['alice'] = self.source(teacher, 'alice', aid, sid_a, SOURCE + 'Revision 2 adds a synthetic note.\n')
        self.req(teacher, 'POST', f"/assessments/{stale['id']}/review", {
            'action': 'approve', 'note': 'Attempt stale synthetic source', 'criteria': valid['criteria']}, status=409)
        # Keep the stale record as evidence; browser selection isolates the
        # current pending card by its immutable source revision.
        grades = {}
        for user, sid in [('alice', sid_a), ('bob', sid_b)]:
            record = self.capture(teacher, sid)
            assert record['revision'] == revisions[user]
            assert record['document']['artifacts'][0]['sha256'] == hashlib.sha256(
                (SOURCE + ('Revision 2 adds a synthetic note.\n' if user == 'alice' else '')).encode()).hexdigest()
            grades[user] = self.browser_review(teacher, page, sid, record)
            learner = self.learners[user]
            detail = self.req(learner, 'GET', f'/me/work/{sid}')
            assert detail['latest_grade']['score'] == 8
            assert any(a['criteria'][0]['feedback'] == FEEDBACK for a in detail['assessments'])
        for user, sid in [('alice', sid_a), ('bob', sid_b)]:
            if user == 'bob':
                response = requests.post(self.url + '/simulate', data={'action': 'drop'}, timeout=5)
                assert response.status_code == 200
            panel = self.delivery_panel(page, sid)
            panel.get_by_role('button', name='Send to Brightspace', exact=True).click()
            expect(panel.get_by_text('Delivery: verified (1 attempt).', exact=True)).to_be_visible(timeout=45000)
            status = self.req(teacher, 'GET', f'/submissions/{sid}/lti')['delivery']
            assert status['grade_id'] == grades[user] and status['attempts'] == 1 and status['verified_at']
        scores = self.scores(page)
        assert set(scores) == {'student-a', 'student-b'}
        assert all(s['scoreGiven'] == 8 and s['scoreMaximum'] == 10 for s in scores.values())
        assert all('comment' not in s and 'feedback' not in s for s in scores.values())
        timestamps = {u: s['timestamp'] for u, s in scores.items()}
        # A new instructor review may change a grade, but an intervening LMS edit
        # must be preserved rather than silently overwritten.
        updated = self.capture(teacher, sid_a)
        grades['alice'] = self.browser_review(teacher, page, sid_a, updated, points=9)
        assert requests.post(self.url + '/simulate', data={'action': 'edit'}, timeout=5).status_code == 200
        panel = self.delivery_panel(page, sid_a)
        panel.get_by_role('button', name='Send to Brightspace', exact=True).click()
        expect(panel.get_by_text('Delivery: conflict (0 attempts).', exact=True)).to_be_visible(timeout=45000)
        assert self.scores(page)['student-a']['scoreGiven'] == 63
        before = {sid: self.req(teacher, 'GET', f'/submissions/{sid}/lti') for sid in [sid_a, sid_b]}
        course.restart()
        teacher2, page2 = self.login(browser, 'teacher')
        for user, sid in [('alice', sid_a), ('bob', sid_b)]:
            after = self.req(teacher2, 'GET', f'/submissions/{sid}/lti')
            assert after == before[sid], 'grade or delivery state changed across Cairn restart'
            learner, student_page = self.login(browser, user)
            self.req(learner, 'GET', '/me/work/' + (sid_b if user == 'alice' else sid_a), status=404)
            detail = self.req(learner, 'GET', '/me/work/' + sid)
            assert detail['latest_grade']['source'] == 'instructor-reviewed'
            assert detail['latest_grade']['score'] == (9 if user == 'alice' else 8)
            student_page.goto(course.url + '/me')
            student_page.get_by_text('Per-test results & history', exact=True).click()
            expect(student_page.get_by_text(FEEDBACK, exact=True).first).to_be_visible()
            student_page.screenshot(path=str(course.root / f'{user}-reviewed-feedback.png'), full_page=True)
        duplicate = self.req(teacher2, 'POST', f'/submissions/{sid_b}/lti', {'grade_id': grades['bob']})
        assert duplicate['attempts'] == 1 and duplicate['status'] == 'verified'
        self.req(teacher2, 'POST', f'/submissions/{sid_a}/lti', {'grade_id': grades['alice']}, status=409)
        export_path = f'/classrooms/{self.classroom_id}/grades.csv'
        exported = teacher2.request.get(course.url + export_path)
        assert exported.status == 200 and 'text/csv' in exported.headers.get('content-type', '')
        rows = list(csv.DictReader(io.StringIO(exported.text())))
        assert len(rows) == 2
        assert {row['username']: (float(row['score']), float(row['max_score'])) for row in rows} == {
            'alice': (9, 10), 'bob': (8, 10)}
        assert {row['assignment'] for row in rows} == {'python-answer'}
        self.req(learner, 'GET', export_path, status=401)
        final_scores = self.scores(page2)
        assert final_scores['student-a']['scoreGiven'] == 63
        assert final_scores['student-b']['timestamp'] == timestamps['student-b']
        report = {'scope': 'synthetic imported proposals, real Forgejo identities, loopback signed LMS',
            'model_calls': 0, 'learners': 2, 'reviewed_initial_score': 8, 'maximum': 10,
            'normal_delivery_attempts': 1, 'lost_response_delivery_attempts': 1,
            'conflicting_remote_score_preserved': 63,
            'checks': ['captured-current-repository-bytes', 'source-hash-bound-import',
                'unassessable-publication-refused', 'stale-input-digest-refused', 'stale-source-approval-refused',
                'learner-assessment-mutation-denied', 'browser-instructor-score-adjustment',
                'reviewed-feedback-visible-to-owner-only', 'signed-instructor-assignment-mapping',
                'signed-two-learner-bindings', 'cross-identity-binding-refused',
                'unreviewed-passback-refused', 'normal-passback-readback', 'lost-response-readback-one-attempt',
                'external-score-conflict-preserved', 'review-and-delivery-persist-after-restart',
                'duplicate-passback-after-restart-no-new-attempt',
                'current-reviewed-grades-csv-export', 'learner-grade-export-denied']}
        return report

    def close(self):
        failures = []
        for context in self.contexts:
            try:
                context.close()
            except Exception as error:
                failures.append(type(error).__name__)
        self.contexts.clear()
        if self.process:
            try:
                if self.process.poll() is None:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            except Exception as error:
                failures.append(type(error).__name__)
        if self.log:
            try:
                self.log.close()
            except Exception as error:
                failures.append(type(error).__name__)
        if failures:
            raise RuntimeError('assessment fixture cleanup failed: ' + ', '.join(failures))
