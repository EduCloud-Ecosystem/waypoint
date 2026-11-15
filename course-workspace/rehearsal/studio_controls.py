"""Provider-disabled browser controls against the opt-in Cairn Go fixture."""
import argparse,json,traceback
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True);a=p.parse_args();root=a.fixture
url=(root/'url').read_text();report={'scope':'actual-Cairn-loopback-synthetic-fixture','model_requests':0,'passed':[],'failed':[]}
try:
 with sync_playwright() as pw:
  browser=pw.chromium.launch()
  student=browser.new_context(reduced_motion='reduce',storage_state=str(root/'student-state.json'),viewport={'width':320,'height':820})
  page=student.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  attempts=[0]
  def detail(route):
   attempts[0]+=1
   if attempts[0]==1:route.fulfill(status=503,content_type='application/json',body='{"error":"synthetic transient failure"}')
   else:route.continue_()
  page.route(url+'/me/work/s',detail)
  page.goto(url+'/me');page.get_by_text('Per-test results & history',exact=True).click()
  expect(page.get_by_role('button',name='Retry details')).to_be_visible()
  page.get_by_role('button',name='Retry details').press('Enter')
  expect(page.get_by_role('heading',name='Results and history')).to_be_focused()
  assert attempts[0]==2
  report['passed'].append('real-detail-response-after-injected-503-and-keyboard-retry')
  summary=page.locator('summary');summary.focus()
  page.evaluate('load()')
  expect(summary).to_be_focused();assert page.locator('details').evaluate('e=>e.open')
  report['passed'].append('work-refresh-preserves-focused-expanded-history')
  for pref,resolved in [('dark','dark'),('light','light'),('system','dark')]:
   page.emulate_media(color_scheme='dark');page.get_by_label('Appearance',exact=True).select_option(pref)
   expect(page.locator('html')).to_have_attribute('data-studio-theme',resolved)
   assert page.evaluate('document.documentElement.scrollWidth===innerWidth')
  page.reload();expect(page.get_by_label('Appearance',exact=True)).to_have_value('system')
  page.emulate_media(color_scheme='light');expect(page.locator('html')).to_have_attribute('data-studio-theme','light')
  page.screenshot(path=str(root/'student-320-light.png'),full_page=True)
  report['passed'].append('student-three-modes-reload-and-320-reflow')
  instructor=browser.new_context(reduced_motion='reduce',storage_state=str(root/'instructor-state.json'),viewport={'width':1280,'height':820})
  teacher=instructor.new_page();teacher.on('pageerror',lambda e:errors.append(str(e)));teacher.goto(url+'/')
  expect(teacher.get_by_label('Appearance',exact=True)).to_be_visible()
  teacher.get_by_label('Appearance',exact=True).select_option('dark')
  expect(teacher.locator('html')).to_have_attribute('data-studio-theme','dark')
  expect(teacher.get_by_role('heading',name='Synthetic assessment course',exact=True)).to_be_visible()
  teacher.screenshot(path=str(root/'instructor-dark.png'),full_page=True)
  teacher.reload();expect(teacher.get_by_label('Appearance',exact=True)).to_have_value('dark')
  teacher.set_viewport_size({'width':320,'height':820})
  expect(teacher.get_by_role('heading',name='Synthetic assessment course',exact=True)).to_be_visible()
  assert teacher.evaluate('document.documentElement.scrollWidth===innerWidth')
  teacher.screenshot(path=str(root/'instructor-320-dark.png'),full_page=True)
  report['passed'].append('built-instructor-console-dark-persistence-and-320-reflow')
  assert not errors,errors
  browser.close()
except Exception as e:
 report['failed'].append(str(e));(root/'browser-failure.txt').write_text(traceback.format_exc())
finally:
 (root/'browser-result.json').write_text(json.dumps(report,indent=2));(root/'stop').touch()
print(json.dumps(report,indent=2));raise SystemExit(bool(report['failed']))
