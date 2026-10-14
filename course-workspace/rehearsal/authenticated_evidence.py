"""Strict public report schema; validation is not independent runtime proof."""
import json
import math
import re

LEGACY_VERSION='waypoint-authenticated-runtime-rehearsal-v1'
PREVIOUS_VERSION='waypoint-authenticated-runtime-rehearsal-v2'
VERSION='waypoint-authenticated-runtime-rehearsal-v3'
SCOPE='actual-local-docker-one-daemon-two-logical-workers-synthetic-learners'
PHASES=(
    'mtls_role_course_and_nonowner_refusal',
    'source_real_python_r_saved_home_and_socketless_hub',
    'peer_real_python_r_saved_home_and_socketless_hub',
    'authority_outage_stops_actual_notebooks',
    'interrupted_transfer_durable_pending_denies_both_endpoints',
    'authenticated_reconciliation_retains_history_and_peer_runtime',
    'restarted_source_hub_cannot_revive_fenced_epoch',
    'receiver_real_python_r_and_coursework_continuity',
    'guardian_loss_authority_watchdog_stops_receiver_preserves_peer',
    'guardian_certificate_revocation_stops_only_its_worker',
    'hub_certificate_revocation_refuses_fresh_access',
    'service_restart_retains_authority_incarnation_epoch_and_barriers',
)
LEGACY_CODE_FILES=frozenset({
    'hub/authority_client.py','hub/authority_spawner.py','hub/home_policy.py','hub/jupyterhub_config.py','hub/settings.py',
    'ownership_contract.py','authority_access.py','authority_operator.py','runtime_authority.py','runtime_guardian.py','controller_service.py',
    'tests/authority_test_pki.py','rehearsal/authenticated_fixture.py','rehearsal/verify_authenticated_runtime.py',
})
CODE_FILES=LEGACY_CODE_FILES|{'rehearsal/authenticated_evidence.py'}
LEGACY_ALLOCATION_PRESETS={
    'four-cpu-ci':{'name':'four-cpu-ci','learner_cpu':.1,'hub_cpu':1.0,'reserved_cpu':4.0,'minimum_cpu':4},
    'five-cpu-local':{'name':'five-cpu-local','learner_cpu':.25,'hub_cpu':1.0,'reserved_cpu':4.5,'minimum_cpu':5},
}
ALLOCATION_PRESETS={
    name:dict(value,controller_cpu=.4,guardian_cpu=.1,host_reserve_cpu=.7 if name=='four-cpu-ci' else .75)
    for name,value in LEGACY_ALLOCATION_PRESETS.items()
}
ALLOCATION_PRESETS['six-cpu-local']={'name':'six-cpu-local','learner_cpu':.25,'hub_cpu':1.0,
    'reserved_cpu':5.25,'minimum_cpu':6,'controller_cpu':1.0,'guardian_cpu':.1,'host_reserve_cpu':1.5}
RECIPE_FILES=frozenset({'.dockerignore','hub/Dockerfile','controller.Dockerfile','course/Dockerfile'})
BASE={'version','status','scope','phases','cleanup_verified','acceptance_eligible'}
LEGACY_COMPLETE=BASE|{'images','source','source_dirty','code_sha256','recipe_sha256','host'}
COMPLETE=LEGACY_COMPLETE|{'allocation'}
ALLOWED=COMPLETE|{'failure'}
FAILURES=frozenset({'RuntimeError','AssertionError','OSError','FileNotFoundError','PermissionError','CalledProcessError','TimeoutExpired',
                    'KeyboardInterrupt','SystemExit','ValueError','TypeError','KeyError','ConnectionError','TimeoutError'})
HASH=re.compile(r'[0-9a-f]{64}\Z')
IMAGE=re.compile(r'sha256:[0-9a-f]{64}\Z')
COMMIT=re.compile(r'[0-9a-f]{40}\Z')


def allocation_for_cpu(cpu):
    """Deterministic disposable-fixture allocation from verified daemon CPUs."""
    if type(cpu) is not int or not 4<=cpu<=4096:raise ValueError('authenticated_runtime_allocation_refused')
    name='six-cpu-local' if cpu>=6 else ('five-cpu-local' if cpu==5 else 'four-cpu-ci')
    return dict(ALLOCATION_PRESETS[name])


def _hash_map(value,keys,pattern):
    return isinstance(value,dict) and set(value)==keys and all(isinstance(item,str) and pattern.fullmatch(item) for item in value.values())


def _validate(report,require_clean):
    if type(require_clean) is not bool or not isinstance(report,dict) or not BASE<=set(report)<=ALLOWED:raise ValueError()
    if (report['version'] not in (VERSION,PREVIOUS_VERSION,LEGACY_VERSION) or report['scope']!=SCOPE or report['status'] not in ('PASS','FAIL')
            or type(report['cleanup_verified']) is not bool or type(report['acceptance_eligible']) is not bool):raise ValueError()
    legacy=report['version']==LEGACY_VERSION
    complete=LEGACY_COMPLETE if legacy else COMPLETE
    if not set(report)<=complete|{'failure'}:raise ValueError()
    phases=report['phases']
    if not isinstance(phases,list) or any(not isinstance(phase,str) for phase in phases) or phases!=list(PHASES[:len(phases)]):raise ValueError()
    if 'images' in report and not _hash_map(report['images'],{'course','hub','controller'},IMAGE):raise ValueError()
    if 'source' in report and (not isinstance(report['source'],str) or not COMMIT.fullmatch(report['source'])):raise ValueError()
    if 'source_dirty' in report and (type(report['source_dirty']) is not bool or require_clean and report['source_dirty']):raise ValueError()
    if 'code_sha256' in report and not _hash_map(report['code_sha256'],LEGACY_CODE_FILES if legacy else CODE_FILES,HASH):raise ValueError()
    if 'recipe_sha256' in report and not _hash_map(report['recipe_sha256'],RECIPE_FILES,HASH):raise ValueError()
    if 'host' in report:
        host=report['host']
        if (not isinstance(host,dict) or set(host)!={'memory_bytes','cpu'} or type(host['memory_bytes']) is not int
                or not 6656*1024**2<=host['memory_bytes']<=2**50 or type(host['cpu']) is not int or not 4<=host['cpu']<=4096):raise ValueError()
    if 'allocation' in report:
        allocation=report['allocation']
        modern=report['version']==VERSION
        numeric={'learner_cpu','hub_cpu','reserved_cpu'}|({'controller_cpu','guardian_cpu','host_reserve_cpu'} if modern else set())
        if (not isinstance(allocation,dict) or set(allocation)!=numeric|{'name','minimum_cpu'}
                or any(type(allocation[key]) not in (int,float) or not math.isfinite(allocation[key]) for key in numeric)
                or type(allocation['minimum_cpu']) is not int or 'host' not in report):raise ValueError()
        expected=(allocation_for_cpu(report['host']['cpu']) if modern else
                  LEGACY_ALLOCATION_PRESETS['five-cpu-local' if report['host']['cpu']>=5 else 'four-cpu-ci'])
        if allocation!=expected:raise ValueError()
        if modern and (allocation['reserved_cpu']!=3*(allocation['hub_cpu']+allocation['learner_cpu'])+allocation['host_reserve_cpu']
                or allocation['host_reserve_cpu']<allocation['controller_cpu']+2*allocation['guardian_cpu']):raise ValueError()
    if 'failure' in report and (not isinstance(report['failure'],str) or report['failure'] not in FAILURES):raise ValueError()
    if report['status']=='PASS':
        if (set(report)!=complete or phases!=list(PHASES) or report['cleanup_verified'] is not True
                or report['acceptance_eligible'] is not (not report['source_dirty'])):raise ValueError()
    elif report['acceptance_eligible'] is not False:raise ValueError()
    if len(json.dumps(report,sort_keys=True,allow_nan=False).encode())>64*1024:raise ValueError()


def validate_report(report,require_clean=True):
    """Return None or a fixed ValueError; dirty diagnostics need explicit opt-in."""
    try:_validate(report,require_clean)
    except Exception:raise ValueError('authenticated_runtime_evidence_refused') from None
