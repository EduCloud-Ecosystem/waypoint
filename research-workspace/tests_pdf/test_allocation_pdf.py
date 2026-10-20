"""Required bounded PDF integration suite when invoked; no skip-to-green path."""
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader
from research_workspace import assessment as a, allocation_export as x, allocation_pdf as pdf, allocation_sources as sources

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT/'assessment/fixtures/export'


def fixture(name): return json.loads((FIXTURES/name).read_text())


class PDFIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font_path=Path(os.environ['ASSESSMENT_EXPORT_FONT']).resolve(strict=True)
        cls.font=pdf.inspect_font(x.read_bytes(cls.font_path,pdf.FONT_LIMIT))[0]
        cls.font_bytes=x.read_bytes(cls.font_path,pdf.FONT_LIMIT)

    def preview(self,profile='access_discover',narrative=None):
        kind='classroom' if profile=='nairr_classroom_draft' else 'research'
        return x.make_preview(profile,fixture(kind+'-estimate.json'),[fixture(kind+'-manifest.json')],
            narrative or fixture(profile+'-narrative.json'),self.font,sources.observation(profile),include_planning_pdf=profile=='access_explore')

    def cli(self,*args):
        return subprocess.run([sys.executable,'-m','research_workspace',*map(str,args)],cwd=ROOT,capture_output=True,timeout=20)

    def test_supported_pdf_cli_profiles_metadata_parity_and_package_hashes(self):
        for profile in x.PROFILES:
            with self.subTest(profile=profile),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();kind='classroom' if profile=='nairr_classroom_draft' else 'research'
                r=self.cli('assessment-export-preview',FIXTURES/(kind+'-estimate.json'),'--manifest',FIXTURES/(kind+'-manifest.json'),
                    '--profile',profile,'--narrative',FIXTURES/(profile+'-narrative.json'),'--font',self.font_path,
                    *(['--include-planning-pdf'] if profile=='access_explore' else []))
                self.assertEqual(r.returncode,0,r.stderr.decode());p=json.loads(r.stdout);saved=root/'preview.json';saved.write_bytes(r.stdout)
                out=root/'package'
                with patch.object(sources,'observation',side_effect=AssertionError('hidden refresh')):
                    # Direct package path is network-free; separate CLI exercises real delivery.
                    x.package_files(p,p['preview_sha256'],self.font_bytes)
                r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256',p['preview_sha256'],'--font',self.font_path,'--output',out)
                self.assertEqual(r.returncode,0,r.stderr.decode());self.assertEqual(json.loads(r.stdout)['status'],'COMPLETE_DRAFT')
                self.assertEqual(json.loads(r.stdout)['package_sha256'],hashlib.sha256((out/'package.json').read_bytes()).hexdigest())
                self.assertFalse((out/'INCOMPLETE').exists())
                self.assertEqual((out/'source.json').read_bytes(),(out/'worksheet.json').read_bytes())
                self.assertEqual(json.loads((out/'worksheet.json').read_bytes()),p['record'])
                rows=list(csv.reader(io.StringIO((out/'worksheet.csv').read_text())))
                self.assertEqual(rows[0],['json_pointer','json_value'])
                self.assertEqual([(a[1:],b[1:]) for a,b in rows[1:]],list(x.worksheet_rows(p['record'])))
                reader=PdfReader(out/'draft.pdf',strict=True)
                self.assertLessEqual(len(reader.pages),x.PROFILES[profile]['pages'])
                text=''.join(page.extract_text() for page in reader.pages)
                for f in p['record']['fields']:
                    self.assertIn(''.join(f['text'].split()),''.join(text.split()))
                self.assertEqual(reader.metadata.author,'')
                self.assertNotIn(str(root),json.dumps(dict(reader.metadata)))
                for k in ('/OpenAction','/AA','/Names','/AcroForm'):self.assertNotIn(k,reader.trailer['/Root'])
                def margins(text, cm, tm, font_dict, font_size):
                    if not text.strip(): return
                    self.assertGreaterEqual(font_size,11)
                    # pypdf's visitor positions drift for multiline T* text.
                    # Check the direct single-line header/footer anchors here;
                    # local acceptance measures every character with pdfplumber.
                    anchors={x.PROFILES[profile]['title']+' - DRAFT',
                             'Local operator draft; no submission or resource authority.'}
                    if text.strip() not in anchors and not text.strip().startswith('Page '): return
                    self.assertEqual(cm[:4],[1,0,0,1]);self.assertEqual(tm[:4],[1,0,0,1])
                    descriptor=font_dict['/FontDescriptor'].get_object()
                    baseline=cm[5]+tm[5]
                    self.assertLessEqual(baseline+float(descriptor['/Ascent'])*font_size/1000,720.001)
                    self.assertGreaterEqual(baseline+float(descriptor['/Descent'])*font_size/1000,71.999)
                for page in reader.pages:
                    self.assertFalse(page.get('/Annots'));page.extract_text(visitor_text=margins)
                manifest=json.loads((out/'package.json').read_bytes())
                self.assertEqual(manifest['status'],'PREPARED_DRAFT')
                for name,meta in manifest['files'].items():
                    data=(out/name).read_bytes();self.assertEqual(meta['size_bytes'],len(data));self.assertEqual(meta['sha256'],hashlib.sha256(data).hexdigest())
                self.assertTrue(all(v is False for v in manifest['gates'].values()))
                self.assertNotIn(str(self.font_path).encode(),b''.join(f.read_bytes() for f in out.iterdir()))

    def test_digest_mismatch_does_not_create_output_and_no_private_error_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();p=self.preview();saved=root/'PRIVATE_SENTINEL.json';saved.write_bytes(x.export_bytes(p));out=root/'out'
            r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256','a'*64,'--font',self.font_path,'--output',out)
            self.assertNotEqual(r.returncode,0);self.assertFalse(out.exists());self.assertNotIn(b'PRIVATE_SENTINEL',r.stderr+r.stdout)

    def test_unknown_metadata_is_rejected_through_real_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();value=fixture('research-estimate.json');value['series'][0]['raw_log']='PRIVATE_SENTINEL'
            saved=root/'estimate';saved.write_bytes(x.export_bytes(value))
            r=self.cli('assessment-export-preview',saved,'--manifest',FIXTURES/'research-manifest.json','--profile','access_discover',
                '--narrative',FIXTURES/'access_discover-narrative.json','--font',self.font_path)
            self.assertNotEqual(r.returncode,0);self.assertNotIn(b'PRIVATE_SENTINEL',r.stderr+r.stdout)

    def test_no_overwrite_real_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();p=self.preview();saved=root/'preview';saved.write_bytes(x.export_bytes(p));out=root/'out';out.mkdir();(out/'owned').write_bytes(b'unchanged')
            r=self.cli('assessment-export','--preview',saved,'--approved-preview-sha256',p['preview_sha256'],'--font',self.font_path,'--output',out)
            self.assertNotEqual(r.returncode,0);self.assertEqual(list(out.iterdir()),[out/'owned']);self.assertEqual((out/'owned').read_bytes(),b'unchanged')

    def test_page_overflow_refuses_without_truncating_or_shrinking(self):
        n=fixture('access_discover-narrative.json');n['fields']['objectives']='Lengthy justification. '*85
        n['fields']['resource_needs']='Additional resource justification. '*55
        p=self.preview(narrative=n)
        with self.assertRaisesRegex(a.AssessmentError,'page limit|page area'):x.package_files(p,p['preview_sha256'],self.font_bytes)

    def test_unsupported_glyph_refuses_instead_of_substitution(self):
        n=fixture('access_discover-narrative.json');n['fields']['title']='Unsupported character \U0001f680'
        p=self.preview(narrative=n)
        with self.assertRaisesRegex(a.AssessmentError,'glyph'):x.package_files(p,p['preview_sha256'],self.font_bytes)

    def test_font_change_and_invalid_font_refuse(self):
        p=self.preview();p['record']['font']['sha256']='b'*64;p['preview_sha256']=x.export_digest(p['record'])
        with self.assertRaisesRegex(a.AssessmentError,'font differs'):x.package_files(p,p['preview_sha256'],self.font_bytes)
        with self.assertRaises(a.AssessmentError):pdf.inspect_font(b'not-a-font')

    def test_formula_cells_and_literal_markup_are_preserved_as_text(self):
        n=fixture('access_discover-narrative.json');n['fields']['objectives']='=HYPERLINK("https://invalid")\n<font size="1">Literal text</font>'
        p=self.preview(narrative=n);files=x.package_files(p,p['preview_sha256'],self.font_bytes)
        rows=list(csv.reader(io.StringIO(files['worksheet.csv'].decode())))
        self.assertTrue(all(a.startswith("'") and b.startswith("'") for a,b in rows[1:]))
        text=''.join(page.extract_text() for page in PdfReader(io.BytesIO(files['draft.pdf'])).pages)
        self.assertIn('<font size="1">Literal text</font>',text)
        self.assertFalse(any(page.get('/Annots') for page in PdfReader(io.BytesIO(files['draft.pdf'])).pages))

    def test_multpage_nairr_keeps_every_section(self):
        n=fixture('nairr_research_draft-narrative.json')
        for key in n['fields']:
            if key not in ('title','preferred_resource','alternative_resource'):n['fields'][key]='Reviewable narrative with explicit assumptions. '*30
        p=self.preview('nairr_research_draft',n);files=x.package_files(p,p['preview_sha256'],self.font_bytes)
        reader=PdfReader(io.BytesIO(files['draft.pdf']));self.assertGreater(len(reader.pages),1)
        text=''.join(page.extract_text() for page in reader.pages)
        for f in p['record']['fields']:self.assertIn(''.join(f['text'].split()),''.join(text.split()))

    def test_explore_internal_and_accelerate_document_caps_refuse_overflow(self):
        for profile in ('access_explore','access_accelerate'):
            with self.subTest(profile=profile):
                n=fixture(profile+'-narrative.json')
                for name in n['fields']:
                    if name!='title':n['fields'][name]='Synthetic planning text for review. '*50
                p=self.preview(profile,n)
                with self.assertRaisesRegex(a.AssessmentError,'page limit|page area'):
                    x.package_files(p,p['preview_sha256'],self.font_bytes)

    def test_unimplemented_tiers_refuse_at_cli_before_inputs(self):
        r=self.cli('assessment-export-preview','missing','--manifest','missing','--profile','access_maximize','--narrative','missing','--font','missing')
        self.assertNotEqual(r.returncode,0);self.assertIn(b'no complete export profile',r.stderr)


if __name__=='__main__':unittest.main()
