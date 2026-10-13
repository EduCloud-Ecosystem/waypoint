"""Schema controls only; fabricated examples are not runtime acceptance."""
from copy import deepcopy
import unittest

import authenticated_evidence as evidence


def example():
    return {'version':evidence.VERSION,'status':'PASS','scope':evidence.SCOPE,'phases':list(evidence.PHASES),
        'images':{'course':'sha256:'+'a'*64,'hub':'sha256:'+'b'*64,'controller':'sha256:'+'c'*64},
        'source':'a'*40,'source_dirty':False,'code_sha256':{path:'a'*64 for path in evidence.CODE_FILES},
        'recipe_sha256':{path:'b'*64 for path in evidence.RECIPE_FILES},'host':{'memory_bytes':6656*1024**2,'cpu':4},
        'cleanup_verified':True,'acceptance_eligible':True}


class AuthenticatedEvidenceTests(unittest.TestCase):
    def refused(self,report,**kwargs):
        with self.assertRaisesRegex(ValueError,'^authenticated_runtime_evidence_refused$'):evidence.validate_report(report,**kwargs)

    def test_exact_complete_clean_boundary_report_validated_without_mutation(self):
        report=example();before=deepcopy(report);self.assertIsNone(evidence.validate_report(report));self.assertEqual(report,before)
        self.assertIn('rehearsal/verify_authenticated_runtime.py',evidence.CODE_FILES);self.assertEqual(len(evidence.CODE_FILES),14)
        self.assertIn('.dockerignore',evidence.RECIPE_FILES);self.assertEqual(len(evidence.PHASES),12)

    def test_every_phase_missing_duplicate_out_of_order_or_extra_is_refused(self):
        for phase in evidence.PHASES:
            report=example();report['phases'].remove(phase)
            with self.subTest(missing=phase):self.refused(report)
        for phases in [list(evidence.PHASES)+[evidence.PHASES[0]],list(reversed(evidence.PHASES)),['private_token'],None]:
            report=example();report['phases']=phases;self.refused(report)

    def test_unknown_fields_and_private_nested_values_are_refused(self):
        for key in ('logs','request','receipt','authority_id','container_id','pid','certfile','keyfile','saved_content','worker_id','url','failure'):
            report=example();report[key]='private';self.refused(report)
        for field,key in [('images','keyfile'),('code_sha256','private.key'),('recipe_sha256','private.env'),('host','hostname')]:
            report=example();report[field][key]='private';self.refused(report)

    def test_code_and_recipe_maps_require_exact_paths_and_sha256_not_mutable_labels(self):
        for field,paths in [('code_sha256',evidence.CODE_FILES),('recipe_sha256',evidence.RECIPE_FILES)]:
            for path in paths:
                report=example();del report[field][path]
                with self.subTest(field=field,path=path):self.refused(report)
            for bad in ('latest','sha256:'+'a'*64,'A'*64,'a'*63,True,None):
                report=example();report[field][next(iter(paths))]=bad;self.refused(report)
        for image in ('course','hub','controller'):
            report=example();report['images'][image]='latest';self.refused(report)
        report=example();report['source']='main';self.refused(report)
        report=example();report['source']='a'*64;self.refused(report)

    def test_host_observations_are_typed_bounded_and_meet_minimum(self):
        for field,bad in [('memory_bytes',6656*1024**2-1),('memory_bytes',float(6656*1024**2)),('memory_bytes',True),('memory_bytes',2**50+1),
                          ('cpu',3),('cpu',4.0),('cpu',True),('cpu',float('nan')),('cpu',4097)]:
            report=example();report['host'][field]=bad
            with self.subTest(field=field,bad=bad):self.refused(report)

    def test_cleanup_and_required_schema_cannot_be_promoted_by_truthy_values(self):
        for field in ('cleanup_verified','acceptance_eligible'):
            for bad in (False,1,'true',None):
                report=example();report[field]=bad;self.refused(report)
        for key in evidence.COMPLETE:
            report=example();del report[key]
            with self.subTest(missing=key):self.refused(report)
        for field,bad in [('version','wrong'),('scope','actual-two-host-production'),('status','SUCCESS'),('source_dirty',0)]:
            report=example();report[field]=bad;self.refused(report)

    def test_dirty_development_report_is_explicitly_ineligible_even_when_all_phases_pass(self):
        report=example();report['source_dirty']=True;report['acceptance_eligible']=False
        self.refused(report);self.assertIsNone(evidence.validate_report(report,require_clean=False))
        report['acceptance_eligible']=True;self.refused(report,require_clean=False)
        for flag in (0,1,None,'false'):self.refused(example(),require_clean=flag)

    def test_fail_diagnostics_can_never_claim_acceptance_or_include_secret_failure_text(self):
        report={'version':evidence.VERSION,'status':'FAIL','scope':evidence.SCOPE,'phases':list(evidence.PHASES[:2]),
                'cleanup_verified':False,'acceptance_eligible':False,'failure':'RuntimeError'}
        self.assertIsNone(evidence.validate_report(report))
        for bad in ('secret API key','RuntimeError: private details',{'logs':'private'},True):
            value=deepcopy(report);value['failure']=bad;self.refused(value)
        value=deepcopy(report);value['acceptance_eligible']=True;self.refused(value)
        value=deepcopy(report);value['phases']=[evidence.PHASES[1]];self.refused(value)
        value=example();value['status']='FAIL';value['acceptance_eligible']=False;value['failure']='AssertionError';self.assertIsNone(evidence.validate_report(value))


if __name__=='__main__':unittest.main()
