#!/usr/bin/env python3
"""Seal a stopped institutional course for retention review without deleting work.

The prepared encrypted snapshot carries a sticky retirement tag even if its
preview is abandoned. Restoring that snapshot cannot reopen learner access.
"""
import argparse
from datetime import date, datetime, timedelta, timezone
from functools import wraps
import json
from pathlib import Path
import re
import sys
import subprocess

import upgrade as u
from maintenance import write, sync_directory, home_lock, digest
from pilot import locked
from deployment import preflight, require_no_resource_amendment
from recovery import backup_repository_locked, restic, TAG, RETIRED_TAG

VERSION = 'educloud-retirement-v1'
PENDING = '.retirement-pending.json'
MARKER = '.course-retired.json'
RECEIPT = '.retirement-receipt.json'
MAX_JSON = 64 * 1024
BINDINGS = {'directory_sha256', 'env_sha256', 'policy_sha256', 'files_sha256', 'source_manifest_sha256',
            'recovery_config_sha256', 'recovery_config_path_sha256', 'backup_password_sha256', 'backup_password_path_sha256'}
PLAN_FIELDS = {'version', 'tool_sha256', 'bindings', 'images', 'counts', 'snapshot_id', 'retention_owner', 'review_after', 'digest'}
LEDGER_FIELDS = {'version', 'status', 'source', 'retention', 'retention_owner', 'review_after', 'plan_digest',
                 'snapshot_id', 'images', 'counts', 'bindings', 'digest'}
RESTORED_FIELDS = {'version', 'status', 'source', 'snapshot_id', 'retention'}


class RetirementError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def guarded(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RetirementError:
            raise
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, OverflowError, UnicodeError,
                RecursionError, subprocess.SubprocessError):
            raise RetirementError('retirement_refused_or_incomplete') from None
    return run


def tool_hash():
    here = Path(__file__).resolve().parent
    return digest({'upgrade_tool_sha256': u.tool_hash(), 'retirement_sha256': u.sha((here / 'retirement.py').read_bytes())})


def environment(directory):
    """Read retained configuration for retirement resume/export, never activation."""
    u.safe_directory(directory)
    require_no_resource_amendment(directory)
    if (directory / 'pilot.env').exists() or (directory / 'pilot.env').is_symlink():
        raise RetirementError('institutional_course_required')
    raw = u._read_regular(directory / '.env', u.MAX_ENV_BYTES, private=True).decode('utf-8')
    env = {}
    for line in raw.splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if (not separator or not re.fullmatch(r'WORKSPACE_[A-Z_]+', key) or key in env
                or any(char.isspace() for char in value) or any(char in value for char in ('$', '"', "'", '#', '\\', '\x00'))):
            raise RetirementError('retained_environment_invalid')
        env[key] = value
    u.validate_institutional(env)
    if env.get('WORKSPACE_ENV_FILE') != str(directory / '.env'):
        raise RetirementError('retained_environment_binding_invalid')
    return env


def owner_date(owner, review_after, require_future=False):
    if not isinstance(owner, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,127}', owner) or not owner.strip():
        raise RetirementError('retention_owner_required')
    if not isinstance(review_after, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', review_after):
        raise RetirementError('retention_review_date_required')
    value = date.fromisoformat(review_after)
    if require_future:
        today = datetime.now(timezone.utc).date()
        if not today <= value <= today + timedelta(days=3650):
            raise RetirementError('retention_review_date_outside_window')


def common_fields(record):
    if (not isinstance(record['snapshot_id'], str) or not u.HASH.fullmatch(record['snapshot_id'])
            or not isinstance(record['counts'], dict) or set(record['counts']) != {'retained_homes', 'file_entries'}
            or any(type(value) is not int or not 1 <= value <= u.MAX_ENTRIES for value in record['counts'].values())
            or not isinstance(record['bindings'], dict) or set(record['bindings']) != BINDINGS
            or any(not isinstance(value, str) or not u.HASH.fullmatch(value) for value in record['bindings'].values())
            or not isinstance(record['images'], dict) or set(record['images']) != {'hub', 'course'}
            or any(not isinstance(value, str) or not u.IMAGE.fullmatch(value) for value in record['images'].values())):
        raise RetirementError('retirement_record_schema_invalid')
    owner_date(record['retention_owner'], record['review_after'])


def load_plan(path):
    record, _ = u.read_json(path)
    if (set(record) != PLAN_FIELDS or record.get('version') != VERSION
            or not isinstance(record.get('digest'), str)
            or record['digest'] != digest({key: value for key, value in record.items() if key != 'digest'})
            or not isinstance(record['tool_sha256'], str) or not u.HASH.fullmatch(record['tool_sha256'])):
        raise RetirementError('retirement_plan_digest_invalid')
    common_fields(record)
    return record


@guarded
def read_retirement(directory):
    directory = Path(directory)
    u.safe_directory(directory)
    record, _ = u.read_json(directory / MARKER)
    if (record.get('version') != VERSION or record.get('status') != 'RETIRED' or record.get('retention') != 'pending'):
        raise RetirementError('retirement_record_invalid')
    if record.get('source') == 'snapshot_restore':
        if set(record) != RESTORED_FIELDS or not isinstance(record['snapshot_id'], str) or not u.HASH.fullmatch(record['snapshot_id']):
            raise RetirementError('retirement_record_invalid')
        return record
    if (set(record) != LEDGER_FIELDS or record.get('source') != 'operator_retirement'
            or not isinstance(record.get('digest'), str)
            or record['digest'] != digest({key: value for key, value in record.items() if key != 'digest'})
            or not isinstance(record['plan_digest'], str) or not u.HASH.fullmatch(record['plan_digest'])):
        raise RetirementError('retirement_record_invalid')
    common_fields(record)
    return record


def no_conflicts(directory, root, allow_retirement=False):
    require_no_resource_amendment(directory)
    paths = [directory / u.PENDING, directory / u.MAINTENANCE_PENDING, root / u.ROOT_PENDING]
    if not allow_retirement:
        paths.extend((directory / PENDING, directory / MARKER))
    if any(path.exists() or path.is_symlink() for path in paths):
        raise RetirementError('course_has_unfinished_or_retired_state')


def backup_proof(cfg, snapshot_id, instance):
    raw = restic(cfg, 'snapshots', '--json', snapshot_id)
    if not isinstance(raw, str) or len(raw.encode()) > MAX_JSON:
        raise RetirementError('retirement_backup_proof_invalid')
    rows = json.loads(raw)
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get('id') != snapshot_id or rows[0].get('hostname') != instance
            or not isinstance(rows[0].get('tags'), list)
            or not {TAG, RETIRED_TAG, 'instance=' + instance} <= set(rows[0]['tags'])):
        raise RetirementError('retirement_backup_proof_invalid')


def expected_state(plan):
    return {key: plan['bindings'][key] for key in ('env_sha256', 'policy_sha256', 'files_sha256', 'source_manifest_sha256')}


def ledger_for(plan):
    record = {'version': VERSION, 'status': 'RETIRED', 'source': 'operator_retirement', 'retention': 'pending',
              'retention_owner': plan['retention_owner'], 'review_after': plan['review_after'],
              'plan_digest': plan['digest'], 'snapshot_id': plan['snapshot_id'], 'images': plan['images'],
              'counts': plan['counts'], 'bindings': plan['bindings']}
    record['digest'] = digest(record)
    return record


def summary(record, status):
    return {'version': VERSION, 'status': status, 'snapshot_id': record['snapshot_id'],
            'counts': record['counts'], 'plan_digest': record.get('plan_digest', record.get('digest')),
            'scope': 'stopped-course-retention', 'deployment_started': False, 'files_deleted': False}


@guarded
def preview(directory, recovery_config_path, plan_path, retention_owner, review_after):
    directory, plan_path = Path(directory), Path(plan_path)
    u.safe_directory(directory)
    u.plan_destination(plan_path)
    owner_date(retention_owner, review_after, require_future=True)
    with locked(directory):
        env = environment(directory)
        root = Path(env['WORKSPACE_HOME_ROOT'])
        with home_lock(root):
            no_conflicts(directory, root)
            images = preflight(directory)['images']
            cfg, recovery_bindings = u.recovery_inputs(recovery_config_path)
            protected = [root, u.hub_data(env, images)]
            if cfg['repository'].startswith('/'):
                protected.append(Path(cfg['repository']))
            u.plan_destination(plan_path, protected)
            before, counts = u.state(directory, env, images)
            snapshot_id = backup_repository_locked(directory, cfg, quiet=True, extra_tags=(RETIRED_TAG,))
            if not isinstance(snapshot_id, str) or not u.HASH.fullmatch(snapshot_id):
                raise RetirementError('retirement_backup_missing')
            after, after_counts = u.state(directory, environment(directory), images)
            _, frozen_recovery = u.recovery_inputs(recovery_config_path)
            if before != after or counts != after_counts or frozen_recovery != recovery_bindings:
                raise RetirementError('course_changed_during_retirement_backup')
            backup_proof(cfg, snapshot_id, env['WORKSPACE_INSTANCE'])
            record = {'version': VERSION, 'tool_sha256': tool_hash(),
                      'bindings': {'directory_sha256': u.path_hash(directory), **after, **recovery_bindings},
                      'images': images, 'counts': counts, 'snapshot_id': snapshot_id,
                      'retention_owner': retention_owner, 'review_after': review_after}
            record['digest'] = digest(record)
            write(plan_path, record, exclusive=True)
            return record


@guarded
def apply(directory, plan_path, recovery_config_path):
    directory, plan_path = Path(directory), Path(plan_path)
    u.safe_directory(directory)
    plan = load_plan(plan_path)
    if plan['tool_sha256'] != tool_hash() or plan['bindings']['directory_sha256'] != u.path_hash(directory):
        raise RetirementError('retirement_plan_tool_or_deployment_changed')
    with locked(directory):
        env = environment(directory)
        root = Path(env['WORKSPACE_HOME_ROOT'])
        pending, marker = directory / PENDING, directory / MARKER
        with home_lock(root):
            no_conflicts(directory, root, allow_retirement=True)
            cfg, recovery_bindings = u.recovery_inputs(recovery_config_path)
            if any(plan['bindings'][key] != value for key, value in recovery_bindings.items()):
                raise RetirementError('retirement_backup_configuration_changed')
            backup_proof(cfg, plan['snapshot_id'], env['WORKSPACE_INSTANCE'])
            arch = u.worker_architecture()
            if any(u.candidate_image(value, arch) != value for value in plan['images'].values()):
                raise RetirementError('retirement_runtime_changed')
            observed, counts = u.state(directory, env, plan['images'])
            if observed != expected_state(plan) or counts != plan['counts']:
                raise RetirementError('retirement_plan_stale')
            expected_ledger = ledger_for(plan)
            if marker.exists() or marker.is_symlink():
                if read_retirement(directory) != expected_ledger:
                    raise RetirementError('course_already_retired_with_different_plan')
            if pending.exists() or pending.is_symlink():
                journal, _ = u.read_json(pending)
                if set(journal) != {'version', 'plan', 'ledger'} or journal.get('version') != VERSION or journal.get('plan') != plan or journal.get('ledger') != expected_ledger:
                    raise RetirementError('retirement_resume_requires_exact_plan')
            elif not (marker.exists() or marker.is_symlink()):
                # Public preflight must still prove active and stopped immediately
                # before creating the journal. Resume uses frozen state above.
                if preflight(directory)['images'] != plan['images']:
                    raise RetirementError('retirement_runtime_changed')
                journal = {'version': VERSION, 'plan': plan, 'ledger': expected_ledger}
                if len((json.dumps(journal, indent=2, sort_keys=True) + '\n').encode()) > MAX_JSON:
                    raise RetirementError('retirement_journal_budget_exceeded')
                write(pending, journal, exclusive=True)
            if not (marker.exists() or marker.is_symlink()):
                write(marker, expected_ledger, exclusive=True)
            if read_retirement(directory) != expected_ledger:
                raise RetirementError('retirement_seal_verification_failed')
            observed, counts = u.state(directory, environment(directory), plan['images'])
            if observed != expected_state(plan) or counts != plan['counts']:
                raise RetirementError('course_changed_during_retirement_apply')
            receipt = summary(expected_ledger, 'RETIRED')
            write(directory / RECEIPT, receipt)
            if pending.exists() or pending.is_symlink():
                pending.unlink()
                sync_directory(directory)
            return receipt


resume = apply


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('preview', 'apply', 'resume', 'export', 'verify-export'))
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--recovery-config', type=Path)
    parser.add_argument('--retention-owner')
    parser.add_argument('--review-after')
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'verify-export':
            if args.destination is None:
                raise RetirementError('export_destination_required')
            from retirement_export import verify_export
            result = verify_export(args.destination)
        elif args.operation == 'export':
            if args.directory is None or args.destination is None:
                raise RetirementError('retired_course_and_export_destination_required')
            from retirement_export import export_course
            result = export_course(args.directory, args.destination)
        else:
            if args.directory is None or args.plan is None or args.recovery_config is None:
                raise RetirementError('retirement_plan_inputs_required')
            if args.operation == 'preview':
                record = preview(args.directory, args.recovery_config, args.plan, args.retention_owner, args.review_after)
                result = summary(record, 'PREVIEWED')
            elif args.retention_owner is not None or args.review_after is not None:
                raise RetirementError('retirement_apply_uses_reviewed_plan')
            else:
                result = apply(args.directory, args.plan, args.recovery_config)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (RetirementError, u.UpgradeError, OSError, ValueError, KeyError, TypeError, RuntimeError, ImportError, subprocess.SubprocessError):
        print(json.dumps({'version': VERSION, 'status': 'REFUSED_OR_INCOMPLETE', 'deployment_started': False,
                          'files_deleted': False, 'code': 'retirement_refused_or_incomplete',
                          'instruction': 'Keep the course stopped and retained. Resume the exact pending retirement or inspect the private retention record.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
