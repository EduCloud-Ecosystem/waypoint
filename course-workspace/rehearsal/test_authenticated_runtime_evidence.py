"""Schema controls only; fabricated examples are not runtime acceptance."""
from copy import deepcopy
import unittest

import authenticated_evidence as evidence


def example(cpu=4):
    return {'version':evidence.VERSION,'status':'PASS','scope':evidence.SCOPE,'phases':list(evidence.PHASES),
        'images':{'course':'sha256:'+'a'*64,'hub':'sha256:'+'b'*64,'controller':'sha256:'+'c'*64},
        'source':'a'*40,'source_dirty':False,'code_sha256':{path:'a'*64 for path in evidence.CODE_FILES},
        'recipe_sha256':{path:'b'*64 for path in evidence.RECIPE_FILES},'host':{'memory_bytes':6656*1024**2,'cpu':cpu},
        'allocation':evidence.allocation_for_cpu(cpu),
        'cleanup_verified':True,'acceptance_eligible':True}


class AuthenticatedEvidenceTests(unittest.TestCase):
    def refused(self,report,**kwargs):
        with self.assertRaisesRegex(ValueError,'^authenticated_runtime_evidence_refused$'):evidence.validate_report(report,**kwargs)

    def test_exact_complete_clean_boundary_report_validated_without_mutation(self):
        report=example();before=deepcopy(report);self.assertIsNone(evidence.validate_report(report));self.assertEqual(report,before)
        self.assertIn('rehearsal/verify_authenticated_runtime.py',evidence.CODE_FILES);self.assertEqual(len(evidence.CODE_FILES),15)
        self.assertIn('rehearsal/authenticated_evidence.py',evidence.CODE_FILES)
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

    def test_verified_host_cpu_deterministically_selects_exact_allocation(self):
        for cpu,name in ((4,'four-cpu-ci'),(5,'five-cpu-local'),(6,'six-cpu-local'),(64,'six-cpu-local')):
            report=example(cpu);self.assertIsNone(evidence.validate_report(report))
            self.assertEqual(report['allocation']['name'],name)
            report['allocation']=deepcopy(evidence.ALLOCATION_PRESETS['five-cpu-local' if cpu==4 else 'four-cpu-ci'])
            with self.subTest(cpu=cpu):self.refused(report)

    def test_allocation_fields_limits_types_and_unknown_values_fail_closed(self):
        for cpu in (4,5,6):
            for field in example(cpu)['allocation']:
                report=example(cpu);del report['allocation'][field]
                with self.subTest(cpu=cpu,missing=field):self.refused(report)
            for field,bad in [('name','automatic'),('learner_cpu',.2),('hub_cpu',.5),('reserved_cpu',3.9),
                              ('minimum_cpu',True),('minimum_cpu',4.0),('minimum_cpu',7),
                              ('learner_cpu',True),('hub_cpu',True),('reserved_cpu',True),
                              ('controller_cpu',True),('guardian_cpu',True),('controller_cpu',.2),('guardian_cpu',.2),
                              ('host_reserve_cpu',True),('host_reserve_cpu',.6),
                              ('learner_cpu',float('nan')),('reserved_cpu',float('inf')),
                              ('controller_cpu',float('nan')),('guardian_cpu',float('inf')),('host_reserve_cpu',float('nan'))]:
                report=example(cpu);report['allocation'][field]=bad
                with self.subTest(cpu=cpu,field=field,bad=bad):self.refused(report)
            report=example(cpu);report['allocation']['worker_id']='private';self.refused(report)
        report=example();del report['allocation'];self.refused(report)

    def test_legacy_pass_and_failed_history_keep_distinct_version_and_code_schema(self):
        report=example();report['version']=evidence.LEGACY_VERSION;del report['allocation']
        report['code_sha256']={path:'a'*64 for path in evidence.LEGACY_CODE_FILES}
        before=deepcopy(report);self.assertIsNone(evidence.validate_report(report));self.assertEqual(report,before)
        self.assertEqual(len(evidence.LEGACY_CODE_FILES),14)
        report['status']='FAIL';report['acceptance_eligible']=False;report['failure']='RuntimeError'
        self.assertIsNone(evidence.validate_report(report))
        report={'version':evidence.LEGACY_VERSION,'status':'FAIL','scope':evidence.SCOPE,'phases':[],
                'cleanup_verified':True,'acceptance_eligible':False,'failure':'RuntimeError'}
        self.assertIsNone(evidence.validate_report(report))

    def test_legacy_schema_cannot_be_relabelled_or_silently_promoted_to_v2(self):
        report=example();report['version']=evidence.LEGACY_VERSION
        self.refused(report)  # Old reports cannot contain new allocation fields.
        del report['allocation'];self.refused(report)  # Nor the new dependency hash.
        report['code_sha256']={path:'a'*64 for path in evidence.LEGACY_CODE_FILES}
        self.assertIsNone(evidence.validate_report(report))
        report['version']=evidence.VERSION;self.refused(report)
        report['allocation']=deepcopy(evidence.ALLOCATION_PRESETS['four-cpu-ci']);self.refused(report)

    def test_historical_v2_retains_old_allocation_and_cannot_be_promoted_to_v3(self):
        for cpu in (4,5,6,64):
            report=example(cpu);report['version']=evidence.PREVIOUS_VERSION
            report['allocation']=deepcopy(evidence.LEGACY_ALLOCATION_PRESETS['four-cpu-ci' if cpu==4 else 'five-cpu-local'])
            before=deepcopy(report);evidence.validate_report(report);self.assertEqual(report,before)
            report['version']=evidence.VERSION;self.refused(report)
            report=before;report['allocation']=evidence.allocation_for_cpu(cpu);self.refused(report)
        report=example(6);report['version']=evidence.PREVIOUS_VERSION;self.refused(report)

    def test_canonical_allocation_is_independent_typed_and_covers_control_plane(self):
        for cpu in (4,5,6,8,4096):
            allocation=evidence.allocation_for_cpu(cpu)
            self.assertEqual(set(allocation),{'name','learner_cpu','hub_cpu','reserved_cpu','minimum_cpu','controller_cpu','guardian_cpu','host_reserve_cpu'})
            self.assertEqual(3*(allocation['hub_cpu']+allocation['learner_cpu'])+allocation['host_reserve_cpu'],allocation['reserved_cpu'])
            self.assertLessEqual(allocation['controller_cpu']+2*allocation['guardian_cpu'],allocation['host_reserve_cpu'])
            self.assertLessEqual(allocation['reserved_cpu'],cpu)
            allocation['controller_cpu']=999
            self.assertNotEqual(evidence.allocation_for_cpu(cpu)['controller_cpu'],999)
        for cpu in (True,False,4.0,3,-1,'6',None,4097):
            with self.subTest(cpu=cpu),self.assertRaisesRegex(ValueError,'^authenticated_runtime_allocation_refused$'):
                evidence.allocation_for_cpu(cpu)

    def test_current_reservation_cannot_omit_or_understate_controller_or_guardians(self):
        for cpu in (4,5,6):
            for field in ('controller_cpu','guardian_cpu','host_reserve_cpu'):
                report=example(cpu);report['allocation'][field]=0;self.refused(report)
            report=example(cpu)
            report['allocation']['reserved_cpu']=3*(report['allocation']['hub_cpu']+report['allocation']['learner_cpu'])
            self.refused(report)

    def test_partial_v2_failure_validates_observed_allocation_without_acceptance(self):
        report=example(5);report['status']='FAIL';report['acceptance_eligible']=False
        report['phases']=list(evidence.PHASES[:2]);report['failure']='RuntimeError'
        self.assertIsNone(evidence.validate_report(report))
        report['allocation']=deepcopy(evidence.ALLOCATION_PRESETS['four-cpu-ci']);self.refused(report)
        del report['allocation'];self.assertIsNone(evidence.validate_report(report))

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
