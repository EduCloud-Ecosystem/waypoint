"""Strict bounded report for actual local enrollment/rotation; not hosted proof."""
from copy import deepcopy
import authenticated_evidence as base

VERSION = 'waypoint-worker-enrollment-rehearsal-v1'
SCOPE = base.SCOPE
FAILURES = base.FAILURES
CODE_FILES = base.CODE_FILES | {'worker_enrollment.py', 'worker_credentials.py',
    'rehearsal/enrollment_fixture.py', 'rehearsal/verify_worker_enrollment.py',
    'rehearsal/worker_enrollment_evidence.py'}
PHASES = (
    'unenrolled_legacy_acl_and_unknown_worker_refused',
    'reviewed_worker_enrollment_digest_and_replay',
    'enrolled_python_r_saved_home_and_socketless_hubs',
    'staged_fresh_key_rotation_inert_after_restart',
    'bounded_overlap_old_new_mutual_tls',
    'retired_hub_denied_new_hub_coursework_continues',
    'guardian_overlap_expiry_stops_own_worker_preserves_peer',
    'rotated_guardian_recovers_saved_coursework',
    'worker_revocation_stops_own_worker_and_blocks_reenrollment',
    'service_restart_preserves_enrollment_and_ownership',
)


def validate_report(report, require_clean=True):
    try:
        if (not isinstance(report, dict) or report.get('version') != VERSION
                or not isinstance(report.get('phases'), list)
                or report['phases'] != list(PHASES[:len(report['phases'])])):
            raise ValueError()
        if report.get('status') == 'PASS' and report['phases'] != list(PHASES):
            raise ValueError()
        if 'code_sha256' in report and not base._hash_map(report['code_sha256'], CODE_FILES, base.HASH):
            raise ValueError()
        # Reuse the exact capacity, allocation, privacy and provenance checks.
        # This normalized value is internal schema validation, never evidence.
        normalized = deepcopy(report)
        normalized['version'] = base.VERSION
        normalized['phases'] = list(base.PHASES) if report.get('status') == 'PASS' else []
        if 'code_sha256' in normalized:
            normalized['code_sha256'] = {path: normalized['code_sha256'][path] for path in base.CODE_FILES}
        base.validate_report(normalized, require_clean=require_clean)
    except Exception:
        raise ValueError('worker_enrollment_evidence_refused') from None
