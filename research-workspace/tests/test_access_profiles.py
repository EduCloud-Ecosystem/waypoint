"""Tier-specific drafts, dependency-free Explore, and accepted-v1 compatibility."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_workspace import assessment as a, allocation_export as x, allocation_sources as sources
from test_allocation_export import fixture, preview, FONT, FIXTURES

ROOT=Path(__file__).resolve().parents[1]


class AccessProfileTests(unittest.TestCase):
    def cli(self,*args):
        # Isolated standard-library process: no site directories or inherited
        # Python path; prove that neither optional PDF dependency is installed.
        wrapper="""import sys,importlib.util,runpy
sys.path.insert(0,sys.argv.pop(1))
if any(importlib.util.find_spec(name) is not None for name in ('reportlab','pypdf')):
    raise RuntimeError('Optional PDF libraries unexpectedly available')
runpy.run_module('research_workspace',run_name='__main__')
if any(name in sys.modules for name in ('reportlab','pypdf','research_workspace.allocation_pdf')):
    raise RuntimeError('Worksheet-only path loaded PDF code')
"""
        return subprocess.run([sys.executable,'-I','-S','-c',wrapper,str(ROOT),*map(str,args)],cwd=ROOT,capture_output=True,timeout=15)

    def preview_args(self,profile='access_explore'):
        return ('assessment-export-preview',FIXTURES/'research-estimate.json','--manifest',FIXTURES/'research-manifest.json',
                '--profile',profile,'--narrative',FIXTURES/(profile+'-narrative.json'))

    def test_exact_pr51_accepted_record_digests_remain_unchanged(self):
        bindings=fixture('pr51-accepted-preview-bindings.json')
        for profile,binding in bindings.items():
            kind='classroom' if profile=='nairr_classroom_draft' else 'research'
            p=x.make_preview(profile,fixture(kind+'-estimate.json'),[fixture(kind+'-manifest.json')],
                             fixture(profile+'-narrative.json'),binding['font'],binding['source'])
            self.assertEqual(p['preview_sha256'],binding['preview_sha256'])
            self.assertNotIn('include_planning_pdf',p['record'])
            self.assertEqual(x.validate_preview(p),p['record'])

    def test_explore_real_cli_without_font_or_pdf_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();r=self.cli(*self.preview_args());self.assertEqual(r.returncode,0,r.stderr.decode())
            p=json.loads(r.stdout);self.assertIsNone(p['record']['font']);self.assertIs(p['record']['include_planning_pdf'],False)
            self.assertIsNone(p['record']['requirements']['maximum_pages']);self.assertFalse(p['record']['requirements']['required_narrative_pdf_established'])
            saved=root/'preview';saved.write_bytes(r.stdout);out=root/'package'
            r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256',p['preview_sha256'],'--output',out)
            self.assertEqual(r.returncode,0,r.stderr.decode());receipt=json.loads(r.stdout)
            self.assertEqual(receipt['status'],'COMPLETE_DRAFT');self.assertEqual(receipt['file_count'],8)
            self.assertFalse((out/'draft.pdf').exists());self.assertFalse((out/'INCOMPLETE').exists())
            self.assertEqual(receipt['package_sha256'],hashlib.sha256((out/'package.json').read_bytes()).hexdigest())
            self.assertIn(b'No PDF was requested',(out/'README.txt').read_bytes())
            self.assertTrue(all(v is False for v in receipt['gates'].values()))

    def test_explore_no_pdf_refuses_font_without_reading_it(self):
        r=self.cli(*self.preview_args(),'--font','/missing/PRIVATE_SENTINEL.ttf')
        self.assertNotEqual(r.returncode,0);self.assertIn(b'does not accept a font',r.stderr);self.assertNotIn(b'PRIVATE_SENTINEL',r.stderr)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();p=preview('access_explore');saved=root/'preview';saved.write_bytes(x.export_bytes(p));out=root/'package'
            r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256',p['preview_sha256'],'--font','/missing/PRIVATE_SENTINEL.ttf','--output',out)
            self.assertNotEqual(r.returncode,0);self.assertIn(b'does not accept a font',r.stderr);self.assertFalse(out.exists())

    def test_optional_pdf_and_required_pdf_profiles_require_font(self):
        for args in ((self.preview_args()+('--include-planning-pdf',)),self.preview_args('access_accelerate')):
            r=self.cli(*args);self.assertNotEqual(r.returncode,0);self.assertIn(b'requires an operator-approved font',r.stderr)

    def test_render_mode_is_approved_and_cannot_change_on_export(self):
        plain=preview('access_explore');pdf=preview('access_explore',include_planning_pdf=True)
        self.assertNotEqual(plain['preview_sha256'],pdf['preview_sha256'])
        self.assertEqual(pdf['record']['requirements']['maximum_pages'],2)
        self.assertEqual(pdf['record']['requirements']['page_limit_basis'],'internal_optional_planning_bound_not_official')
        with patch('research_workspace.allocation_pdf.render_pdf',side_effect=AssertionError('must not render')):
            with self.assertRaises(a.AssessmentError):x.package_files(pdf,plain['preview_sha256'],b'font')
        changed=copy.deepcopy(plain);changed['record']['include_planning_pdf']=True;changed['preview_sha256']=x.export_digest(changed['record'])
        with self.assertRaises(a.AssessmentError):x.validate_preview(changed)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();saved=root/'preview';saved.write_bytes(x.export_bytes(plain));out=root/'package'
            r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256',plain['preview_sha256'],'--include-planning-pdf','--output',out)
            self.assertNotEqual(r.returncode,0);self.assertFalse(out.exists())

    def test_other_profiles_reject_new_mode_and_nullable_font_semantics(self):
        for profile in ('access_discover','access_accelerate','nairr_research_draft','nairr_classroom_draft'):
            p=preview(profile);r=p['record']
            with self.assertRaises(a.AssessmentError):x.make_preview(profile,r['estimate'],r['manifests'],r['narrative'],r['font'],r['requirements']['source'],include_planning_pdf=True)
            r['include_planning_pdf']=False;p['preview_sha256']=x.export_digest(r)
            with self.assertRaises(a.AssessmentError):x.validate_preview(p)
            del r['include_planning_pdf'];r['font']=None;p['preview_sha256']=x.export_digest(r)
            with self.assertRaises(a.AssessmentError):x.validate_preview(p)

    def test_modes_reject_nonboolean_values_and_hidden_requirements_change(self):
        for value in (0,1,None,'false',[]):
            p=preview('access_explore');p['record']['include_planning_pdf']=value;p['preview_sha256']=x.export_digest(p['record'])
            with self.assertRaises(a.AssessmentError):x.validate_preview(p)
        p=preview('access_explore');p['record']['requirements']['optional_planning_pdf_included']=True;p['preview_sha256']=x.export_digest(p['record'])
        with self.assertRaises(a.AssessmentError):x.validate_preview(p)

    def test_tiers_have_closed_distinct_manual_fields_and_checklists(self):
        explore=preview('access_explore')['record'];accelerate=preview('access_accelerate')['record']
        for key in ('research_objectives','resource_estimates','computational_plan','software_special_needs','team_preparedness'):
            self.assertIn(key,accelerate['narrative']['fields']);self.assertNotIn(key,explore['narrative']['fields'])
        self.assertEqual(accelerate['requirements']['maximum_pages'],3)
        self.assertNotEqual(explore['requirements']['missing_documents_and_reviews'],accelerate['requirements']['missing_documents_and_reviews'])
        for record in (explore,accelerate):
            shown={v['id']:v['text'] for v in record['fields']}
            for key,value in record['narrative']['fields'].items():self.assertEqual(shown['narrative.'+key],value)
            self.assertTrue(all(value is False for value in record['gates'].values()))
        n=fixture('access_accelerate-narrative.json');del n['fields']['team_preparedness']
        with self.assertRaises(a.AssessmentError):x.validate_narrative(n,'access_accelerate')
        n=fixture('access_explore-narrative.json');n['fields']['invented_performance']='123 GPU hours'
        with self.assertRaises(a.AssessmentError):x.validate_narrative(n,'access_explore')

    def test_each_access_profile_uses_only_fixed_public_source_policy(self):
        for profile in sources.ACCESS_PROFILES:
            source=sources.observation(profile);self.assertEqual(source['url'],sources.URLS['access_discover'])
            self.assertEqual(source['status'],'snapshot_only');self.assertFalse(source['current_requirements_ready'])
            source['status']='robots_blocked_not_verified'
            with self.assertRaises(a.AssessmentError):x.requirements(profile,source)
        with self.assertRaises(a.AssessmentError):sources.observation('access_maximize',refresh=True)

    def test_explore_no_pdf_does_not_overwrite_or_hide_failed_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();p=preview('access_explore');saved=root/'preview';saved.write_bytes(x.export_bytes(p));out=root/'package'
            r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256','0'*64,'--output',out)
            self.assertNotEqual(r.returncode,0);self.assertFalse(out.exists())
            out.mkdir();(out/'original').write_text('preserve')
            r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256',p['preview_sha256'],'--output',out)
            self.assertNotEqual(r.returncode,0);self.assertEqual([p.name for p in out.iterdir()],['original'])


if __name__=='__main__':unittest.main()
