"""Independent local Docker stopper with fresh authenticated ownership polling."""
import argparse
import logging
import queue
import threading
import time

from runtime_authority import DockerBackend, RuntimeRefusal, read_record
from hub.authority_client import AuthorityClient, AuthorityError, ERRORS

OBSERVATION_CODES = ERRORS | frozenset({'guardian_observation_invalid', 'guardian_unavailable',
    'guardian_stale_or_changed', 'guardian_container_unavailable', 'runtime_cached_epoch_invalid'})


class RuntimeGuardian:
    def __init__(self, backend, worker_id, clients, *, monotonic=time.monotonic):
        if not 1 <= len(clients) <= 16 or set(clients) != set(backend.config['courses'][worker_id]):
            raise RuntimeRefusal('guardian_course_scope_invalid')
        self.backend, self.worker, self.clients = backend, worker_id, clients
        self.epochs = {course: 0 for course in clients}
        self.pending_stops = {}
        self.monotonic, self.deferred_logs = monotonic, {}

    def stop_pending(self, course):
        identity, max_epoch, observed = self.pending_stops[course]
        try:
            self.backend.guardian_failclosed(self.worker, course, identity, max_epoch, observed)
        except RuntimeRefusal as error:
            if error.code == 'runtime_endpoint_lock_busy': return False
            raise
        del self.pending_stops[course]
        return True

    def deferred(self, course, phase):
        # Only fixed phase messages, bounded to one per phase/course/5 seconds.
        now = self.monotonic()
        key = (course, phase)
        if key not in self.deferred_logs or now - self.deferred_logs[key] >= 5:
            self.deferred_logs[key] = now
            message = {'snapshot': 'Guardian snapshot deferred: endpoint_busy',
                       'stop': 'Guardian stop deferred: endpoint_busy'}[phase]
            logging.getLogger(__name__).warning(message)

    def tick_course(self, course):
        if course not in self.clients:
            raise RuntimeRefusal('guardian_course_scope_invalid')
        client = self.clients[course]
        identity = self.backend.guardian_identity(self.worker)
        if course in self.pending_stops and not self.stop_pending(course):
            self.deferred(course, 'stop')
            return False  # Retain exact IDs, NO heartbeat while stopping.
        try:
            observed = self.backend.guardian_snapshot(self.worker, course)
        except RuntimeRefusal as error:
            if error.code == 'runtime_endpoint_lock_busy':
                self.deferred(course, 'snapshot')
                return False
            raise
        try:
            result = client.call('guardian.observe', identity)
            if (set(result) != {'authorized', 'epoch', 'worker_id', 'course_id'} or result['authorized'] is not True
                    or result['worker_id'] != self.worker or result['course_id'] != course
                    or type(result['epoch']) is not int or not 1 <= result['epoch'] <= 10**9):
                raise RuntimeRefusal('guardian_observation_invalid')
            self.epochs[course] = result['epoch']
        except Exception as error:
            code = getattr(error, 'code', None)
            if not isinstance(error, (AuthorityError, RuntimeRefusal)) or not isinstance(code, str) or code not in OBSERVATION_CODES:
                code = 'guardian_observation_failed'
            logging.getLogger(__name__).warning('Guardian observation refused: %s', code)
            # A later reviewed epoch is never stopped by this old failed view.
            # First successful service heartbeat is necessary for any spawn.
            max_epoch = max([self.epochs[course], *(epoch for _, epoch in observed)])
            self.pending_stops[course] = (identity, max_epoch, observed)
            if not self.stop_pending(course):
                self.deferred(course, 'stop')
                return False
        return True

    def tick(self):
        # Deterministic single-cycle API; production run isolates each course.
        for course in self.clients:
            self.tick_course(course)

    def run(self, stop_event=None, interval=1):
        if not 0.1 <= interval <= 2:
            raise RuntimeRefusal('guardian_interval_invalid')
        stop_event = stop_event or threading.Event()
        failures = queue.SimpleQueue()
        def poll(course):
            try:
                while not stop_event.is_set():
                    completed = self.tick_course(course)
                    stop_event.wait(interval if completed else 0.1)
            except BaseException as error:
                failures.put(error)
                stop_event.set()  # Any broken enforcer causes container exit.
        threads = [threading.Thread(target=poll, args=(course,), daemon=True,
                                    name='guardian-course-poll') for course in self.clients]
        failed = False
        try:
            for thread in threads: thread.start()
            while not stop_event.wait(0.1):
                pass
            if not failures.empty():
                failed = True
                raise failures.get()
        finally:
            stop_event.set()
            # RPCs/endpoint waits remain bounded; do not strand run on a wedged
            # course while another enforcer has failed. Daemon threads cannot
            # keep the failed Guardian process alive after main propagates.
            deadline = time.monotonic() + (0.5 if failed else 5)
            for thread in threads:
                if thread.ident is not None: thread.join(timeout=max(0, deadline - time.monotonic()))
            if not failed and not failures.empty():
                raise failures.get()
            if not failed and any(thread.is_alive() for thread in threads):
                raise RuntimeRefusal('guardian_shutdown_incomplete')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--worker', required=True)
    parser.add_argument('--client-config', required=True)
    args = parser.parse_args()
    config = read_record(args.config)
    backend = DockerBackend(config, inspect_roots=False)
    client_config = read_record(args.client_config)
    clients = {course: AuthorityClient(worker_id=args.worker, course_id=course, **client_config)
               for course in config['courses'][args.worker]}
    RuntimeGuardian(backend, args.worker, clients).run()


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        raise SystemExit('guardian_service_failed') from None
