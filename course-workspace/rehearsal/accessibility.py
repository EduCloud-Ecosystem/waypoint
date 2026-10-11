"""Automated WCAG checks and keyboard probes of synthetic pilot screens.

This is a regression gate, not accessibility certification or a screen-reader test.
The pinned axe asset comes from the reviewed Cairn dashboard's npm lockfile.
"""
import hashlib
import json
from urllib.parse import urlparse


def audit(page, source):
    asset = source / 'web/node_modules/axe-core/axe.min.js'
    package = json.loads((asset.parent / 'package.json').read_text())
    lock = json.loads((source / 'web/package-lock.json').read_text())
    assert package['version'] == lock['packages']['node_modules/axe-core']['version']
    page.evaluate('async () => { await document.fonts.ready; await Promise.all(document.getAnimations().map(a => a.finished.catch(() => {}))); }')
    page.add_script_tag(path=str(asset))
    result = page.evaluate('''async () => {
      const result = await axe.run(document, {
        runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']}
      });
      return {violations: result.violations.map(v => ({id: v.id,
        targets: v.nodes.map(n => n.target).slice(0, 8)}))};
    }''')
    assert not result['violations'], 'accessibility violations: ' + json.dumps(result['violations'])
    return hashlib.sha256(asset.read_bytes()).hexdigest()


def tab_to(page, target, limit=120):
    """Reach an existing visible control using sequential keyboard navigation."""
    for _ in range(limit):
        if target.evaluate('(element) => element === document.activeElement'):
            return
        page.keyboard.press('Tab')
    raise AssertionError('keyboard could not reach target control')


def dashboard_keyboard(page, source, output=None):
    from playwright.sync_api import expect
    # Start at a fresh document so this also tests the skip link's tab order.
    page.reload()
    opener = page.get_by_role('button', name='+ New', exact=True)
    expect(opener).to_be_visible()
    page.keyboard.press('Tab')
    expect(page.get_by_role('link', name='Skip to main content')).to_be_focused()
    page.keyboard.press('Enter')
    expect(page.locator('#main-content')).to_be_focused()
    tab_to(page, opener)
    page.keyboard.press('Enter')
    dialog = page.get_by_role('dialog', name='New classroom', exact=True)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label('Course name')).to_be_focused()
    audit(page, source)
    # Validation and server errors must remain in the modal accessibility tree;
    # an alert outside a native modal is inert even when a DOM locator finds it.
    create = dialog.get_by_role('button', name='Create', exact=True)
    tab_to(page, create)
    page.keyboard.press('Enter')
    expect(dialog.get_by_role('alert')).to_contain_text('name and host organization are required')
    expect(dialog).to_be_visible()
    audit(page, source)
    location = urlparse(page.url)
    if location.scheme != 'http' or location.hostname != '127.0.0.1':
        raise ValueError('mocked creation failure requires the disposable loopback course')
    endpoint = location.scheme + '://' + location.netloc + '/classrooms'
    rejected = []
    def reject_creation(route):
        if route.request.method == 'POST':
            rejected.append(True)
            route.fulfill(status=503, content_type='application/json', body=json.dumps({
                'error': 'Synthetic create failure; no classroom was created.'}))
        else:
            route.continue_()
    page.route(endpoint, reject_creation)
    try:
        dialog.get_by_label('Course name').fill('Synthetic keyboard rejection')
        dialog.get_by_label('Host organization').fill('synthetic-keyboard-rejection')
        tab_to(page, create)
        page.keyboard.press('Enter')
        expect(dialog.get_by_role('alert')).to_contain_text('Synthetic create failure')
        assert rejected == [True], 'creation failure was not intercepted exactly once'
        expect(dialog.get_by_label('Course name')).to_have_value('Synthetic keyboard rejection')
        expect(dialog).to_be_visible()
        audit(page, source)
        if output is not None:
            page.screenshot(path=str(output / 'instructor-modal-error.png'), full_page=True)
    finally:
        page.unroute(endpoint, reject_creation)
    # A click in the actual interior padding must not discard entered fields.
    box = dialog.bounding_box()
    assert box, 'modal bounds unavailable'
    page.mouse.click(box['x'] + 5, box['y'] + 5)
    expect(dialog).to_be_visible()
    expect(dialog.get_by_label('Course name')).to_have_value('Synthetic keyboard rejection')
    # Native modal focus must stay inside across repeated forward/backward wraps.
    for key in ('Tab', 'Shift+Tab'):
        for _ in range(10):
            page.keyboard.press(key)
            assert dialog.evaluate('(element) => element.contains(document.activeElement)')
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)
    expect(opener).to_be_focused()
    # Test actual backdrop dismissal separately from Escape and interior clicks.
    page.keyboard.press('Enter')
    expect(dialog).to_be_visible()
    box = dialog.bounding_box()
    assert box and box['y'] > 2, 'fixture needs a point above the modal backdrop'
    page.mouse.click(box['x'] + box['width'] / 2, box['y'] - 2)
    expect(dialog).to_have_count(0)
    expect(opener).to_be_focused()
    audit(page, source)
    if output is not None:
        page.screenshot(path=str(output / 'instructor-dashboard.png'), full_page=True)
