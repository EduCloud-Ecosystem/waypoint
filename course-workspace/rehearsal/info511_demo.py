# SPDX-License-Identifier: Apache-2.0
"""Opt-in CW1 synthetic submission/review walkthrough, never automatic acceptance.

Requires pre-existing local images and binaries, a fresh admitted runtime window,
and explicit interactive or labeled synthetic fixture review. The optional native
notebook contract uses operator-prestarted isolated synthetic services; defaults
do not execute notebooks or call models.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time

from cairn_journey import Course, FORGEJO_IMAGE, run
from classroom_transfer import submit_info511_attempt
from info511_review_fixture import LABEL, TASK, review_inputs
from assignment_profiles import LEGACY, get_profile
from info511_policy import template_files
from playwright.sync_api import sync_playwright

PACKAGE = LEGACY.package
FILES = LEGACY.files


def read_json(path, limit=1024 * 1024):
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('bounded JSON input exceeded')
    return json.loads(raw)


def req(course, session, method, path, data=None, status=200):
    response = session.request.fetch(course.url + path, method=method, data=data,
                                     headers={'Origin': course.url}, timeout=30000)
    if response.status != status:
        raise RuntimeError(f'CW1 endpoint returned {response.status}, expected {status}')
    return response.json() if response.body() else None


def active_work(course, learner, *, profile=LEGACY):
    profile = get_profile(profile)
    end = time.monotonic() + 60
    while time.monotonic() < end:
        rows = req(course, learner, 'GET', '/me/work')['work']
        rows = [row for row in rows if row.get('assignment_slug') == profile.task]
        if len(rows) > 1:
            raise RuntimeError('ambiguous synthetic assignment binding')
        if rows and rows[0]['status'] == 'active':
            return rows[0]
        if rows and rows[0]['status'] == 'failed':
            raise RuntimeError('synthetic repository preparation failed')
        time.sleep(.25)
    raise RuntimeError('synthetic repository preparation did not finish')


def await_grade(course, learner, sid, commit, *, profile=LEGACY):
    profile = get_profile(profile)
    end = time.monotonic() + 90
    while time.monotonic() < end:
        detail = req(course, learner, 'GET', '/me/work/' + sid)
        grades = [row for row in detail.get('history') or [] if row.get('submission_revision') == commit]
        if grades:
            if len(grades) != 1 or grades[0]['score'] != 4 or grades[0]['max_score'] != 4:
                raise RuntimeError('submitted CSV did not establish the expected four-point correctness result')
            tests = detail.get('tests') or []
            if len(tests) != 1 or tests[0]['name'] != profile.correctness_test or tests[0]['passed'] is not True:
                raise RuntimeError('expected pinned correctness check is absent or failed')
            return {'grade': grades[0], 'tests': tests}
        time.sleep(.25)
    raise RuntimeError('correctness-only grading did not settle for the submitted revision')


def operator_review(course, teacher, sid, receipt, directory, user, *, fixture=None, learner=None, profile=LEGACY):
    """Default human review, or separately opted-in synthetic API gate exercise."""
    profile = get_profile(profile)
    selected_files = profile.files
    record = req(course, teacher, 'POST', f'/submissions/{sid}/assessments', {}, 201)
    document = record['document']
    if record['status'] != 'collected' or record['revision'] != receipt['commit'] or record['submission_id'] != sid:
        raise RuntimeError('capture is blocked or does not match the submitted receipt')
    actual = {a['path']: (a['sha256'], a['size']) for a in document['artifacts'] if not a.get('issue')}
    expected = {a['path']: (a['sha256'], a['bytes']) for a in receipt['changes']}
    if len(document['artifacts']) != len(selected_files) or actual != expected or set(actual) != set(selected_files):
        raise RuntimeError('capture changed, omitted or could not extract a submitted artifact')
    # Export only the four captured originals, never the operator solution tree,
    # private judgments, tutor history, or mutable saved-home snapshot.
    exported = directory / (user + '-submitted-work')
    exported.mkdir(mode=0o700)
    export_hashes = {}
    for artifact in document['artifacts']:
        raw = base64.b64decode(artifact['original'], validate=True)
        if len(raw) > 256 * 1024 or len(raw) != artifact['size'] or hashlib.sha256(raw).hexdigest() != artifact['sha256']:
            raise RuntimeError('captured original does not match the immutable receipt')
        target = exported / artifact['path']  # Path already equals one of FILES.
        with target.open('xb') as stream:
            stream.write(raw)
        target.chmod(0o600)
        reopened = target.read_bytes()
        if reopened != raw:
            raise RuntimeError('submitted work export did not reopen identically')
        export_hashes[artifact['path']] = hashlib.sha256(reopened).hexdigest()
    capture = directory / (user + '-captured-evidence.json')
    capture.write_text(json.dumps(record, indent=2) + '\n'); capture.chmod(0o600)
    if fixture is not None:
        proposal, review = review_inputs(record, receipt, fixture, user, read_json(profile.package / 'operator/rubric.json'), profile=profile)
        before = req(course, learner, 'GET', f'/me/work/{sid}')['history']
        decision = directory / (user + '-automated-synthetic-review.json')
        decision.write_text(json.dumps({'review_actor_kind': LABEL, 'human_review_performed': False,
            'assessment_quality_evaluated': False, 'assessment_id': record['id'],
            'proposal': proposal, 'explicit_review_request': review}, indent=2) + '\n')
        decision.chmod(0o600)
        pending = req(course, teacher, 'POST', f'/assessments/{record["id"]}/proposal', proposal)
        if pending['status'] != 'pending' or pending.get('grade_id') or pending['document']['proposal'] != proposal:
            raise RuntimeError('fixture import did not preserve a pending, ungraded proposal')
        if req(course, learner, 'GET', f'/me/work/{sid}')['history'] != before:
            raise RuntimeError('proposal import changed the published grade history')
        # A distinct authenticated request exercises approval; never a human claim.
        approved = req(course, teacher, 'POST', f'/assessments/{record["id"]}/review', review)
        rows = req(course, teacher, 'GET', f'/submissions/{sid}/assessments')
        matches = [row for row in rows if row['id'] == record['id']]
        if len(matches) != 1 or matches[0]['status'] != 'approved' or not approved.get('grade_id') or matches[0].get('grade_id') != approved['grade_id']:
            raise RuntimeError('explicit synthetic approval lacks matching persisted grade identity')
        if matches[0]['document']['proposal'] != proposal or matches[0]['document']['review']['criteria'] != review['criteria'] or matches[0]['document']['review']['note'] != review['note']:
            raise RuntimeError('persisted fixture proposal or explicit review changed')
        total = sum(c['points'] for c in review['criteria'])
        after = req(course, learner, 'GET', f'/me/work/{sid}')['history']
        added = [row for row in after if row not in before]
        if len(after) != len(before) + 1 or len(added) != 1 or added[0]['source'] != 'instructor-reviewed' or added[0]['submission_revision'] != receipt['commit'] or added[0]['score'] != total or added[0]['max_score'] != 10:
            raise RuntimeError('synthetic review did not publish exactly its fixture grade')
        return {'assessment_id': record['id'], 'grade_id': approved['grade_id'],
                'revision': record['revision'], 'points': total,
                'submitted_export_sha256': export_hashes, 'review_actor_kind': LABEL,
                'human_review_performed': False, 'assessment_quality_evaluated': False,
                'pending_grade_history_unchanged': True, 'persisted_review_readback': True}
    draft = {'assessment_id': record['id'], 'input_digest': document['input_digest'],
             'note': 'REPLACE_ME after inspecting all four artifacts; no model or monitoring evidence.',
             'criteria': [{'criterion_id': c['id'], 'points': None, 'feedback': 'REPLACE_ME',
                           'uncertainty': 'REPLACE_ME', 'citations': []}
                          for c in document['rubric']['criteria']]}
    decision = directory / (user + '-operator-judgments.json')
    decision.write_text(json.dumps(draft, indent=2) + '\n'); decision.chmod(0o600)
    print(f'Inspect {capture}. Fill {decision} with points, bounded feedback/uncertainty, '
          'and citations copied from actual path/sha256/location/quote. No default points are supplied.', flush=True)
    if input('Type import after reviewing the captured evidence, or anything else to stop: ').strip() != 'import':
        raise RuntimeError('operator stopped before proposal import')
    judgments = read_json(decision, 128 * 1024)
    if judgments.get('assessment_id') != record['id'] or judgments.get('input_digest') != document['input_digest'] or 'REPLACE_ME' in json.dumps(judgments):
        raise ValueError('operator judgments are incomplete or bound to another capture')
    proposal = {'submission_status': 'relevant_work', 'source': 'instructor-import',
                'model': 'none-human-instructor', 'prompt_version': 'CW1-operator-review-v1',
                'input_digest': document['input_digest'], 'criteria': judgments['criteria']}
    pending = req(course, teacher, 'POST', f'/assessments/{record["id"]}/proposal', proposal)
    if pending['status'] != 'pending':
        raise RuntimeError('proposal did not remain pending for explicit review')
    print('Proposal imported. This has not published a reviewed grade.', flush=True)
    if input(f'Type approve {record["id"]} to explicitly publish this reviewed judgment: ').strip() != 'approve ' + record['id']:
        raise RuntimeError('operator stopped before grade publication')
    approved = req(course, teacher, 'POST', f'/assessments/{record["id"]}/review', {
        'action': 'approve', 'note': judgments['note'], 'criteria': judgments['criteria']})
    if approved['status'] != 'approved' or not approved.get('grade_id'):
        raise RuntimeError('review did not publish an identified grade')
    return {'assessment_id': record['id'], 'grade_id': approved['grade_id'],
            'revision': record['revision'], 'points': sum(c['points'] for c in judgments['criteria']),
            'submitted_export_sha256': export_hashes}


def exercise(course, browser, args):
    profile = get_profile(getattr(args, 'assignment_profile', 'legacy'))
    revision = course.start()
    teacher, _ = course.login(browser, 'teacher', '/auth/login')
    contexts = [teacher]
    try:
        classroom = req(course, teacher, 'POST', '/classrooms', {
            'name': 'CW1 INFO511 synthetic demonstration', 'host': 'forgejo',
            'host_namespace': 'course-test', 'join_policy': 'roster'}, 201)
        cid = classroom['id']
        for user in ('alice', 'bob'):
            req(course, teacher, 'POST', f'/classrooms/{cid}/roster', {'username': user}, 201)
        assignment = req(course, teacher, 'POST', f'/classrooms/{cid}/assignments', {
            'title': profile.title, 'slug': profile.task,
            'type': 'individual', 'grading_spec': 'grading.json',
            'template': {'namespace': 'course-test', 'name': 'template', 'ref': revision}}, 201)
        aid = assignment['id']
        req(course, teacher, 'PATCH', f'/assignments/{aid}/grading-policy', {
            'template_commit': revision, 'grading_spec': 'grading.json'})
        course.env.update(CAIRN_ASSESSMENT_REVIEW='1', CAIRN_SUBMISSIONS='true',
                          CAIRN_SUBMISSION_CLASSROOMS=cid)
        if args.device_only:
            course.env.pop('CAIRN_WORKSPACE_URLS', None)
        else:
            course.env['CAIRN_WORKSPACE_URLS'] = json.dumps({cid: args.workspace})
        course.restart()
        teacher.close()
        contexts.remove(teacher)
        teacher, _ = course.login(browser, 'teacher', '/auth/login'); contexts.append(teacher)
        capabilities = req(course, teacher, 'GET', '/assessment-capabilities')
        if not capabilities['review'] or capabilities['model_provider']:
            raise RuntimeError('review-only capabilities required; no model provider')
        rubric = read_json(profile.package / 'operator/rubric.json')
        req(course, teacher, 'PUT', f'/assignments/{aid}/assessment-rubric', rubric)
        learners = {}
        for user in ('alice', 'bob'):
            learner, _ = course.login(browser, user, f'/assignments/{aid}/accept')
            contexts.append(learner)
            item = active_work(course, learner, profile=profile)
            if args.device_only and item.get('workspace_url'):
                raise RuntimeError('device-only fixture unexpectedly exposes a workspace mapping')
            assigned = req(course, teacher, 'GET', f'/assignments/{aid}/submissions')
            if not any(row['id'] == item['submission_id'] for row in assigned):
                raise RuntimeError('learner work is not bound to the pinned assignment')
            learners[user] = (learner, item)
        result = {'assignment_profile':profile.key, 'task_version':profile.task, 'template_commit': revision, 'assignment_id': aid, 'submissions': {},
                  'scope': 'synthetic-device-transfer-and-automated-review-gates' if args.reviewer_fixture else 'synthetic-explicit-device-transfer-and-human-API-review',
                  'review_actor_kind': LABEL if args.reviewer_fixture else 'interactive-instructor',
                  'human_review_performed': not bool(args.reviewer_fixture),
                  'assessment_quality_evaluated': False,
                  'workspace_mapping': 'omitted-device-only' if args.device_only else 'configured',
                  'notebook_execution': 'SEPARATE_NOT_ESTABLISHED_HERE',
                  'model_calls': 0, 'real_students': False, 'full_browser_accessibility': 'NOT_ESTABLISHED_HERE'}
        attempts = [('alice', 'bob', args.alice_saved), ('bob', 'alice', args.bob_saved)]
        if getattr(args, 'notebook_data', None):
            # Exercise the new native/live path first so failures stop early
            # within the finite grant, before repeating the unchanged control.
            attempts.reverse()
        for user, other, saved in attempts:
            learner, item = learners[user]
            notebook_page = None
            fixture = args.fixture_data
            if user == 'bob' and getattr(args, 'notebook_data', None):
                from info511_notebook import journey, derive_fixture
                notebook, notebook_page, actual = journey(course, learner, learners[other][0], item, args.notebook_data, profile=profile)
                saved = Path(args.notebook_data['bob']['work_directory'])
                if fixture is not None:
                    fixture, provenance = derive_fixture(fixture, actual, args.notebook_review_fixture_out,
                        args.notebook_data['contract_sha256'], args.fixture_digest, profile=profile)
                    notebook['derived_automated_fixture'] = provenance
                result['notebook'] = notebook
                result['notebook_execution'] = 'ACTUAL_BOB_NATIVE_SYNTHETIC_ONLY'
                result['model_calls'] = 'NOT_COUNTED_HERE_SEE_BELAY_LEDGER'
                result['scope'] = 'synthetic-native-notebook-exact-saved-revision-and-review'
            receipt = submit_info511_attempt(course, learner, learners[other][0], item,
                saved.resolve(), learner_username=user, expected_actions={name: 'create' for name in profile.files},
                notebook_page=notebook_page, profile=profile)
            correctness = await_grade(course, learner, item['submission_id'], receipt['commit'], profile=profile)
            review = operator_review(course, teacher, item['submission_id'], receipt, course.root, user,
                                     fixture=fixture, learner=learner, profile=profile)
            result['submissions'][user] = {'receipt': receipt, 'correctness': correctness, 'review': review}
        response = teacher.request.get(course.url + f'/classrooms/{cid}/grades.csv')
        if response.status != 200:
            raise RuntimeError('instructor grade export unavailable')
        rows = list(csv.DictReader(io.StringIO(response.text())))
        if len(rows) != 2 or {r['username'] for r in rows} != {'alice', 'bob'}:
            raise RuntimeError('unexpected export scope')
        for row in rows:
            if float(row['score']) != result['submissions'][row['username']]['review']['points'] or float(row['max_score']) != 10:
                raise RuntimeError('export differs from instructor-reviewed grades')
        export = course.root / ('automated-synthetic-reviewed-grades.csv' if args.reviewer_fixture else 'instructor-reviewed-grades.csv')
        export.write_bytes(response.body()); export.chmod(0o600)
        if list(csv.DictReader(io.StringIO(export.read_text()))) != rows:
            raise RuntimeError('export did not reopen identically')
        # Downloading is NOT confirm-export: the separate retention clock is not started.
        result['grade_export_sha256'] = hashlib.sha256(export.read_bytes()).hexdigest()
        result['retention_confirmation'] = 'NOT_REQUESTED'
        return result
    finally:
        for context in contexts:
            context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cairn-binary', 'cairn-source', 'belay-source', 'alice-saved', 'bob-saved', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--image', required=True, help='Existing local immutable sha256 image ID')
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument('--workspace', help='Existing approved synthetic workspace URL; no service is created here')
    destination.add_argument('--device-only', action='store_true', help='Omit unused Hub/workspace mapping; actual saved-file device upload only')
    parser.add_argument('--reviewer-fixture', type=Path, help='Explicit authored automated synthetic reviewer fixture; never human review')
    parser.add_argument('--notebook-contract', type=Path, help='Private operator contract for prestarted native synthetic Jupyter/Belay; no provisioning')
    parser.add_argument('--notebook-review-fixture-out', type=Path, help='Explicit fresh derived automated fixture output; only Bob executed notebook/CSV hashes may change')
    parser.add_argument('--forgejo-image', default=FORGEJO_IMAGE, help='Existing local Forgejo image ID or fixture-specific tag/reference')
    parser.add_argument('--checkout-image', default='cairn-checkout:local', help='Existing local acquisition image ID or fixture-specific tag')
    parser.add_argument('--execute-synthetic', action='store_true')
    parser.add_argument('--assignment-profile', choices=('legacy','nhanes-v1'), default='legacy')
    args = parser.parse_args()
    profile = get_profile(args.assignment_profile)
    if not args.execute_synthetic or (args.reviewer_fixture is None and not sys.stdin.isatty()):
        parser.error('explicit synthetic execution and either interactive instructor or explicit reviewer fixture are required')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', args.image):
        parser.error('use the already inspected local immutable CPU course image ID')
    binding = read_json(profile.package / 'operator/cairn-source-binding.json')
    for name, expected in binding['cairn_files'].items():
        if hashlib.sha256((args.cairn_source / name).read_bytes()).hexdigest() != expected:
            parser.error('Cairn candidate source differs from the reviewed CSV binding')
    args.fixture_data = None
    fixture_digest = None
    if args.reviewer_fixture:
        with args.reviewer_fixture.resolve().open('rb') as stream:
            raw_fixture = stream.read(128 * 1024 + 1)
        if len(raw_fixture) > 128 * 1024:
            parser.error('reviewer fixture exceeds the input limit')
        args.fixture_data = json.loads(raw_fixture)
        fixture_digest = hashlib.sha256(raw_fixture).hexdigest()
        if not isinstance(args.fixture_data, dict) or args.fixture_data.get('schema') != 'CW1-reviewer-fixture-v1' or args.fixture_data.get('task_version') != profile.task or args.fixture_data.get('review_actor_kind') != LABEL or set(args.fixture_data.get('learners', {})) != {'alice', 'bob'}:
            parser.error('explicit two-learner CW1 automated synthetic fixture required')
    args.fixture_digest = fixture_digest
    args.notebook_data = None
    if args.notebook_contract:
        from info511_notebook import read_contract
        args.notebook_data = read_contract(args.notebook_contract.absolute(), args.workspace,
            device_only=args.device_only, fixture=args.fixture_data, derivative=args.notebook_review_fixture_out, profile=profile)
    elif args.notebook_review_fixture_out:
        parser.error('derived notebook fixture output requires an explicit notebook contract')
    # Refuse missing data/checker bindings before any Docker or Course effects.
    files = template_files(args.belay_source.resolve(), args.image, profile=profile)
    images = {}
    for role, selected in (('correctness', args.image), ('forgejo', args.forgejo_image), ('checkout', args.checkout_image)):
        if not selected or selected.startswith('-') or any(c.isspace() for c in selected):
            parser.error('image selectors must be existing local IDs or nonempty fixture tags')
        inspected = json.loads(run('docker', 'image', 'inspect', selected))  # Never pull, build or retag.
        if len(inspected) != 1 or not re.fullmatch(r'sha256:[0-9a-f]{64}', inspected[0]['Id']):
            parser.error('image selector did not resolve to one immutable local image ID')
        images[role] = inspected[0]['Id']
    args.output.mkdir(mode=0o700, parents=False, exist_ok=False)
    course = Course(args.output.resolve(), args.cairn_binary.resolve(), args.workspace,
                    args.image, args.cairn_source.resolve(), template_files=files,
                    forgejo_image=images['forgejo'], checkout_image=images['checkout'])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            observations = exercise(course, browser, args)
        finally:
            try:
                browser.close()
            finally:
                course.close()  # Existing invocation-owned cleanup and absence checks.
    observations['owned_cleanup_verified'] = True
    observations['image_ids'] = images
    observations['reviewer_fixture_sha256'] = fixture_digest
    result = args.output / 'CW1-OBSERVATIONS.json'
    result.write_text(json.dumps(observations, indent=2) + '\n'); result.chmod(0o600)
    print('Recorded bounded CW1 observations. This is not notebook/model/whole-platform acceptance.')


if __name__ == '__main__':
    main()
