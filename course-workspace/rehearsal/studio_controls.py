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
  # Inject changing API observations to exercise UI refresh, not real grading.
  observed=page.request.get(url+'/me/work').json()
  item=observed['work'][0]
  details={'history':[],'tests':[],'assessments':[]}
  page.unroute(url+'/me/work/s')
  detail_reads=[]
  def revised_detail(route):
   detail_reads.append(len(details['history']))
   route.fulfill(json=details)
  page.route(url+'/me/work/s',revised_detail)
  page.route(url+'/me/work',lambda route:route.fulfill(json=observed))
  item.update(classroom_name='Updated synthetic course',repo_web_url='https://example.org/new-repository',status='published',deadline='2030-01-01T12:00:00Z')
  def next_grade(n):
   grade={'score':n,'max_score':10,'graded_at':f'2026-11-15T12:00:0{n}Z','source':'deterministic','submission_revision':str(n)*40}
   item['latest_grade']=grade
   details['history'].append(grade)
  next_grade(1)
  summary.focus();page.evaluate('load()')
  expect(page.locator('.work-meta')).to_contain_text('Updated synthetic course')
  expect(page.locator('.work-meta')).to_contain_text('status: published')
  expect(page.locator('.work-meta a')).to_have_attribute('href','https://example.org/new-repository')
  expect(page.locator('details tbody tr')).to_have_count(1)
  expect(summary).to_be_focused()
  summary.click();expect(page.locator('details')).not_to_have_attribute('open','')
  next_grade(2);page.evaluate('load()')
  expect(page.locator('.row > .score')).to_have_text('2 / 10')
  assert detail_reads==[1],detail_reads
  summary.click();expect(page.locator('details tbody tr')).to_have_count(2)
  # Delay one details response while another grade arrives: stale data must not win.
  held=[]
  page.unroute(url+'/me/work/s')
  page.route(url+'/me/work/s',lambda route:held.append(route))
  next_grade(3);page.evaluate('load()')
  expect(page.locator('details')).to_contain_text('Loading details')
  page.wait_for_function('true')
  assert len(held)==1
  next_grade(4);page.evaluate('load()')
  expect(page.locator('.row > .score')).to_have_text('4 / 10')
  page.unroute(url+'/me/work/s')
  page.route(url+'/me/work/s',revised_detail)
  held[0].fulfill(json={'history':details['history'][:3],'tests':[],'assessments':[]})
  expect(page.locator('details tbody tr')).to_have_count(4)
  report['passed'].append('injected-metadata-and-grade-revisions-refresh-open-closed-and-inflight-history')
  page.goto(url+'/me?submission=s&appearance=dark#retained-synthetic-fragment')
  expect(page.get_by_label('Appearance',exact=True)).to_have_value('dark')
  assert 'appearance=' not in page.url and 'submission=s' in page.url and page.url.endswith('#retained-synthetic-fragment')
  page.get_by_label('Appearance',exact=True).select_option('light');page.reload()
  expect(page.get_by_label('Appearance',exact=True)).to_have_value('light')
  report['passed'].append('theme-handoff-consumed-once-preserving-query-and-fragment')
  # A held assignment retry must be the only request even on rapid repeated clicks.
  submit=student.new_page();submit.on('pageerror',lambda e:errors.append(str(e)))
  item.update(submissions_enabled=True,submission_open=True)
  pending=[];work_reads=[]
  def assignment_response(route):
   work_reads.append(1)
   if len(work_reads)==1:route.fulfill(status=503,json={'error':'synthetic retry control'})
   else:pending.append(route)
  submit.route(url+'/me/work',assignment_response)
  submit.route('**/submission-receipts',lambda route:route.fulfill(json={'receipts':[]}))
  submit.goto(url+'/me/submit?submission=s')
  expect(submit.locator('#retry-load')).to_be_visible()
  submit.evaluate("document.getElementById('retry-load').click(); document.getElementById('retry-load').click(); document.getElementById('reload').click()")
  expect(submit.locator('#retry-load')).to_be_disabled()
  assert len(pending)==1 and len(work_reads)==2
  pending[0].fulfill(json=observed)
  expect(submit.locator('#sources')).to_be_enabled()
  submit.locator('#files').set_input_files({'name':'response.md','mimeType':'text/markdown','buffer':b'Synthetic saved work'})
  expect(submit.locator('#file-summary')).to_contain_text('response.md')
  expect(submit.locator('#preview')).to_be_enabled()
  assert len(work_reads)==2
  submit.close()
  report['passed'].append('injected-assignment-retry-is-single-flight-and-retains-selected-files')
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
