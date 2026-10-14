"""Explicit local authority service; startup never bootstraps or repairs state."""
import argparse
import threading

from authority_access import AccessPolicy, AuthorityServer, ReplayStore
from ownership_contract import Authority
from runtime_authority import DockerBackend, RuntimeDispatcher, read_record
from authority_operator import OperatorDispatcher


def serve(config, service):
    backend = DockerBackend(config)
    authority = Authority.open(service['authority_directory'], config['authority_id'], backend)
    enrollment = None
    binding_verifier = None
    if 'enrollment_directory' in service:
        # Explicit configured mode; missing/corrupt managed state never falls
        # back to legacy runtime entries or creates a replacement registry.
        from worker_credentials import verify_binding, make_runtime_binding_verifier
        from worker_enrollment import EnrollmentStore
        directory = service['enrollment_directory']
        if not isinstance(directory, str) or not directory: raise ValueError('managed_enrollment_required')
        binding = verify_binding(config, service, backend=backend, authority=authority)
        enrollment = EnrollmentStore.open(directory, config['authority_id'], binding)
        binding_verifier = make_runtime_binding_verifier(config, service, backend, enrollment.binding)
    policy = AccessPolicy(service['acl_file'], config['authority_id'], enrollment=enrollment, binding_verifier=binding_verifier)
    runtime = RuntimeDispatcher(authority, backend,
        lambda fingerprint, worker, course: policy.authorize_fingerprint(fingerprint, 'guardian', worker, course))
    operator = OperatorDispatcher(authority, service['plan_directory'])
    def dispatch(principal, operation, course, payload):
        target = operator if operation.startswith('ownership.') else runtime
        return target.dispatch(principal, operation, course, payload)
    server = AuthorityServer((service['bind'], service['port']),
        authority_id=config['authority_id'], certfile=service['certfile'], keyfile=service['keyfile'],
        ca_file=service['ca_file'], acl_file=service['acl_file'],
        replay_store=ReplayStore.open(service['replay_directory'], config['authority_id']), dispatch=dispatch, access_policy=policy)
    stopped = threading.Event()
    failed = []
    def supervise():
        while not stopped.is_set():
            try:
                runtime.supervision_tick()
            except Exception as exc:
                failed.append(exc)
                server.shutdown()
                return
            stopped.wait(0.5)
    monitor = threading.Thread(target=supervise, daemon=True)
    try:
        monitor.start()
        server.serve_forever(poll_interval=0.1)
    finally:
        stopped.set()
        server.server_close()
        if monitor.ident is not None:
            monitor.join(timeout=10)
    if failed:
        raise RuntimeError('authority_supervision_failed') from failed[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--service-config', required=True)
    args = parser.parse_args()
    serve(read_record(args.config), read_record(args.service_config))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('authority_service_failed') from None
