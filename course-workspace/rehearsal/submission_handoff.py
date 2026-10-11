"""Real Cairn/Forgejo selected-file receipt acceptance, with browser confirmation."""
import base64
import hashlib
import time

from playwright.sync_api import expect

from accessibility import audit, tab_to


def first_submission(course, alice, bob, item, solution, notebook_page=None):
    submission = item['submission_id']
    endpoint = f'/me/work/{submission}'
    if notebook_page:
        page = notebook_page(alice, item)
    else:
        page = alice.new_page()
        page.goto(course.url + '/me/submit?submission=' + submission)
        page.get_by_label('Or select files saved on this device').set_input_files({
            'name': 'solution.py', 'mimeType': 'text/x-python', 'buffer': solution.encode()})
    expect(page.get_by_text('Signed in to Cairn as alice.', exact=False)).to_be_visible()
    expect(page.locator('#file-summary')).to_contain_text('solution.py')
    assert course.request(alice, 'GET', endpoint + '/submission-receipts')['receipts'] == []
    with page.expect_response(lambda response: response.url.endswith('/submission-previews') and response.request.method == 'POST') as response:
        tab_to(page, page.get_by_role('button', name='Preview submission', exact=True))
        page.keyboard.press('Enter')
    result = response.value
    assert result.status == 201, f'preview failed: {result.status}'
    preview = result.json()
    payload = result.request.post_data_json
    assert preview['status'] == 'preview'
    expected_hash = hashlib.sha256(solution.encode()).hexdigest()
    assert preview['changes'] == [{'path': 'solution.py', 'sha256': expected_hash,
                                   'bytes': len(solution.encode()), 'action': 'create'}]
    expect(page.get_by_role('button', name='Confirm submission', exact=True)).to_be_visible()
    audit(page, course.cairn_source)
    page.screenshot(path=str(course.root / 'submission-preview.png'), full_page=True)
    # The server-held preview survives a page refresh without uploading again.
    page.reload()
    page.get_by_role('button', name='Review this preview', exact=True).click()
    expect(page.locator('#manifest')).to_contain_text(expected_hash)
    expect(page.get_by_role('button', name='Confirm submission', exact=True)).to_be_visible()

    def post(context, path, data, status, origin=None):
        res = context.request.post(course.url + path, data=data,
                                   headers={'Origin': origin or course.url})
        assert res.status == status, f'handoff boundary status {res.status}, expected {status}'
        return res.json()

    confirm = endpoint + '/submission-previews/' + preview['id'] + '/confirm'
    post(bob, confirm, {}, 404)
    post(alice, confirm, {}, 403, 'https://untrusted.invalid')
    different = dict(payload, files=[{'path': 'solution.py', 'content': base64.b64encode(b'changed').decode()}])
    post(alice, endpoint + '/submission-previews', different, 409)
    post(alice, endpoint + '/submission-previews', {
        'idempotency_key': 'unsafe-path-fixture',
        'files': [{'path': '../outside.py', 'content': base64.b64encode(b'bad').decode()}]}, 400)

    # Drop only the browser's response after the actual server has confirmed and
    # persisted its operation. A retry must resolve the very same receipt.
    intercepted = []
    def lose_response(route):
        response = route.fetch()
        assert response.status in (200, 202)
        intercepted.append(response.json()['id'])
        route.abort('failed')
    page.route(course.url + confirm, lose_response)
    page.get_by_role('button', name='Confirm submission', exact=True).click()
    expect(page.locator('#status')).to_contain_text('same preview cannot create a second submission')
    page.unroute(course.url + confirm, lose_response)
    assert intercepted == [preview['id']]
    page.get_by_role('button', name='Confirm submission', exact=True).click()
    expect(page.locator('#preview-section')).to_be_hidden()
    for _ in range(120):
        receipt = course.request(alice, 'GET', endpoint + '/submission-receipts/' + preview['id'])
        if receipt['status'] == 'committed':
            break
        if receipt['status'] in ('conflict', 'failed', 'rejected', 'verification_needed'):
            raise AssertionError('submission failed: ' + receipt['status'])
        time.sleep(1)
    else:
        raise AssertionError('submission writer did not finish')
    assert receipt['changes'] == preview['changes']
    assert len(receipt['commit_revision']) in (40, 64)
    peer = bob.request.get(course.url + endpoint + '/submission-receipts/' + preview['id'])
    assert peer.status == 404
    receipts = course.request(alice, 'GET', endpoint + '/submission-receipts')['receipts']
    assert len(receipts) == 1 and receipts[0]['id'] == preview['id']
    page.get_by_role('button', name='Refresh receipts', exact=True).click()
    expect(page.get_by_role('heading', name='Committed — repository receipt verified')).to_be_visible()
    audit(page, course.cairn_source)
    page.screenshot(path=str(course.root / 'submission-receipt.png'), full_page=True)
    return {'id': preview['id'], 'commit': receipt['commit_revision'], 'changes': receipt['changes'],
            'checks': ['explicit-saved-file-preview', 'exact-file-digest-receipt',
                       'separate-confirmation-required', 'submission-csrf-denied',
                       'submission-peer-denied', 'submission-unsafe-path-denied',
                       'idempotency-payload-conflict', 'lost-confirmation-response-retry',
                       'one-commit-receipt', 'submission-ui-keyboard-axe']}


def verify_receipt_after_restart(course, context, submission, expected):
    endpoint = f'/me/work/{submission}/submission-receipts'
    receipt = course.request(context, 'GET', endpoint + '/' + expected['id'])
    assert receipt['status'] == 'committed'
    assert receipt['commit_revision'] == expected['commit']
    assert receipt['changes'] == expected['changes']
    assert len(course.request(context, 'GET', endpoint)['receipts']) == 1
    expected['checks'].append('submission-receipt-after-restart')
