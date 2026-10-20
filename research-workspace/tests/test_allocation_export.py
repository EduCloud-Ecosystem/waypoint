"""Closed metadata, approval and publication boundaries without optional PDF deps."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_workspace import assessment as a, allocation_export as x, allocation_sources as sources

FIXTURES = Path(__file__).resolve().parents[1] / 'assessment/fixtures/export'
FONT = {'sha256': '4dd9761b058c009db9b2145f55ee6617d093c273dd3d6c5b3e43ff6110fa9bf6',
        'family': 'cmr10', 'postscript_name': 'Cmr10', 'size_bytes': 26348,
        'provenance': 'operator_supplied_font_digest_approved'}


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def preview(profile='access_discover', *, include_planning_pdf=False):
    name = 'classroom' if profile == 'nairr_classroom_draft' else 'research'
    return x.make_preview(profile, fixture(name+'-estimate.json'), [fixture(name+'-manifest.json')],
                          fixture(profile+'-narrative.json'), None if profile=='access_explore' and not include_planning_pdf else FONT, sources.observation(profile),
                          include_planning_pdf=include_planning_pdf)


class ExportBoundaries(unittest.TestCase):
    def test_all_profiles_are_drafts_and_narrative_is_visible(self):
        for profile in x.PROFILES:
            record = x.validate_preview(preview(profile))
            self.assertTrue(all(value is False for value in record['gates'].values()))
            self.assertFalse(record['requirements']['submission_ready'])
            shown = {f['id']: f['text'] for f in record['fields']}
            for key, text in record['narrative']['fields'].items():
                self.assertEqual(shown['narrative.'+key], text)

    def test_unknown_profiles_never_fall_back(self):
        for profile in ('access_maximize','discover','nairr','access_unknown'):
            with self.assertRaises(a.AssessmentError): x.profile_info(profile)

    def test_unknown_metadata_sentinels_refused_at_nested_boundaries(self):
        base = fixture('research-estimate.json')
        for path in ((), ('series',0), ('series',0,'software'), ('series',0,'models','cpu_consumed'),
                     ('series',0,'cleanup_claims',0), ('series',0,'totals','cpu_consumed'),
                     ('series',0,'capacity_observations','peak_memory')):
            value=copy.deepcopy(base); cursor=value
            for key in path: cursor=cursor[key]
            cursor['raw_uploaded_data_sha256']='PRIVATE_SENTINEL'
            with self.assertRaises(a.AssessmentError): x.validate_estimate(value,[fixture('research-manifest.json')])

    def test_tampered_math_coefficients_heldout_variability_and_prediction_refused(self):
        paths = [('coefficients','intercept_nanoseconds','numerator'), ('prediction','nanoseconds','numerator'),
                 ('held_out','errors',0,'absolute_error_nanoseconds','numerator'), ('repeat_variability',0,'median','numerator')]
        for path in paths:
            value=fixture('research-estimate.json'); cursor=value['series'][0]['models']['cpu_consumed']
            for key in path[:-1]: cursor=cursor[key]
            cursor[path[-1]]='999999'
            with self.assertRaises(a.AssessmentError): x.validate_estimate(value,[fixture('research-manifest.json')])

    def test_manifest_and_cleanup_bindings_refused(self):
        for change in ('digest','scope','duplicate_manifest'):
            value=fixture('research-estimate.json'); manifests=[fixture('research-manifest.json')]
            if change=='digest': value['series'][0]['manifest_sha256']='a'*64
            if change=='scope': value['series'][0]['cleanup_claims'][0]['observed_scopes'][1]['scope_id']='f'*32
            if change=='duplicate_manifest': manifests*=2
            with self.assertRaises(a.AssessmentError): x.validate_estimate(value,manifests)

    def test_unknown_overhead_failed_oom_and_mixed_scope_are_preserved(self):
        from test_assessment_estimation import calculate, fixture as series_fixture
        scenarios=[]
        series=series_fixture();scenario=series_fixture('scenario.json');scenario['overheads']['setup']['cpu_nanoseconds']=None
        scenarios.append((series,scenario))
        series=series_fixture();series['receipts'][0]['measurement']['execution']['failure_category']='out_of_memory'
        series['receipts'][0]['measurement']['execution']['status']='failed';scenarios.append((series,None))
        series=series_fixture();series['receipts'][0]['measurement']['measured_consumption']['cpu_consumed']['scope']='sandbox'
        scenarios.append((series,None))
        for series,scenario in scenarios:
            output=calculate(series,scenario);x.validate_estimate(output,[series['manifest']])
            p=x.make_preview('access_discover',output,[series['manifest']],fixture('access_discover-narrative.json'),FONT,sources.observation('access_discover'))
            self.assertEqual(p['record']['estimate'],output)
            self.assertTrue(all(v is False for v in p['record']['gates'].values()))

    def test_instructional_profile_requires_instructional_manifest(self):
        with self.assertRaises(a.AssessmentError):
            x.make_preview('nairr_classroom_draft',fixture('research-estimate.json'),[fixture('research-manifest.json')],
                fixture('nairr_classroom_draft-narrative.json'),FONT,sources.observation('nairr_classroom_draft'))

    def test_preview_digest_covers_every_visible_narrative_character(self):
        first=preview(); second=copy.deepcopy(first)
        second['record']['narrative']['fields']['objectives']+=' Added.'
        with self.assertRaises(a.AssessmentError): x.validate_preview(second)
        r=second['record']; rebuilt=x.make_preview(r['profile'],r['estimate'],r['manifests'],r['narrative'],r['font'],r['requirements']['source'])
        self.assertNotEqual(first['preview_sha256'],rebuilt['preview_sha256'])

    def test_unknown_preview_fields_and_true_gates_refused_even_if_rehashed(self):
        for key in ('hidden','gates','fields'):
            value=preview()
            if key=='hidden': value['record']['private_path']='/PRIVATE_SENTINEL'
            elif key=='gates': value['record']['gates']['cleanup_trusted']=True
            else: value['record']['fields'][0]['text']='Hidden substitution'
            value['preview_sha256']=x.export_digest(value['record'])
            with self.assertRaises(a.AssessmentError): x.validate_preview(value)

    def test_narrative_controls_unknown_fields_and_bounds(self):
        for text in ('', 'x'*2049, 'a\x00b', 'a\u202eb', 'e\u0301'):
            n=fixture('access_discover-narrative.json');n['fields']['objectives']=text
            with self.assertRaises(a.AssessmentError): x.validate_narrative(n,'access_discover')
        n=fixture('access_discover-narrative.json');n['raw_log']='PRIVATE_SENTINEL'
        with self.assertRaises(a.AssessmentError): x.validate_narrative(n,'access_discover')

    def test_supported_unicode_is_preserved_not_silently_replaced(self):
        n=fixture('access_discover-narrative.json');n['fields']['objectives']='Café\nMultiline review.'
        self.assertEqual(x.validate_narrative(n,'access_discover'),n)

    def test_reader_rejects_duplicates_floats_depth_and_hardlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();path=root/'input'
            for payload in (b'{"x":1,"x":2}',b'{"x":1.5}',b'{"x":NaN}',b'{"x":'+b'['*24+b'0'+b']'*24+b'}'):
                path.write_bytes(payload)
                with self.assertRaises(a.AssessmentError): x.read_export_document(path)
            path.write_bytes(b'{"x":1}');os.link(path,root/'other')
            with self.assertRaises(a.AssessmentError): x.read_export_document(path)

    def test_reader_refuses_final_symlink_and_oversized_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();path=root/'input';path.write_bytes(b'{"x":1}')
            (root/'link').symlink_to(path)
            with self.assertRaises(a.AssessmentError): x.read_export_document(root/'link')
            with self.assertRaises(a.AssessmentError): x.read_export_document(path,limit=2)

    def test_requirements_are_fixed_urls_and_cannot_be_promoted_by_retrieval(self):
        value=preview();source=value['record']['requirements']['source']
        source.update(status='retrieved_unreviewed',retrieved_at='2026-10-19T01:00:00Z',content_sha256='a'*64)
        self.assertFalse(x.requirements('access_discover',source)['submission_ready'])
        source['url']='https://private.example/upload'
        with self.assertRaises(a.AssessmentError): x.requirements('access_discover',source)

    def test_nairr_robots_denial_is_not_retried(self):
        with patch.object(sources.subprocess,'Popen',side_effect=AssertionError('network')):
            for p in ('nairr_research_draft','nairr_classroom_draft'):
                self.assertEqual(sources.observation(p,refresh=True)['status'],'robots_blocked_not_verified')

    def test_refresh_failure_stays_draft_and_does_not_echo_response(self):
        process=unittest.mock.Mock(returncode=0)
        with patch.object(sources.subprocess,'Popen',return_value=process), patch.object(sources,'collect',side_effect=ValueError('PRIVATE_SENTINEL')):
            result=sources.observation('access_discover',refresh=True)
        self.assertEqual(result['status'],'unavailable');self.assertFalse(result['current_requirements_ready'])
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(result))

    def test_public_refresh_uses_fixed_child_and_no_input(self):
        process=unittest.mock.Mock(returncode=0)
        body=b'{"status":"retrieved_unreviewed","retrieved_at":"2026-10-19T01:00:00Z","content_sha256":"'+b'a'*64+b'"}'
        with patch.object(sources.subprocess,'Popen',return_value=process) as call, patch.object(sources,'collect',return_value=(body,b'')) as collect:
            result=sources.observation('access_discover',refresh=True)
        self.assertEqual(call.call_args.args[0][-1],'research_workspace.allocation_sources')
        self.assertEqual(call.call_args.kwargs['stdin'],sources.subprocess.DEVNULL)
        self.assertEqual(collect.call_args.kwargs['timeout'],12)
        self.assertFalse(result['current_requirements_ready'])

    def test_no_overwrite_or_parent_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();out=root/'package'
            files={'source.json':b'{}','package.json':b'{"status":"COMPLETE_DRAFT"}'}
            x.write_package(str(out),files)
            before={p.name:p.read_bytes() for p in out.iterdir()}
            with self.assertRaises(a.AssessmentError): x.write_package(str(out),files)
            self.assertEqual(before,{p.name:p.read_bytes() for p in out.iterdir()})
            (root/'link').symlink_to(root,target_is_directory=True)
            with self.assertRaises(a.AssessmentError): x.write_package(str(root/'link'/'new'),files)
            self.assertFalse((root/'new').exists())

    def test_interrupted_write_retains_incomplete_and_never_completes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();out=root/'package';real=os.fsync
            count=[0]
            def fail(fd):
                count[0]+=1
                if count[0]==2: raise OSError('injected')
                return real(fd)
            with patch('os.fsync',side_effect=fail), self.assertRaises(a.AssessmentError):
                x.write_package(str(out),{'source.json':b'{}','package.json':b'{}'})
            self.assertTrue((out/'INCOMPLETE').exists());self.assertFalse((out/'package.json').exists())

    def test_final_directory_sync_failure_restores_stop_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp).resolve()/'package';real=os.fsync;count=[0]
            def fail(fd):
                count[0]+=1
                if count[0]==5: raise OSError('final durability failure')
                return real(fd)
            with patch('os.fsync',side_effect=fail),self.assertRaisesRegex(a.AssessmentError,'no completion receipt'):
                x.write_package(str(out),{'source.json':b'{}','package.json':b'{"status":"PREPARED_DRAFT"}'})
            self.assertTrue((out/'INCOMPLETE').exists())
            self.assertEqual(json.loads((out/'package.json').read_bytes())['status'],'PREPARED_DRAFT')

    def test_final_sync_and_marker_recreation_failure_stays_explicitly_uncertain(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp).resolve()/'package';real_sync=os.fsync;real_open=os.open;count=[0];markers=[0]
            def fail_sync(fd):
                count[0]+=1
                if count[0]==5: raise OSError('final durability failure')
                return real_sync(fd)
            def fail_marker(path,*args,**kwargs):
                if path=='INCOMPLETE':
                    markers[0]+=1
                    if markers[0]==2: raise OSError('marker recreation failure')
                return real_open(path,*args,**kwargs)
            with patch('os.fsync',side_effect=fail_sync),patch('os.open',side_effect=fail_marker),self.assertRaisesRegex(a.AssessmentError,'marker could not be confirmed'):
                x.write_package(str(out),{'source.json':b'{}','package.json':b'{"status":"PREPARED_DRAFT"}'})
            self.assertFalse((out/'INCOMPLETE').exists())
            self.assertEqual(json.loads((out/'package.json').read_bytes())['status'],'PREPARED_DRAFT')

    def test_preview_does_not_share_mutable_input_references(self):
        n=fixture('access_discover-narrative.json');font=copy.deepcopy(FONT)
        p=x.make_preview('access_discover',fixture('research-estimate.json'),[fixture('research-manifest.json')],n,font,sources.observation('access_discover'))
        n['fields']['title']='Changed outside preview';font['sha256']='f'*64
        self.assertNotEqual(p['record']['font']['sha256'],font['sha256']);x.validate_preview(p)

    def test_csv_projection_preserves_original_leading_formula_text(self):
        data={'formula':'=HYPERLINK("https://invalid")','negative':'-1','original_quote':"'value",'space':' \n@cmd'}
        rows=dict(x.worksheet_rows(data))
        for k,v in data.items(): self.assertEqual(json.loads(rows['/'+k]),v)

    def test_consent_failure_precedes_renderer(self):
        p=preview()
        with patch('research_workspace.allocation_pdf.render_pdf',side_effect=AssertionError('must not render')):
            with self.assertRaises(a.AssessmentError):x.package_files(p,'0'*64,b'font')


if __name__ == '__main__': unittest.main()
