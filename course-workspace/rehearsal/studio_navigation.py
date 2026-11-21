"""SPDX-License-Identifier: Apache-2.0
Native side-tab navigation and local first-use introduction controls.
"""
import json
import re

from playwright.sync_api import expect


def side_tab(page, panel):
    return page.locator('.jp-SideBar.jp-mod-right [data-id="waypoint-' + panel + '-panel"]')


def lecture_action(page, name):
    panel = page.locator('#waypoint-lecture-panel')
    toggle = panel.get_by_role('button', name='Lecture guide', exact=True)
    if toggle.count() and toggle.get_attribute('aria-expanded') == 'false':
        toggle.click()
    return panel.get_by_role('button', name=name, exact=True)


def verify_studio_navigation(page, root):
    welcome = page.locator('#waypoint-studio-welcome')
    expect(welcome.get_by_role('heading', name='Your side tabs', exact=True)).to_be_visible()
    assert page.evaluate("localStorage.getItem('educloud-studio-welcome-v1')") is None
    (root/'studio-native-tabs.json').write_text(json.dumps(page.evaluate("""() => ({
      tabs:[...document.querySelectorAll('[role=tab]')].map(e=>({id:e.id,owner:e.dataset.id,label:e.textContent})),
      bars:[...document.querySelectorAll('.jp-SideBar')].map(e=>({id:e.id,classes:e.className}))
    })""")))
    # One native entry per feature; no duplicate top-bar actions.
    expect(side_tab(page, 'lecture')).to_have_count(1)
    expect(side_tab(page, 'belay')).to_have_count(1)
    expect(page.locator('#waypoint-belay-link button')).to_have_count(0)
    expect(page.locator('#waypoint-lecture-open')).to_have_count(0)
    page.screenshot(path=str(root/'studio-first-use.png'))
    welcome.get_by_role('button', name='Start quick tour').focus()
    welcome.get_by_role('button', name='Start quick tour').press('Enter')
    for step, title in enumerate(('Your notebook','Run and Save','Your files',
                                  'Lecture notes and the guide','Optional tutoring','Submit when ready')):
        expect(welcome.get_by_role('heading', name=f'{step+1} of 6 · {title}', exact=True)).to_be_visible()
        next_button = welcome.get_by_role('button', name='Finish tour' if step == 5 else 'Next', exact=True)
        page.wait_for_timeout(100)
        expect(next_button).to_be_focused()
        if step == 1:
            welcome.get_by_role('button', name='Previous', exact=True).click()
            expect(welcome.get_by_role('heading', name='1 of 6 · Your notebook', exact=True)).to_be_visible()
            welcome.get_by_role('button', name='Next', exact=True).click()
        next_button.press('Enter')
    expect(welcome).to_have_count(0)
    assert page.evaluate("localStorage.getItem('educloud-studio-welcome-v1')") == 'seen'
    page.wait_for_function("() => !!document.activeElement.closest('#jp-main-dock-panel')")
    page.reload()
    expect(side_tab(page,'lecture')).to_be_visible(timeout=60000)
    expect(page.locator('.jp-Notebook .jp-CodeCell .cm-content').first).to_be_visible(timeout=60000)
    # The router also reopens the URL's file after the notebook has rendered.
    # Wait for that native file-browser restoration before testing overlay focus.
    expect(page.locator('.jp-FileBrowser')).not_to_have_class(re.compile(r'\bjp-mod-restoring\b'), timeout=60000)
    expect(welcome).to_have_count(0)
    # Opening a tab cannot implicitly capture code or ask for a hint.
    side_tab(page,'belay').click()
    expect(page.get_by_role('button',name='Review course example',exact=True)).to_be_visible()
    expect(page.frame_locator('iframe[title="Course tutor"]').locator('#source')).to_have_value('')
    expect(page.frame_locator('iframe[title="Course tutor"]').locator('#policy')).not_to_have_text('Checking permitted help…')
    side_tab(page,'belay').click()
    expect(page.locator('#waypoint-belay-panel')).to_be_hidden()
    for width, theme in ((1280,'light'),(320,'dark')):
        page.set_viewport_size({'width':width,'height':900})
        page.get_by_label('Appearance',exact=True).select_option(theme)
        expect(page.locator('body')).to_have_attribute('data-jp-theme-name','JupyterLab '+theme.title())
        page.locator('.jp-SplashScreen').wait_for(state='hidden')
        side_tab(page, 'lecture').click()
        panel = page.locator('#waypoint-lecture-panel')
        toggle = panel.get_by_role('button',name='Lecture guide',exact=True)
        expect(toggle).to_have_attribute('aria-expanded','false')
        expect(panel.get_by_role('region',name='Visual guidance')).to_be_hidden()
        notes = page.frame_locator('iframe[title="INFO 511 lecture notes"]')
        expect(notes.get_by_role('heading', name='Reading a joined dataset')).to_be_visible()
        notes.get_by_role('link', name='Missing readings').click()
        expect(notes.get_by_role('heading', name='Missing readings')).to_be_in_viewport()
        box = page.locator('iframe[title="INFO 511 lecture notes"]').bounding_box()
        assert box and box['height'] >= 600, box
        page.screenshot(path=str(root/f'studio-compact-notes-{width}.png'))
        toggle.focus(); toggle.press('Enter')
        expect(panel.get_by_role('button',name='Reading notes',exact=True)).to_be_focused()
        page.keyboard.press('Escape')
        expect(toggle).to_be_focused(); expect(toggle).to_have_attribute('aria-expanded','false')
        lecture_action(page,'Workspace tour').click()
        expect(welcome.get_by_role('heading',name='Your side tabs',exact=True)).to_be_visible()
        welcome.get_by_role('button',name='Start quick tour',exact=True).click()
        expect(welcome.get_by_role('heading',name='1 of 6 · Your notebook',exact=True)).to_be_visible()
        assert welcome.evaluate('(e)=>e.scrollWidth <= e.clientWidth')
        assert page.locator('html').evaluate('(e)=>e.scrollWidth <= e.clientWidth')
        page.screenshot(path=str(root/f'studio-tour-{width}.png'))
        page.keyboard.press('Escape'); expect(welcome).to_have_count(0)
        page.wait_for_function("() => !!document.activeElement.closest('#jp-main-dock-panel')")
    page.set_viewport_size({'width':1280,'height':900})
    page.get_by_label('Appearance',exact=True).select_option('light')
    expect(page.locator('body')).to_have_attribute('data-jp-theme-name','JupyterLab Light')
    page.locator('.jp-SplashScreen').wait_for(state='hidden')
    return {'single_side_entries':True,'no_implicit_code_capture':True,'first_use_steps':6,
            'seen_preference_only':True,'reload_suppressed':True,'replay':True,
            'keyboard_return':True,'compact_notes_min_height':600,'widths':[1280,320],
            'themes':['light','dark']}
