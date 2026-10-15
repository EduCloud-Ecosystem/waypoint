"""Review managed credentials against configured, freshly observed remote workers."""
import argparse
from pathlib import Path

from authority_access import AccessPolicy, publish, read_private
from ownership_contract import digest, encoded, HEX64
from remote_runtime_service import verify_binding, refuse
from worker_credentials import _certificate_sources, verify_certificate
from worker_enrollment import EnrollmentStore

REVIEW_VERSION = 'educloud-remote-credential-review-v1'


def execute(args):
    config = read_private(args.config)
    service, control = config['service'], config['control']
    approved_config = digest(config)
    def fresh_binding(observe=False):
        current = read_private(args.config)
        if digest(current) != approved_config:
            refuse()
        return verify_binding(current, observe=observe)
    binding = fresh_binding(observe=args.command == 'create')
    directory = service['enrollment_directory']
    authority_id = control['authority_id']
    if args.command == 'create':
        EnrollmentStore.create(directory, authority_id, binding)
        return {'status': 'CREATED', 'authority_id': authority_id}
    store = EnrollmentStore.open(directory, authority_id, binding)
    if args.command == 'status':
        return {'status': 'INSPECTED', 'configured_workers': len(binding['workers']), 'live_credentials': len(store.entries())}
    if args.command == 'preview':
        if args.action in ('enroll', 'stage', 'activate'):
            if fresh_binding(observe=True) != binding:
                refuse()
        if args.worker not in binding['workers']:
            refuse()
        slots = {'guardian'} | {'hub:' + course for course in binding['workers'][args.worker]['courses']}
        sources, metadata = {}, None
        if args.action in ('enroll', 'stage', 'activate'):
            expected = slots if args.action == 'enroll' else {args.slot}
            if args.credentials is None or None in expected:
                refuse()
            sources, actual = _certificate_sources(args.credentials, expected, control, service)
            metadata = actual if args.action == 'enroll' else actual[args.slot] if args.action == 'stage' else None
        elif args.credentials is not None:
            refuse()
        summary = store.preview(args.action, args.worker, credentials=metadata, slot=args.slot,
            target_fingerprint=args.target_fingerprint, overlap_seconds=args.overlap_seconds)
        approved = store.certificates_for_plan(summary['plan_id'], summary['plan_sha256'])
        if sources and actual != approved:
            refuse()
        record = {'version': REVIEW_VERSION, 'authority_id': authority_id,
            'plan_id': summary['plan_id'], 'plan_sha256': summary['plan_sha256'], 'sources': sources}
        publish(Path(args.output), record)
        return dict(summary, review_sha256=digest(record))
    if args.command != 'apply':
        refuse()
    record = read_private(args.plan_file)
    if (not isinstance(args.review_sha256, str) or not HEX64.fullmatch(args.review_sha256)
            or not isinstance(record, dict) or set(record) != {'version', 'authority_id', 'plan_id', 'plan_sha256', 'sources'}
            or record['version'] != REVIEW_VERSION or record['authority_id'] != authority_id
            or digest(record) != args.review_sha256):
        refuse()
    approved = store.certificates_for_plan(record['plan_id'], record['plan_sha256'])
    if not isinstance(record['sources'], dict) or set(record['sources']) != set(approved):
        refuse()
    if approved:
        static = AccessPolicy(service['acl_file'], authority_id)._read()
        operators = {fp for fp, entry in static.items() if entry['role'] == 'operator'}
        actual = {slot: verify_certificate(path, service['ca_file']) for slot, path in record['sources'].items()}
        if actual != approved or any(cert['fingerprint'] in operators for cert in actual.values()):
            refuse()
    plan = store.plan(record['plan_id'], record['plan_sha256'])
    return store.apply(record['plan_id'], record['plan_sha256'],
        lambda: fresh_binding(observe=plan['action'] in ('enroll', 'stage', 'activate')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('create'); commands.add_parser('status')
    preview = commands.add_parser('preview')
    preview.add_argument('--action', choices=('enroll', 'stage', 'activate', 'retire', 'revoke', 'revoke_worker'), required=True)
    preview.add_argument('--worker', required=True); preview.add_argument('--credentials')
    preview.add_argument('--slot'); preview.add_argument('--target-fingerprint'); preview.add_argument('--overlap-seconds', type=int)
    preview.add_argument('--output', required=True)
    apply = commands.add_parser('apply')
    apply.add_argument('--plan-file', required=True); apply.add_argument('--review-sha256', required=True)
    try:
        print(encoded(execute(parser.parse_args())).decode())
    except Exception:
        raise SystemExit('remote_credentials_failed') from None


if __name__ == '__main__':
    main()
