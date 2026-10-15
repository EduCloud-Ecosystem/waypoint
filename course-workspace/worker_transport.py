"""Bounded persistent mTLS worker channels; no retries or reusable grants.

The authenticated controller owns ownership decisions. Exclusive fence effects
quarantine the worker until an explicit post-COMPLETE finish, never on disconnect.
"""
from contextlib import contextmanager, ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import socketserver
import ssl
import struct
import stat
import sys
import threading
import time
import uuid

from worker_channel_state import WorkerJournal, JournalError, private, parsed, encoded, digest
from hub.authority_client import material
from runtime_authority import DockerBackend

VERSION = 'educloud-worker-channel-v1'
MAX_FRAME = 65536
MAX_CALLS = 128
HEX = re.compile(r'[0-9a-f]{32}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')
SLUG = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
ACTIVATE = {'authority_id', 'course_id', 'worker_id', 'provider_id', 'boot_id', 'epoch'}
TRANSFER = {'authority_id', 'transaction_id', 'course_id', 'source_worker_id', 'receiver_worker_id',
            'source_provider_id', 'source_boot_id', 'receiver_provider_id', 'receiver_boot_id', 'old_epoch', 'plan_sha256'}
RECOVERY = {'selected_worker_id', 'original_transaction_id'}
ERRORS = frozenset({'worker_channel_refused', 'worker_frame_invalid', 'worker_request_invalid',
    'worker_identity_changed', 'worker_quarantined', 'worker_session_expired', 'worker_session_finished',
    'worker_operation_refused', 'worker_fence_unverified', 'worker_connection_closed', 'worker_session_closed',
    'worker_transport_unavailable', 'worker_server_pin_changed', 'worker_peer_validity_refused',
    'worker_host_identity_refused', 'worker_shutdown_incomplete'})


class WorkerError(ValueError):
    def __init__(self, code):
        self.code = code if code in ERRORS else 'worker_channel_refused'; super().__init__(self.code)


def refuse(code='worker_channel_refused'): raise WorkerError(code)
def slug(value): return isinstance(value, str) and SLUG.fullmatch(value)
def number(value, low, high): return type(value) in (int, float) and low <= value <= high


def tls_context(server, certfile, keyfile, ca_file):
    try:
        checked = []
        for path, secret in ((certfile, False), (keyfile, True), (ca_file, False)):
            value = material(path, secret)
            info = Path(value).lstat()
            if info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022: refuse()
            checked.append(value)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2; context.verify_mode = ssl.CERT_REQUIRED
        if not server: context.check_hostname = True
        context.load_verify_locations(cafile=checked[2]); context.load_cert_chain(checked[0], checked[1])
        return context
    except WorkerError: raise
    except Exception: refuse()


def linux_boot_id():
    """Actual local Linux incarnation only; no configured path or OS fallback."""
    fd = None
    try:
        if sys.platform != 'linux': refuse('worker_host_identity_refused')
        fd = os.open('/proc/sys/kernel/random/boot_id', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode): refuse('worker_host_identity_refused')
        raw = bytearray()
        while len(raw) <= 64:
            chunk = os.read(fd, 65 - len(raw))
            if not chunk: break
            raw.extend(chunk)
        value = bytes(raw).decode('ascii')
        if value.endswith('\n'): value = value[:-1]
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value):
            refuse('worker_host_identity_refused')
        return uuid.UUID(value).hex
    except WorkerError: raise
    except Exception: refuse('worker_host_identity_refused')
    finally:
        if fd is not None: os.close(fd)


class LinuxWorkerBackend(DockerBackend):
    def inspect_worker(self, worker):
        evidence = super().inspect_worker(worker)
        if linux_boot_id() != evidence['boot_id']: refuse('worker_host_identity_refused')
        return evidence


def journal_binding(backend, worker):
    return digest({'config': backend.config, 'home_bindings': backend.home_bindings,
                   'worker': backend.inspect_worker(worker)})


class _PeerValidity:
    def __init__(self, certificate):
        self.before = ssl.cert_time_to_seconds(certificate['notBefore'])
        self.after = ssl.cert_time_to_seconds(certificate['notAfter'])
        self.floor = None; self.check()

    def check(self):
        now = time.time()
        if (type(now) not in (int, float) or not math.isfinite(now)
                or not self.before <= now < self.after or self.floor is not None and now < self.floor):
            refuse('worker_peer_validity_refused')
        self.floor = now


def receive(stream, deadline, timeout):
    def take(count):
        value = bytearray()
        while len(value) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0: refuse('worker_session_expired')
            stream.settimeout(min(timeout, remaining))
            chunk = stream.recv(count - len(value))
            if not chunk: refuse('worker_connection_closed')
            value.extend(chunk)
        return bytes(value)
    size = struct.unpack('!I', take(4))[0]
    if not 1 <= size <= MAX_FRAME: refuse('worker_frame_invalid')
    try: value = parsed(take(size))
    except Exception: refuse('worker_frame_invalid')
    if not isinstance(value, dict): refuse('worker_frame_invalid')
    return value


def send(stream, value):
    try: raw = encoded(value)
    except Exception: refuse('worker_frame_invalid')
    if not 1 <= len(raw) <= MAX_FRAME: refuse('worker_frame_invalid')
    stream.sendall(struct.pack('!I', len(raw)) + raw)


def request_record(request, authority, worker, course, mode):
    if not isinstance(request, dict): refuse('worker_request_invalid')
    if mode == 'activate':
        if (set(request) != ACTIVATE or request.get('worker_id') != worker
                or type(request.get('epoch')) is not int or not 1 <= request['epoch'] <= 10**9
                or not isinstance(request.get('provider_id'), str) or not 1 <= len(request['provider_id']) <= 256
                or not isinstance(request.get('boot_id'), str) or not HEX.fullmatch(request['boot_id'])):
            refuse('worker_request_invalid')
    else:
        if (set(request) not in (TRANSFER, TRANSFER | RECOVERY)
                or any(not slug(request.get(key)) for key in ('source_worker_id', 'receiver_worker_id'))
                or request['source_worker_id'] == request['receiver_worker_id']
                or worker not in (request['source_worker_id'], request['receiver_worker_id'])
                or type(request.get('old_epoch')) is not int or not 1 <= request['old_epoch'] <= 10**9
                or not isinstance(request.get('transaction_id'), str) or not HEX.fullmatch(request['transaction_id'])
                or not isinstance(request.get('plan_sha256'), str) or not HASH.fullmatch(request['plan_sha256'])
                or any(not isinstance(request.get(prefix + '_boot_id'), str) or not HEX.fullmatch(request[prefix + '_boot_id'])
                       or not isinstance(request.get(prefix + '_provider_id'), str) or not 1 <= len(request[prefix + '_provider_id']) <= 256
                       for prefix in ('source', 'receiver'))): refuse('worker_request_invalid')
        if RECOVERY <= set(request) and (request['selected_worker_id'] not in (request['source_worker_id'], request['receiver_worker_id'])
                or not isinstance(request['original_transaction_id'], str) or not HEX.fullmatch(request['original_transaction_id'])):
            refuse('worker_request_invalid')
    if request.get('authority_id') != authority or request.get('course_id') != course: refuse('worker_request_invalid')


class WorkerServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    allow_reuse_port = False
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, backend, worker_id, certfile, keyfile, ca_file, acl_file, *, journal,
                 runtime_dispatch=None, timeout=3, session_seconds=20, max_threads=8):
        if (not slug(worker_id) or not number(timeout, .05, 10) or not number(session_seconds, .1, 60)
                or type(max_threads) is not int or not 1 <= max_threads <= 32
                or not isinstance(journal, WorkerJournal) or journal.worker_id != worker_id
                or getattr(backend, 'authority_id', None) != journal.authority_id
                or runtime_dispatch is not None and not callable(runtime_dispatch)): refuse()
        self.backend, self.worker_id, self.journal = backend, worker_id, journal
        self.authority_id, self.acl_file = journal.authority_id, Path(acl_file)
        self.runtime_dispatch, self.timeout, self.session_seconds = runtime_dispatch, timeout, session_seconds
        self._worker = json.loads(encoded(backend.inspect_worker(worker_id)))
        if self._worker.get('worker_id') != worker_id: refuse('worker_identity_changed')
        self._acl()
        self.tls = tls_context(True, certfile, keyfile, ca_file)
        self._slots = threading.BoundedSemaphore(max_threads); self._connections = set(); self._guard = threading.Lock()
        self._closing = threading.Event(); self._handler_threads = set()
        super().__init__(address, _Handler)

    def _acl(self):
        try:
            info = private(self.acl_file)
            if info.st_size > MAX_FRAME: refuse()
            fd = os.open(self.acl_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                actual = os.fstat(source.fileno())
                if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino): refuse()
                value = parsed(source.read(MAX_FRAME + 1))
            if (not isinstance(value, dict) or set(value) != {'version', 'authority_id', 'worker_id', 'controllers'}
                    or value['version'] != VERSION or value['authority_id'] != self.authority_id
                    or value['worker_id'] != self.worker_id or not isinstance(value['controllers'], dict)
                    or len(value['controllers']) > 128): refuse()
            for fingerprint, courses in value['controllers'].items():
                if (not isinstance(fingerprint, str) or not HASH.fullmatch(fingerprint)
                        or not isinstance(courses, list) or not 1 <= len(courses) <= 256
                        or any(not slug(course) or course not in self.journal.courses for course in courses)
                        or len(set(courses)) != len(courses)): refuse()
            return value['controllers']
        except WorkerError: raise
        except Exception: refuse()

    def process_request(self, request, client_address):
        if not self._slots.acquire(False): self.shutdown_request(request); return
        with self._guard:
            if self._closing.is_set():
                self._slots.release(); self.shutdown_request(request); return
            thread = threading.Thread(target=self.process_request_thread, args=(request, client_address), daemon=True)
            self._connections.add(request); self._handler_threads.add(thread)
            try: thread.start()
            except BaseException:
                self._connections.discard(request); self._handler_threads.discard(thread)
                self._slots.release(); self.shutdown_request(request); raise

    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally:
            with self._guard:
                self._connections.discard(request); self._handler_threads.discard(threading.current_thread())
            self._slots.release()

    def handle_error(self, request, client_address): pass  # Never print frames or credential-bearing traceback.

    def server_close(self):
        self._closing.set()
        with self._guard: connections = list(self._connections)
        for stream in connections:
            try: stream.shutdown(socket.SHUT_RDWR)
            except OSError: pass
        super().server_close()
        deadline = time.monotonic() + min(self.timeout + 6, 16)
        while True:
            with self._guard: handlers = [thread for thread in self._handler_threads if thread.is_alive()]
            if not handlers: return
            if threading.current_thread() in handlers: refuse('worker_shutdown_incomplete')
            for thread in handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0: refuse('worker_shutdown_incomplete')
                thread.join(min(remaining, .05))


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server; stream = None; sequence = 0; session = uuid.uuid4().hex; challenge = uuid.uuid4().hex + uuid.uuid4().hex
        deadline = time.monotonic() + server.session_seconds
        def response(ok, value):
            return {'version': VERSION, 'session_id': session, 'challenge': challenge, 'sequence': sequence,
                    'ok': ok, 'result' if ok else 'error': value}
        try:
            if server._closing.is_set(): refuse('worker_session_closed')
            self.request.settimeout(server.timeout)
            self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream = server.tls.wrap_socket(self.request, server_side=True)
            with server._guard:
                server._connections.discard(self.request); server._connections.add(stream)
            fingerprint = hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest()
            validity = _PeerValidity(stream.getpeercert())
            if fingerprint not in server._acl(): refuse()
            send(stream, {'version': VERSION, 'authority_id': server.authority_id, 'worker_id': server.worker_id,
                          'session_id': session, 'challenge': challenge})
            opening = receive(stream, deadline, server.timeout)
            if (set(opening) != {'version', 'authority_id', 'worker_id', 'course_id', 'session_id', 'challenge', 'mode', 'request'}
                    or opening['version'] != VERSION or opening['authority_id'] != server.authority_id
                    or opening['worker_id'] != server.worker_id or opening['session_id'] != session or opening['challenge'] != challenge
                    or not slug(opening['course_id']) or opening['mode'] not in ('inspect', 'activate', 'exclusive')):
                refuse('worker_frame_invalid')
            course, mode, request = opening['course_id'], opening['mode'], opening['request']
            if mode == 'inspect':
                if request is not None: refuse('worker_request_invalid')
            else: request_record(request, server.authority_id, server.worker_id, course, mode)
            def authorize():
                if server._closing.is_set(): refuse('worker_session_closed')
                if time.monotonic() >= deadline: refuse('worker_session_expired')
                validity.check()
                if course not in server._acl().get(fingerprint, []): refuse()
                observed_worker = server.backend.inspect_worker(server.worker_id)
                try: identical = encoded(observed_worker) == encoded(server._worker)
                except Exception: identical = False
                if not identical: refuse('worker_identity_changed')
                quarantined = server.journal.state(course)['quarantined']
                if mode == 'activate' and quarantined: refuse('worker_quarantined')
                validity.check()  # Fresh deadline after potentially slow observations, immediately before effects.
                if server._closing.is_set(): refuse('worker_session_closed')
                if time.monotonic() >= deadline: refuse('worker_session_expired')
            authorize()
            fenced = False; finished = False
            with ExitStack() as stack:
                if mode == 'activate': stack.enter_context(server.backend.activation_context(request))
                elif mode == 'exclusive':
                    stack.enter_context(server.backend.locks([(server.worker_id, course)]))
                    server.backend.validate_request(request, [server.worker_id])
                authorize()
                send(stream, response(True, {'opened': True}))
                for expected in range(1, MAX_CALLS + 1):
                    frame = receive(stream, deadline, server.timeout)
                    if (set(frame) != {'version', 'session_id', 'challenge', 'sequence', 'operation', 'payload'}
                            or frame['version'] != VERSION or frame['session_id'] != session or frame['challenge'] != challenge
                            or type(frame['sequence']) is not int or frame['sequence'] != expected
                            or not isinstance(frame['operation'], str) or not isinstance(frame['payload'], dict)):
                        refuse('worker_frame_invalid')
                    sequence = expected; op, payload = frame['operation'], frame['payload']
                    authorize()
                    if finished: refuse('worker_session_finished')
                    if op in ('worker', 'course', 'state', 'check', 'proof', 'finish') and payload: refuse('worker_frame_invalid')
                    if op == 'worker': result = server.backend.inspect_worker(server.worker_id)
                    elif op == 'course': result = server.backend.inspect_course(course, server.worker_id)
                    elif op == 'state': result = server.journal.state(course)
                    elif op == 'check' and mode != 'inspect': result = {'locked': True}
                    elif op == 'fence' and mode == 'exclusive':
                        if (set(payload) != {'request', 'reconciliation'} or type(payload['reconciliation']) is not bool
                                or encoded(payload['request']) != encoded(request)
                                or payload['reconciliation'] != (RECOVERY <= set(request))): refuse('worker_request_invalid')
                        server.backend.validate_request(request, [server.worker_id]); authorize()
                        server.journal.begin(course, session)
                        authorize()
                        server.backend.fence(request, [server.worker_id])
                        authorize()
                        server.backend.stop_endpoint(server.worker_id, course)
                        result = self.proof(server, course, request, authorize); fenced = True
                    elif op in ('proof', 'finish') and mode == 'exclusive' and fenced:
                        result = self.proof(server, course, request, authorize)
                        if op == 'finish':
                            authorize(); server.journal.finish(course, session); finished = True; result = {'finished': True}
                    elif op == 'runtime' and mode == 'activate' and server.runtime_dispatch is not None:
                        if (set(payload) != {'operation', 'payload'} or not isinstance(payload['operation'], str)
                                or not 1 <= len(payload['operation']) <= 128 or not isinstance(payload['payload'], dict)):
                            refuse('worker_frame_invalid')
                        result = server.runtime_dispatch(request, payload['operation'], payload['payload'], authorize)
                    else: refuse('worker_operation_refused')
                    authorize()
                    if not isinstance(result, dict): refuse('worker_backend_refused')
                    send(stream, response(True, result))
        except Exception as error:
            code = error.code if isinstance(error, WorkerError) else 'worker_channel_refused'
            if stream is not None:
                try: send(stream, response(False, code))
                except Exception: pass
        finally:
            if stream is not None:
                with server._guard: server._connections.discard(stream)
                stream.close()

    @staticmethod
    def proof(server, course, request, authorize):
        authorize(); server.backend.validate_request(request, [server.worker_id])
        barrier = server.backend.barrier(server.worker_id, course)
        evidence = server.backend.inspect_course(course, server.worker_id)
        if (not isinstance(barrier, dict) or barrier.get('authority_id') != server.authority_id
                or barrier.get('worker_id') != server.worker_id or barrier.get('course_id') != course
                or barrier.get('request_sha256') != digest(request)
                or type(barrier.get('denied_through_epoch')) is not int or barrier['denied_through_epoch'] < request['old_epoch']
                or not isinstance(evidence, dict) or type(evidence.get('running_count')) is not int or evidence['running_count'] != 0):
            refuse('worker_fence_unverified')
        authorize(); return {'barrier': barrier, 'course': evidence}


class WorkerClient:
    def __init__(self, host, port, authority_id, worker_id, server_sha256, ca_file, certfile, keyfile, timeout=3):
        if (not isinstance(host, str) or not 1 <= len(host) <= 253 or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(authority_id, str) or not HEX.fullmatch(authority_id) or not slug(worker_id)
                or not isinstance(server_sha256, str) or not HASH.fullmatch(server_sha256) or not number(timeout, .05, 10)): refuse()
        self.host, self.port, self.authority_id, self.worker_id = host, port, authority_id, worker_id
        self.server_sha256, self.timeout = server_sha256, timeout
        self.context = tls_context(False, certfile, keyfile, ca_file)

    @contextmanager
    def session(self, course_id, mode, request=None):
        channel = None; stream = None; opened = False
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            try: stream = self.context.wrap_socket(raw, server_hostname=self.host)
            except BaseException: raw.close(); raise
            if hashlib.sha256(stream.getpeercert(binary_form=True)).hexdigest() != self.server_sha256: refuse('worker_server_pin_changed')
            validity = _PeerValidity(stream.getpeercert())
            deadline = time.monotonic() + 60
            challenge = receive(stream, deadline, self.timeout)
            if (set(challenge) != {'version', 'authority_id', 'worker_id', 'session_id', 'challenge'}
                    or challenge['version'] != VERSION or challenge['authority_id'] != self.authority_id
                    or challenge['worker_id'] != self.worker_id or not isinstance(challenge['session_id'], str)
                    or not HEX.fullmatch(challenge['session_id']) or not isinstance(challenge['challenge'], str)
                    or not HASH.fullmatch(challenge['challenge'])): refuse('worker_frame_invalid')
            send(stream, dict(challenge, course_id=course_id, mode=mode, request=request))
            channel = _Channel(stream, challenge, deadline, self.timeout, validity)
            acknowledgement = channel._response(0)
            if (not isinstance(acknowledgement, dict) or set(acknowledgement) != {'opened'}
                    or acknowledgement['opened'] is not True): refuse('worker_frame_invalid')
            opened = True
            yield channel
        except WorkerError: raise
        except Exception:
            if opened: raise
            refuse('worker_transport_unavailable')
        finally:
            if channel is not None: channel.closed = True
            if stream is not None: stream.close()


class _Channel:
    def __init__(self, stream, challenge, deadline, timeout, validity):
        self.stream, self.challenge, self.deadline, self.timeout = stream, challenge, deadline, timeout
        self.validity = validity
        self.sequence, self.closed = 0, False; self.lock = threading.Lock()

    def _response(self, sequence):
        frame = receive(self.stream, self.deadline, self.timeout)
        self.validity.check()
        if (set(frame) != {'version', 'session_id', 'challenge', 'sequence', 'ok', 'result' if frame.get('ok') is True else 'error'}
                or any(frame.get(k) != self.challenge[k] for k in ('version', 'session_id', 'challenge'))
                or type(frame.get('sequence')) is not int or frame['sequence'] != sequence or type(frame.get('ok')) is not bool): refuse('worker_frame_invalid')
        if not frame['ok']:
            if not isinstance(frame['error'], str) or frame['error'] not in ERRORS: refuse('worker_frame_invalid')
            refuse(frame['error'])
        if not isinstance(frame['result'], dict): refuse('worker_frame_invalid')
        return frame['result']

    def call(self, operation, payload=None):
        with self.lock:
            if self.closed or self.sequence >= MAX_CALLS: refuse('worker_session_closed')
            self.sequence += 1
            try:
                self.validity.check()
                send(self.stream, {k: self.challenge[k] for k in ('version', 'session_id', 'challenge')} |
                    {'sequence': self.sequence, 'operation': operation, 'payload': {} if payload is None else payload})
                return self._response(self.sequence)
            except WorkerError: self.closed = True; raise
            except Exception: self.closed = True; refuse('worker_transport_unavailable')

    def finish(self): return self.call('finish')


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Open an existing single-worker mTLS endpoint; never bootstrap state.')
    for name in ('config', 'journal', 'certfile', 'keyfile', 'ca-file', 'acl-file'): parser.add_argument('--' + name, required=True)
    parser.add_argument('--host', default='127.0.0.1'); parser.add_argument('--port', type=int, default=8444)
    args = parser.parse_args()
    try:
        config_path = Path(args.config); info = private(config_path)
        if info.st_size > MAX_FRAME: refuse()
        fd = os.open(config_path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as source: config = parsed(source.read(MAX_FRAME + 1))
        if len(config['workers']) != 1: refuse()
        worker = next(iter(config['workers'])); backend = LinuxWorkerBackend(config)
        journal = WorkerJournal.open(args.journal, config['authority_id'], worker, list(config['courses'][worker]), journal_binding(backend, worker))
        server = WorkerServer((args.host, args.port), backend, worker, args.certfile, args.keyfile, args.ca_file, args.acl_file, journal=journal)
        try: server.serve_forever(poll_interval=.1)
        finally: server.server_close()
    except Exception: raise SystemExit('worker_channel_refused') from None


if __name__ == '__main__': main()
