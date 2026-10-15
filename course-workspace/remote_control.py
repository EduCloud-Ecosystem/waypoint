#!/usr/bin/env python3
"""Trusted local operator for opt-in independent-worker component testing.

No worker provisioning, authority bootstrap, shell command, upload, learner
launch, quarantine reset or destructive restore operation is exposed.
"""
import argparse
from pathlib import Path

from authority_access import read_private
from authority_operator import OperatorDispatcher
from ownership_contract import Authority, OwnershipError, digest, encoded, HEX32, slug
from remote_backend import RemoteBackend
from worker_transport import WorkerClient

VERSION = 'educloud-independent-workers-v1'


def load_backend(config):
    expected = {'version', 'authority_id', 'authority_directory', 'plan_directory', 'workers'}
    if (not isinstance(config, dict) or set(config) != expected or config['version'] != VERSION
            or not isinstance(config['authority_id'], str) or not HEX32.fullmatch(config['authority_id'])
            or any(not isinstance(config[key], str) or not Path(config[key]).is_absolute()
                   for key in ('authority_directory', 'plan_directory'))
            or not isinstance(config['workers'], dict) or not 1 <= len(config['workers']) <= 16):
        raise OwnershipError('remote_configuration_invalid')
    workers, courses, clients = {}, {}, {}
    fields = {'host', 'port', 'server_sha256', 'ca_file', 'certfile', 'keyfile'}
    for worker, entry in config['workers'].items():
        if (not slug(worker) or not isinstance(entry, dict) or set(entry) != {'evidence', 'courses', 'channel'}
                or not isinstance(entry['channel'], dict) or set(entry['channel']) != fields):
            raise OwnershipError('remote_configuration_invalid')
        workers[worker], courses[worker] = entry['evidence'], entry['courses']
        clients[worker] = WorkerClient(authority_id=config['authority_id'], worker_id=worker, **entry['channel'])
    return RemoteBackend(config['authority_id'], workers, clients, courses)


def execute(config_path, operation, course, payload):
    config = read_private(config_path)
    backend = load_backend(config)
    authority = Authority.open(config['authority_directory'], config['authority_id'], backend)
    # The admin's private configuration is itself part of the reviewed boundary.
    # Recheck without network or nested authority SQL during operator writes.
    approved = digest(config)
    def authorize():
        if digest(read_private(config_path)) != approved:
            raise OwnershipError('remote_configuration_changed')
    if operation == 'inventory':
        authorize()
        result = authority.inventory()
        result['worker_observations_verified'] = all(
            backend.inspect_worker(worker) == backend.workers[worker] for worker in backend.workers)
        result['worker_admission_quarantine'] = {
            worker: {course: backend.quarantine(worker, course) for course in backend.courses[worker]}
            for worker in backend.workers}
        return result
    if not slug(course):
        raise OwnershipError('remote_course_required')
    workers = [worker for worker, values in backend.courses.items() if course in values]
    if not workers:
        raise OwnershipError('remote_endpoint_unknown')
    principal = {'role': 'operator', 'workers': workers, 'courses': [course],
                 'worker_id': workers[0], 'course_id': course, 'authorize': authorize}
    dispatcher = OperatorDispatcher(authority, config['plan_directory'])
    return dispatcher.dispatch(principal, operation, course, payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('inventory')
    for name in ('preview', 'reconcile-preview', 'apply', 'reconcile-apply'):
        command = commands.add_parser(name)
        command.add_argument('--course', required=True)
        if name == 'preview': command.add_argument('--receiver', required=True)
        elif name == 'reconcile-preview': command.add_argument('--selected-worker', required=True)
        else:
            command.add_argument('--plan-id', required=True)
            command.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    operation = {'inventory': 'inventory', 'preview': 'ownership.preview', 'apply': 'ownership.apply',
                 'reconcile-preview': 'ownership.reconcile_preview',
                 'reconcile-apply': 'ownership.reconcile_apply'}[args.command]
    payload = ({'receiver_worker_id': args.receiver} if args.command == 'preview' else
               {'selected_worker_id': args.selected_worker} if args.command == 'reconcile-preview' else
               {'plan_id': args.plan_id, 'plan_sha256': args.plan_sha256} if args.command != 'inventory' else {})
    try:
        print(encoded(execute(args.config, operation, getattr(args, 'course', None), payload)).decode())
    except Exception:
        raise SystemExit('remote_worker_operation_refused') from None


if __name__ == '__main__':
    main()
