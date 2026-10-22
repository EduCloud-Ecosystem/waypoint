"""Offline interrupted-save controls; no Playwright, notebook or service runtime."""
import copy
from types import SimpleNamespace
import unittest

import info511_notebook as notebook


MARKER='print("synthetic")\n# UNSAVED_CONNECTION_CONTROL\n'
ORIGIN='http://127.0.0.1:8189'
NAME='nhanes-wrangling.ipynb'


class SavePage:
    def __init__(self, *, dialog=None):
        self.editor=SimpleNamespace(value=MARKER)
        self.snapshot={NAME:b'saved notebook','tidy-nhanes.csv':b'saved CSV',
            'explanation.md':b'explanation','assistance.md':b'declaration'}
        self.saved=copy.deepcopy(self.snapshot)
        self.request=SimpleNamespace(method='PUT',url=ORIGIN+'/api/contents/'+NAME,
            post_data_json={'type':'notebook','content':{'cells':[{'id':'load-input',
                'cell_type':'code','source':MARKER}] }},failure='net::ERR_INTERNET_DISCONNECTED',
            response=lambda:None)
        self.keyboard=SimpleNamespace(press=self.press)
        self.dialog=dialog;self.clicked=[];self.aborted=[];self.unrouted=False
        self.repeat=False;self.after_abort=lambda:None;self.after_dismiss=lambda:None
    def route(self,pattern,handler): self.pattern,self.handler=pattern,handler
    def unroute(self,pattern,handler):
        assert (pattern,handler)==(self.pattern,self.handler);self.unrouted=True
    def expect_event(self,name,*,predicate,timeout):
        assert name=='requestfailed' and timeout==30000
        page=self
        class Event:
            def __enter__(self):return self
            def __exit__(self,*_):return False
            @property
            def value(self):
                assert predicate(page.request)
                return page.request
        return Event()
    def press(self,key):
        assert key=='Control+s'
        route=SimpleNamespace(request=self.request,
            abort=lambda reason:self.aborted.append(reason),continue_=lambda:None)
        self.handler(route);self.after_abort()
        if self.repeat:self.handler(route)
    def locator(self,selector):
        assert selector=='.jp-Dialog'
        page=self
        class Dialog:
            def count(self):return int(page.dialog is not None)
            def get_by_role(self,role,*,name,exact):
                assert role=='button' and exact
                def click():
                    page.clicked.append(name);page.dialog=None;page.after_dismiss()
                return SimpleNamespace(count=lambda:int(page.dialog==name),click=click)
        return Dialog()


def expect(target):
    def text(value,**kwargs):
        assert kwargs=={'use_inner_text':True,'timeout':10000}
        assert target.value==value
    return SimpleNamespace(to_have_text=text,to_have_count=lambda count,**_:unittest.TestCase().assertEqual(target.count(),count))


class SaveFailureTests(unittest.TestCase):
    def verify(self,page):
        return notebook.verify_interrupted_save(page,page.editor,ORIGIN,NAME,MARKER,page.snapshot,
            read_current=lambda:page.saved,expect=expect)
    def test_notification_without_dialog_still_proves_failed_exact_save_and_unsaved_marker(self):
        page=SavePage();result=self.verify(page)
        self.assertEqual(page.aborted,['internetdisconnected']);self.assertTrue(page.unrouted)
        self.assertEqual(page.clicked,[]);self.assertFalse(result['dialog_dismissed'])
        self.assertTrue(result['failed_request_observed'] and result['marker_in_failed_body'] and
            result['http_response_absent'] and result['saved_four_files_unchanged'])
        self.assertEqual(result['blocked_save_count'],1)
    def test_actual_cancel_or_ok_is_dismissed_without_retry(self):
        for label in ('Cancel','OK'):
            page=SavePage(dialog=label);result=self.verify(page)
            self.assertEqual(page.clicked,[label]);self.assertTrue(result['dialog_dismissed'])
    def test_retry_only_dialog_is_never_clicked(self):
        page=SavePage(dialog='Retry')
        with self.assertRaisesRegex(notebook.NotebookFailure,'safe_dismiss_unavailable'):self.verify(page)
        self.assertEqual(page.clicked,[]);self.assertTrue(page.unrouted)
    def test_wrong_origin_path_or_submitted_source_is_refused(self):
        for mutation in ('origin','path','source','duplicate-cell'):
            page=SavePage()
            if mutation=='origin':page.request.url='http://127.0.0.1:8190/api/contents/'+NAME
            elif mutation=='path':page.request.url=ORIGIN+'/api/contents/other.ipynb'
            elif mutation=='source':page.request.post_data_json['content']['cells'][0]['source']='old saved code'
            else:page.request.post_data_json['content']['cells']*=2
            with self.subTest(mutation=mutation),self.assertRaises(notebook.NotebookFailure):self.verify(page)
            self.assertEqual(page.aborted,['internetdisconnected']);self.assertTrue(page.unrouted)
    def test_http_response_or_missing_failure_cannot_be_accepted_as_interruption(self):
        for mutation in ('response','failure'):
            page=SavePage()
            if mutation=='response':page.request.response=lambda:SimpleNamespace(status=200)
            else:page.request.failure=None
            with self.subTest(mutation=mutation),self.assertRaisesRegex(notebook.NotebookFailure,'failure_unconfirmed'):self.verify(page)
    def test_changed_saved_bytes_editor_or_repeated_put_fails(self):
        for mutation in ('saved','editor','repeat','dismiss-save'):
            page=SavePage(dialog='Cancel' if mutation=='dismiss-save' else None)
            if mutation=='saved':page.after_abort=lambda:page.saved.update({NAME:b'changed'})
            elif mutation=='editor':page.after_abort=lambda:setattr(page.editor,'value','lost marker')
            elif mutation=='repeat':page.repeat=True
            else:page.after_dismiss=lambda:page.saved.update({'assistance.md':b'changed'})
            with self.subTest(mutation=mutation),self.assertRaises((notebook.NotebookFailure,AssertionError)):self.verify(page)
            self.assertTrue(page.unrouted)


if __name__=='__main__':unittest.main()
