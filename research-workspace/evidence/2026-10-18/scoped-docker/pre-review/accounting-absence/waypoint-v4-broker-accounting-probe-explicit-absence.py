"""Pure scratch accounting probe. No Audit initialization, markers, or processes."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'/tmp/waypoint-v4-broker-review.pC2Y1y/research-workspace')
sys.path.insert(0,'/tmp/waypoint-v4-broker-review.pC2Y1y/research-workspace/tests')
import qualify_scoped_docker as q
from test_scoped_docker_qualification import complete_report,OracleTests
names=[name for name in unittest.defaultTestLoader.getTestCaseNames(OracleTests) if name not in ('test_exercise_marker_survives_missing_or_changed_output','test_exclusive_output_and_fifth_attempt_refuse_without_effect')]
with patch.object(q.subprocess,'Popen',side_effect=RuntimeError('Processes forbidden in independent review')):
    result=unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(OracleTests(name) for name in names))
if not result.wasSuccessful(): raise SystemExit('Pure oracle regression failed')
with tempfile.TemporaryDirectory(prefix='waypoint-v4-no-marker-') as temporary:
    audit=object.__new__(q.Audit)
    audit.root=Path(temporary)
    audit.state=audit.root/'state.private'
    audit.state.mkdir()
    (audit.state/'broker').mkdir()
    original=copy.deepcopy(complete_report()['attempts'][0])
    binding=original['binding']; rid=binding['request_id']; pid=binding['project_id']
    binding['ledger_path']=str(audit.state/'broker')
    binding['ledger_identity']['path_sha256']=q.digest(binding['ledger_path'].encode())
    request=q.planned_request(0,rid,pid,binding['ledger_path'])
    request_bytes=q.canonical(request).encode()
    binding['request_sha256']=q.digest(request_bytes)
    record=original['ledger_record']; record['request']=request
    record['payload_hash']=q.digest(json.dumps(request,separators=(',',':')).encode())
    ledger={'version':2,'owner':binding['ledger_identity']['owner'],'authority':{'authority_id':binding['ledger_identity']['authority_id'],'state_path':binding['ledger_path'],'admission':'active'},'runs':{rid:record}}
    (audit.state/'broker/ledger.json').write_bytes(q.encoded(ledger))
    run_dir=audit.state/'runs'/rid; run_dir.mkdir(parents=True)
    (run_dir/'receipt.private.json').write_bytes(q.encoded(original['final_receipt']))
    (audit.root/'1-request.private.json').write_bytes(request_bytes)
    (audit.root/'1-binding.private.json').write_bytes(q.encoded(binding))
    audit.report={'status':'FAIL','checks':{},'artifacts':{},'attempts':[{'binding':binding,'binding_sha256':q.digest(q.encoded(binding)),'outcome':'UNKNOWN_BEFORE_LAUNCH'}],'charged_cpu_seconds':0}
    audit.launch_count=1
    class NoAbsence:
        returncode=1
        stdout=b'[]\n'
        stderr=b'Cannot connect to Docker daemon'
    with patch.object(q.subprocess,'run',return_value=NoAbsence()):
        try: audit.settle(original['run'])
        except ValueError as error:
            print(json.dumps({'boundary':'after successful worker receipt and ledger; independent absence unavailable',
                              'exception':str(error),'report_charge':audit.report['charged_cpu_seconds'],
                              'report_outcome':audit.report['attempts'][0]['outcome'],'dispatch_boundary_entries':audit.launch_count,
                              'retained_receipt_charge':json.loads((run_dir/'receipt.private.json').read_bytes())['compute']['charged_cpu_seconds'],
                              'retained_ledger_charge':record['compute']['charged_cpu_seconds'],
                              'markers_created':0,'broker_processes':0,'docker_attempts':0},sort_keys=True))
        else: raise SystemExit('Expected absence refusal was not raised')
