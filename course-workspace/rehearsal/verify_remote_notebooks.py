"""Actual Python/R over two worker channels on ONE disposable Docker daemon."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(Path(__file__).resolve().parent)]
import evidence
from smoke import kernel, change, xsrf
from hub.authority_client import OPERATIONS, ERRORS
from worker_runtime import SUPERVISOR_RUNTIME_CODES
from worker_transport import ERRORS as WORKER_ERRORS
from runtime_guardian import OBSERVATION_CODES
from verify_authenticated_runtime import command, port, wait, configured_cpu_limit, request_notebook_start
from remote_notebook_fixture import allocation_for_host, mount_plan, WORKERS, ENDPOINTS

VERSION = 'waypoint-remote-notebook-rehearsal-v3'
SCOPE = 'actual-local-docker-one-daemon-two-worker-services-synthetic-notebooks'
PHASES = (
    'central_has_no_socket_homes_or_worker_state',
    'disjoint_worker_state_guardian_mounts_and_actual_caps',
    'managed_enrollment_and_nonowner_role_scope_refusal',
    'source_and_peer_real_python_r_saved_bytes',
    'central_service_loss_guardians_stop_and_retain_homes',
    'local_recovery_helper_stages_exact_stopped_home',
    'post_fence_interruption_quarantines_both_endpoints',
    'stale_approval_refused_fresh_reconciliation_preserves_peer',
    'receiver_real_python_r_retained_work_no_automatic_start',
    'restarted_source_hub_cannot_revive',
    'worker_channel_loss_stops_receiver_preserves_peer',
    'worker_service_restart_retains_floors_journal_and_work',
    'guardian_loss_worker_watchdog_stops_receiver_preserves_peer',
    'managed_hub_rotation_staged_inert_overlap_and_retire',
    'rotated_hub_restarts_with_real_python_r_and_work',
    'guardian_revocation_stops_only_its_worker',
    'owned_cleanup_and_final_source_verified',
)
CONTROLLER_FILES = ('ownership_contract.py', 'authority_access.py', 'authority_operator.py',
    'runtime_authority.py', 'runtime_guardian.py', 'worker_transport.py', 'worker_channel_state.py',
    'worker_runtime.py', 'remote_backend.py', 'remote_control.py', 'remote_runtime_service.py',
    'remote_credentials.py', 'worker_enrollment.py', 'worker_credentials.py', 'volume_archive.py')
CODE_FILES = frozenset(CONTROLLER_FILES) | {'hub/' + name for name in ('authority_client.py', 'authority_spawner.py',
    'home_policy.py', 'jupyterhub_config.py', 'settings.py')} | {'tests/authority_test_pki.py',
    'rehearsal/authenticated_fixture.py', 'rehearsal/authenticated_evidence.py',
    'rehearsal/remote_notebook_fixture.py', 'rehearsal/verify_remote_notebooks.py',
    'rehearsal/test_remote_notebook_fixture.py', 'rehearsal/verify_authenticated_runtime.py', 'smoke.py'}
FLAGS = {'actual_python_r': False, 'actual_mtls_worker_channels': False, 'one_docker_daemon': True,
    'actual_two_host_isolation': False, 'independent_host_file_transfer': False, 'encrypted_backup': False,
    'off_host_backup': False, 'production_ready': False, 'course_capacity_acceptance': False}


def observed_flags(phases):
    if not isinstance(phases, list): raise ValueError('remote_notebook_phases_invalid')
    return dict(FLAGS, actual_python_r=PHASES[3] in phases, actual_mtls_worker_channels=PHASES[2] in phases)


def require_refusal(reply, expected):
    if (not isinstance(reply, dict) or set(reply) != {'ok', 'refused', 'code'}
            or reply['ok'] is not False or reply['refused'] is not True or reply['code'] != expected):
        raise ValueError('remote_notebook_expected_refusal_missing')


def rpc_refusal_context(operation, reply):
    """Only fixed protocol enums; never include a result or private payload."""
    code = reply.get('code') if isinstance(reply, dict) else None
    return {'operation': operation if isinstance(operation, str) and operation in OPERATIONS else 'other',
        'code': code if isinstance(code, str) and code in ERRORS else 'other'}


DIAGNOSTIC_SUPERVISOR_CODES = SUPERVISOR_RUNTIME_CODES | WORKER_ERRORS | frozenset({
    'stale_or_changed', 'worker_error', 'runtime_refusal', 'worker_journal_refused', 'sqlite_error',
    'timeout_error', 'os_error', 'key_error', 'type_error', 'value_error', 'docker_not_found',
    'docker_api_error', 'docker_error', 'docker_transport_timeout', 'docker_connection_error', 'other_exception'})
DIAGNOSTIC_TRANSPORT_STAGES = frozenset({'frame', 'connection', 'request', 'response_headers', 'response_body', 'response_decode', 'response_validate'})
DIAGNOSTIC_TRANSPORT_CATEGORIES = frozenset({'timeout', 'connection', 'tls', 'http', 'other'})
DIAGNOSTIC_GUARDIAN_CODES = OBSERVATION_CODES | frozenset({'guardian_observation_failed'})


def bounded_container_logs(identifier, *, popen=subprocess.Popen, monotonic=time.monotonic):
    """At most128KiB/256reads/3seconds; raw bytes never leave diagnostics."""
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
        raise ValueError('fixture_diagnostic_scope_invalid')
    process = popen(['docker', 'logs', '--tail', '2000', identifier], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    raw = bytearray(); truncated = False; deadline = monotonic() + 3
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            for _ in range(256):
                remaining = deadline - monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise RuntimeError('fixture_diagnostic_unavailable')
                chunk = os.read(process.stdout.fileno(), min(16384, 131073 - len(raw)))
                if not chunk: break
                raw.extend(chunk)
                if len(raw) > 131072:
                    truncated = True; break
            else: truncated = True
        if not truncated and process.wait(timeout=max(.001, deadline - monotonic())) != 0:
            raise RuntimeError('fixture_diagnostic_unavailable')
        return bytes(raw[:131072]).decode('utf-8', errors='replace'), truncated
    finally:
        if process.poll() is None: process.kill()
        process.wait(timeout=2)
        process.stdout.close()


def diagnostic_log_counts(raw):
    result = {'supervisor': {code: 0 for code in sorted(DIAGNOSTIC_SUPERVISOR_CODES)},
        'guardian': {code: 0 for code in sorted(DIAGNOSTIC_GUARDIAN_CODES)}, 'snapshot_busy': 0, 'stop_busy': 0,
        'transport': {stage + '/' + category: 0 for stage in sorted(DIAGNOSTIC_TRANSPORT_STAGES)
            for category in sorted(DIAGNOSTIC_TRANSPORT_CATEGORIES)}}
    for line in raw[:131072].splitlines():
        for field, prefix in (('supervisor', 'Worker runtime supervisor failed: supervision/'),
                ('supervisor', 'Worker runtime stopping owned learners: '), ('guardian', 'Guardian observation refused: ')):
            if prefix in line:
                code = line.partition(prefix)[2].strip()
                if code in result[field]: result[field][code] = min(10000, result[field][code] + 1)
        for field, phrase in (('snapshot_busy', 'Guardian snapshot deferred: endpoint_busy'), ('stop_busy', 'Guardian stop deferred: endpoint_busy')):
            if line.endswith(phrase): result[field] = min(10000, result[field] + 1)
        match = re.search(r'authority_transport_failure stage=([a-z_]+) category=([a-z_]+)$', line)
        if match:
            key = match[1] + '/' + match[2]
            if key in result['transport']: result['transport'][key] = min(10000, result['transport'][key] + 1)
    return result


def diagnostic_http_context(error):
    result = {'available': False, 'status': None, 'final_path_category': 'other',
        'content_type_category': 'other', 'json403_message_class': 'not_applicable'}
    if not isinstance(error, requests.exceptions.HTTPError) or error.response is None: return result
    response = error.response; result['available'] = True
    if type(response.status_code) is int and 100 <= response.status_code <= 599: result['status'] = response.status_code
    try: path = urlsplit(response.url[:4096]).path
    except Exception: path = ''
    if re.fullmatch(r'/user/alice/api/kernels(?:/[A-Za-z0-9-]+(?:/channels)?)?/?', path): result['final_path_category'] = 'notebookkernel'
    elif path.startswith('/hub/user/'): result['final_path_category'] = 'hubfallback'
    elif path == '/hub/login': result['final_path_category'] = 'hublogin'
    elif path in ('/hub/oauth_callback', '/user/alice/oauth_callback', '/hub/api/oauth2/authorize'): result['final_path_category'] = 'oauth'
    media_type = response.headers.get('Content-Type', '')[:128].partition(';')[0].strip().lower()
    result['content_type_category'] = {'application/json': 'json', 'text/html': 'html', 'text/plain': 'text', '': 'missing'}.get(media_type, 'other')
    if result['status'] == 403 and media_type == 'application/json':
        result['json403_message_class'] = 'other'
        try:
            raw = response.content
            if len(raw) > 4096: return result
            value = json.loads(raw); message = value.get('message') if isinstance(value, dict) else None
            if not isinstance(message, str) or len(message) > 1024: return result
            message = message.lower().strip()
            if message in ('forbidden', 'forbidden.'): result['json403_message_class'] = 'forbidden'
            elif message == 'authentication required': result['json403_message_class'] = 'authentication_required'
            elif "'_xsrf' argument missing from " in message: result['json403_message_class'] = 'xsrf_missing'
            elif "'_xsrf' argument invalid" in message: result['json403_message_class'] = 'xsrf_invalid'
            elif 'xsrf cookie does not match ' in message and ' argument' in message: result['json403_message_class'] = 'xsrf_mismatch'
        except Exception: pass
    return result


def runtime_diagnostics(error, name, images, authority_id, *, invoke=command, read_logs=bounded_container_logs):
    def state(value):
        source = value['State']; result = {key: source[field] for key, field in
            (('running', 'Running'), ('oom_killed', 'OOMKilled'), ('exit_code', 'ExitCode'))}
        if (type(result['running']) is not bool or type(result['oom_killed']) is not bool
                or type(result['exit_code']) is not int or not -255 <= result['exit_code'] <= 255):
            raise ValueError('fixture_diagnostic_state_invalid')
        return result
    def owned(identifier, expected_name=None):
        value = json.loads(invoke('inspect', identifier, timeout=3))[0]
        actual = require_owned_container(value, name, images, authority_id)
        if (not re.fullmatch(r'[0-9a-f]{64}', actual) or (re.fullmatch(r'[0-9a-f]{64}', identifier) and actual != identifier)
                or (expected_name is not None and value.get('Name') != '/' + expected_name)):
            raise ValueError('fixture_diagnostic_scope_invalid')
        return value
    result = {'version': 'waypoint-remote-runtime-diagnostics-v1', 'scope': 'post-failure-nonatomic-readonly', 'available': True,
        'http': diagnostic_http_context(error), 'learners': {}, 'enforcers': {}}
    # Capture learner state before any potentially slow log reads.
    for _, _, suffix in ENDPOINTS:
        item = {'available': False, 'container_count': None, 'running_count': None, 'states': []}
        try:
            identifiers = invoke('ps', '-aq', '--no-trunc', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix,
                '--filter', 'label=educloud.workspace.kind=learner', timeout=3).split()
            if len(identifiers) > 1 or any(not re.fullmatch(r'[0-9a-f]{64}', identifier) for identifier in identifiers):
                raise ValueError('fixture_diagnostic_bounds')
            states = [state(owned(identifier, name + '-' + suffix + '-learner-' + hashlib.sha256(b'alice').hexdigest()[:24])) for identifier in identifiers]
            item.update(available=True, container_count=len(states), running_count=sum(value['running'] for value in states), states=states)
        except Exception: pass
        result['learners'][suffix] = item
    for worker in WORKERS:
        for kind in ('service', 'guardian'):
            item = {'available': False, 'state': None, 'logs_available': False, 'truncated': False, 'counts': diagnostic_log_counts('')}
            try:
                value = owned(name + '-' + worker + '-' + kind, name + '-' + worker + '-' + kind)
                item.update(available=True, state=state(value))
                raw, truncated = read_logs(value['Id'])
                if not isinstance(raw, str) or type(truncated) is not bool: raise ValueError('fixture_diagnostic_log_invalid')
                item.update(logs_available=True, truncated=truncated, counts=diagnostic_log_counts(raw))
            except Exception: pass
            result['enforcers'][worker + '-' + kind] = item
    return result


def capture_runtime_diagnostics(path, error, name, images, authority_id, *, collect=runtime_diagnostics, publish=None):
    """Best effort only; diagnostic failure never blocks cleanup or refusal."""
    try:
        if publish is None:
            from authority_access import publish
        try: value = collect(error, name, images, authority_id)
        except Exception: value = {'version': 'waypoint-remote-runtime-diagnostics-v1', 'scope': 'post-failure-nonatomic-readonly', 'available': False}
        publish(path, value)
    except Exception: pass


def exercise_kernels(session, base, invoke=kernel):
    # smoke.kernel's actual protocol requires this exact execution sentinel.
    invoke(session, base, 'alice', 'python3', "print('CONTINUITY_OK')")
    invoke(session, base, 'alice', 'ir', "cat('CONTINUITY_OK\\n')")


def await_worker_readiness(probe, *, timeout=45, monotonic=time.monotonic, sleep=time.sleep):
    """Only explicit transport-startup unavailability can become a retry."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 45:
        raise ValueError('worker_readiness_timeout_invalid')
    deadline = monotonic() + timeout
    while True:
        value = probe()
        if (not isinstance(value, dict) or set(value) != {'ready', 'verified_workers'}
                or type(value['ready']) is not bool or type(value['verified_workers']) is not int
                or not 0 <= value['verified_workers'] <= len(WORKERS)
                or value['ready'] != (value['verified_workers'] == len(WORKERS))):
            raise ValueError('worker_readiness_reply_invalid')
        if value['ready']: return
        remaining = deadline - monotonic()
        if remaining <= 0: raise RuntimeError('authenticated_workers_not_ready')
        sleep(min(.25, remaining))


def verify_source_login_refused(session, base, password):
    session.get(base + '/hub/login', timeout=10).raise_for_status()
    rejected = session.post(base + '/hub/login', data={'username': 'alice', 'password': password,
        '_xsrf': xsrf(session, base + '/hub/login')}, timeout=20, allow_redirects=False)
    if rejected.status_code != 403: raise ValueError('source_login_was_not_denied')
    redirected = session.get(base + '/hub/spawn/alice', timeout=20)
    destination, expected = urlsplit(redirected.url), urlsplit(base)
    if (destination.scheme, destination.netloc, destination.path) != (expected.scheme, expected.netloc, '/hub/login'):
        raise ValueError('source_spawn_was_not_denied')


def await_hub_readiness(session, base, *, timeout=60, monotonic=time.monotonic, sleep=time.sleep):
    """Wait for the Hub process itself before one login/spawn attempt."""
    if type(timeout) not in (int, float) or not 0 < timeout <= 60:
        raise ValueError('hub_readiness_timeout_invalid')
    deadline = monotonic() + timeout
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0: raise RuntimeError('hub_not_ready')
        try:
            response = session.get(base + '/hub/login', timeout=min(3, remaining), allow_redirects=False)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            response = None
        if response is not None:
            if response.status_code == 200: return
            # The proxy may listen before JupyterHub installs its routes.
            if response.status_code not in (404, 503):
                response.raise_for_status()
                raise ValueError('hub_readiness_unexpected_response')
        remaining = deadline - monotonic()
        if remaining <= 0: raise RuntimeError('hub_not_ready')
        sleep(min(.25, remaining))


def require_owned_container(value, name, images, authority_id):
    actual = value.get('Name', '').lstrip('/'); labels = value.get('Config', {}).get('Labels', {})
    image = value.get('Image'); matched = False
    if actual == name + '-authority' or actual in {name + '-' + worker + '-service' for worker in WORKERS}:
        matched = image == images['controller'] and labels.get('educloud.fixture') == name
    elif actual in {name + '-' + worker + '-guardian' for worker in WORKERS}:
        worker = next(worker for worker in WORKERS if actual == name + '-' + worker + '-guardian')
        matched = (image == images['controller'] and labels.get('educloud.runtime.authority') == authority_id
            and authority_id is not None and labels.get('educloud.runtime.worker') == worker
            and labels.get('educloud.runtime.kind') == 'guardian')
    else:
        subject = hashlib.sha256(b'alice').hexdigest()[:24]
        for worker, course, suffix in ENDPOINTS:
            instance = name + '-' + suffix
            if actual == instance + '-hub':
                matched = image == images['hub'] and labels.get('educloud.workspace.instance') == instance and labels.get('educloud.workspace.kind') == 'hub'
            elif actual == instance + '-learner-' + subject:
                matched = (image == images['course'] and authority_id is not None
                    and labels.get('educloud.workspace.instance') == instance and labels.get('educloud.workspace.kind') == 'learner'
                    and labels.get('educloud.runtime.authority') == authority_id and labels.get('educloud.runtime.worker') == worker
                    and labels.get('educloud.runtime.course') == course and labels.get('educloud.runtime.subject') == subject
                    and isinstance(labels.get('educloud.runtime.epoch'), str)
                    and re.fullmatch(r'[1-9][0-9]{0,9}', labels['educloud.runtime.epoch'])
                    and int(labels['educloud.runtime.epoch']) <= 10**9)
            if matched: break
    if not matched: raise ValueError('fixture_container_ownership_unverified')
    return value['Id']


def same_running_enforcer(before, after):
    return (before['Id'] == after['Id'] and before['Image'] == after['Image']
        and before['State']['StartedAt'] == after['State']['StartedAt'] and after['State']['Running'] is True)


def assert_hub_cpu_caps(name, images, authority_id, allocation, inspect_container):
    """Freshly observe every Hub cap before claiming the topology budget."""
    for _, _, suffix in ENDPOINTS:
        expected = name + '-' + suffix + '-hub'
        value = inspect_container(expected)
        require_owned_container(value, name, images, authority_id)
        if (value.get('Name', '').lstrip('/') != expected
                or configured_cpu_limit(value['HostConfig']) != allocation['hub_cpu']):
            raise ValueError('fixture_hub_cpu_unverified')


def wait_for_learner_removal(name, suffix, invoke=command, wait_for=wait):
    """Explicit Hub stop is complete only after stopped containers are removed."""
    wait_for(lambda: not invoke('ps', '-aq', '--filter',
        'label=educloud.workspace.instance=' + name + '-' + suffix,
        '--filter', 'label=educloud.workspace.kind=learner').split(), 30)


def cleanup_participant_containers(name, images, authority_id, invoke=command):
    """Remove verified control writers first; tolerate only exact-ID absence."""
    def snapshot():
        result = {}
        for line in invoke('ps', '-a', '--no-trunc', '--format', '{{.ID}} {{.Names}}').splitlines():
            parts = line.split()
            if (len(parts) != 2 or not re.fullmatch(r'[0-9a-f]{64}', parts[0])
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', parts[1]) or parts[0] in result
                    or parts[1] in result.values()):
                raise ValueError('fixture_cleanup_snapshot_invalid')
            result[parts[0]] = parts[1]
        return result
    def remove(container):
        if not re.fullmatch(r'[0-9a-f]{64}', container): raise ValueError('fixture_cleanup_id_invalid')
        try: value = json.loads(invoke('inspect', container))[0]
        except RuntimeError as error:
            if str(error) != 'docker_fixture_command_failed:inspect' or container in snapshot(): raise
            return
        # Ownership refusal is never converted into an already-gone result.
        if require_owned_container(value, name, images, authority_id) != container:
            raise ValueError('fixture_cleanup_id_changed')
        try: invoke('rm', '-f', container)
        except RuntimeError as error:
            if str(error) != 'docker_fixture_command_failed:rm' or container in snapshot(): raise
        if container in snapshot(): raise ValueError('fixture_cleanup_container_remains')
    controls = [name + '-' + suffix + '-hub' for _, _, suffix in ENDPOINTS]
    controls += [name + '-authority', *(name + '-' + worker + '-guardian' for worker in WORKERS),
        *(name + '-' + worker + '-service' for worker in WORKERS)]
    initial = snapshot()
    for expected in controls:
        for container, actual in initial.items():
            if actual == expected: remove(container)
    if set(snapshot().values()) & set(controls):
        raise ValueError('fixture_cleanup_control_replaced_or_remaining')
    for _, _, suffix in ENDPOINTS:
        for container in invoke('ps', '-aq', '--no-trunc', '--filter',
                'label=educloud.workspace.instance=' + name + '-' + suffix).split():
            remove(container)


def failure_context(error, phases):
    """Bounded diagnostic identifiers only; never messages, URLs or replies."""
    known = (AssertionError, RuntimeError, ValueError, OSError, KeyError, TypeError,
        subprocess.TimeoutExpired, subprocess.CalledProcessError, KeyboardInterrupt,
        requests.exceptions.HTTPError, requests.exceptions.ConnectionError,
        requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout,
        requests.exceptions.TooManyRedirects)
    result = {'phase': PHASES[len(phases)] if len(phases) < len(PHASES) else 'finalization',
        'exception': type(error).__name__ if type(error) in known else 'OtherFailure', 'lines': []}
    trace = error.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code.co_filename == __file__:
            result['lines'].append(trace.tb_lineno)
        elif trace.tb_frame.f_code.co_filename == str(ROOT / 'smoke.py'):
            result['notebook_line'] = trace.tb_lineno
        trace = trace.tb_next
    result['lines'] = result['lines'][-8:]
    if isinstance(error, requests.exceptions.HTTPError):
        status = getattr(error.response, 'status_code', None)
        if type(status) is int and 100 <= status <= 599: result['http_status'] = status
        request = getattr(error.response, 'request', None)
        method = getattr(request, 'method', None)
        if method in ('GET', 'POST', 'PUT', 'DELETE'): result['http_method'] = method
        if request is not None:
            headers = request.headers
            result['xsrf_cookie_count'] = sum(part.strip().startswith('_xsrf=') for part in headers.get('Cookie', '')[:65536].split(';'))
            token = headers.get('X-XSRFToken', '')
            result['xsrf_header_present'] = bool(token)
            result['xsrf_header_quoted'] = token.startswith(('"', "'"))
        body = getattr(error.response, 'text', '')[:4096].lower()
        denial_phrases = ("'_xsrf' argument invalid", *("'_xsrf' argument missing from " + verb
            for verb in ('get', 'post', 'put', 'delete')), *("xsrf cookie does not match " + verb + ' argument'
            for verb in ('get', 'post', 'put', 'delete')))
        if status == 403 and any(phrase in body for phrase in denial_phrases):
            result['request_verification'] = 'xsrf'
    return result


def validate_report(value):
    required = {'version', 'status', 'scope', 'phases', 'cleanup_verified', 'acceptance_eligible',
        'flags', 'source', 'code_sha256', 'images', 'host', 'allocation'}
    if (not isinstance(value, dict) or not required <= set(value) <= required | {'failure'}
            or value['version'] != VERSION or value['scope'] != SCOPE or value['status'] not in ('PASS', 'FAIL')
            or value['flags'] != observed_flags(value.get('phases', []))
            or any(type(value['flags'][key]) is not bool for key in FLAGS)
            or type(value['cleanup_verified']) is not bool or type(value['acceptance_eligible']) is not bool
            or not isinstance(value['phases'], list) or value['phases'] != list(PHASES[:len(value['phases'])])
            or not isinstance(value['source'], str) or not re.fullmatch(r'[0-9a-f]{40}', value['source'])
            or not isinstance(value['code_sha256'], dict) or set(value['code_sha256']) != CODE_FILES
            or any(not isinstance(item, str) or not evidence.HASH.fullmatch(item) for item in value['code_sha256'].values())
            or not isinstance(value['images'], dict) or set(value['images']) != {'course', 'hub', 'controller'}
            or any(not isinstance(item, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', item) for item in value['images'].values())
            or not isinstance(value['host'], dict) or set(value['host']) != {'cpu', 'memory_bytes'}
            or value['allocation'] != allocation_for_host(value['host']['cpu'], value['host']['memory_bytes'])):
        raise ValueError('remote_notebook_report_invalid')
    if any(type(value['allocation'][key]) is bool for key in value['allocation']): raise ValueError('remote_notebook_report_invalid')
    if value['status'] == 'PASS':
        if (value['phases'] != list(PHASES) or value['cleanup_verified'] is not True
                or value['acceptance_eligible'] is not True or 'failure' in value): raise ValueError('remote_notebook_report_invalid')
    elif value['acceptance_eligible'] is not False or value.get('failure') not in {
            'RuntimeError', 'AssertionError', 'ValueError', 'OSError', 'KeyError', 'TypeError',
            'TimeoutExpired', 'CalledProcessError', 'KeyboardInterrupt', 'OtherFailure'}:
        raise ValueError('remote_notebook_report_invalid')
    return value


def assert_fresh_namespace(name, request, invoke=command):
    containers = {name + '-authority', *(name + '-' + worker + '-' + kind for worker in WORKERS for kind in ('service', 'guardian')),
        *(name + '-' + suffix + '-hub' for _, _, suffix in ENDPOINTS)}
    volumes = {name + '-control', *(name + '-' + worker + '-backend' for worker in WORKERS),
        *(name + '-' + suffix + ending for _, _, suffix in ENDPOINTS
            for ending in ('-hubdata', '-home-' + hashlib.sha256(b'alice').hexdigest()[:24]))}
    if containers & set(invoke('ps', '-a', '--format', '{{.Names}}').split()): raise ValueError('fixture_namespace_exists')
    for kind, expected in [('network', {request['network'], request['edge_network'], *request['worker_networks'].values()}), ('volume', volumes)]:
        if expected & set(invoke(kind, 'ls', '--format', '{{.Name}}').split()): raise ValueError('fixture_namespace_exists')
    for kind in ('container', 'network', 'volume'):
        if invoke(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter', 'label=educloud.fixture=' + name):
            raise ValueError('fixture_namespace_exists')
        for _, _, suffix in ENDPOINTS:
            if invoke(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix):
                raise ValueError('fixture_namespace_exists')


def run(args):
    source = evidence._source(ROOT)
    if source['dirty']: raise ValueError('clean_source_required')
    output = Path(args.output)
    if os.path.lexists(output): raise ValueError('fresh_output_required')
    output.mkdir(mode=0o700, parents=True)
    root = Path(tempfile.mkdtemp(prefix='waypoint-remote-notebooks-')).resolve()
    for path in (root / 'control', root / 'backend', *(root / 'backend' / worker for worker in WORKERS)):
        path.mkdir(mode=0o700, exist_ok=True)
    name = 'wa' + uuid.uuid4().hex[:10]; central = name + '-authority'
    phases = []; resources = False; cleaned = False; failure = None; authority_id = None
    images = {role: command('image', 'inspect', getattr(args, role + '_image'), '--format', '{{.Id}}')
              for role in ('course', 'hub', 'controller')}
    info = json.loads(command('info', '--format', '{{json .}}')); host = {'cpu': info['NCPU'], 'memory_bytes': info['MemTotal']}
    allocation = allocation_for_host(host['cpu'], host['memory_bytes'])
    ports = {key: port() for key in ('authority', 'source', 'peer', 'receiver')}
    request = {'name': name, 'network': name + '-authority-control', 'edge_network': name + '-edge',
        'worker_networks': {worker: name + '-' + worker + '-channel' for worker in WORKERS}, 'ports': ports,
        'password': secrets.token_urlsafe(32), **{key + '_image': image for key, image in images.items()}}
    volumes = [name + '-control', *(name + '-' + worker + '-backend' for worker in WORKERS)]
    bases = {key: 'http://127.0.0.1:' + str(ports[key]) for key in ('source', 'peer', 'receiver')}
    proof = {path: evidence.sha256_file(ROOT / path) for path in CODE_FILES}
    report = {'version': VERSION, 'status': 'FAIL', 'scope': SCOPE, 'phases': phases, 'flags': dict(FLAGS),
        'source': source['revision'], 'code_sha256': proof, 'images': images, 'host': host, 'allocation': allocation,
        'cleanup_verified': False, 'acceptance_eligible': False}
    def phase(value):
        assert value == PHASES[len(phases)]; phases.append(value); print(value + ': PASS', flush=True)
    def inspect(container): return json.loads(command('inspect', container))[0]
    def helper(action, payload=None):
        options = ['run', '--rm', '-i', '--network', 'none', '--memory', '256m', '--cpus', str(2 * allocation['learner_cpu']),
            '--mount', 'type=bind,src=' + str(root) + ',dst=' + str(root),
            '--mount', 'type=volume,src=' + volumes[0] + ',dst=' + str(root / 'control') + ',volume-nocopy']
        for worker in WORKERS:
            options += ['--mount', 'type=volume,src=' + name + '-' + worker + '-backend,dst=' + str(root / 'backend' / worker) + ',volume-nocopy']
        options += ['--mount', 'type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock', images['controller'],
            'python', 'remote_notebook_fixture.py', action, str(root)]
        return json.loads(command(*options, data=json.dumps(payload or {})))
    def central_helper(action, payload=None):
        return json.loads(command('exec', '-i', central, 'python', 'remote_notebook_fixture.py', action, str(root), data=json.dumps(payload or {})))
    def rpc(operation, payload=None, identity='operator', worker='worker-one', course='course-main'):
        directory = root / identity / 'credentials'
        reply = json.loads(command('run', '--rm', '-i', '--network', request['network'], '--memory', '128m',
            '--cpus', str(allocation['learner_cpu']), '--mount', 'type=bind,src=' + str(directory) + ',dst=' + str(directory) + ',readonly',
            images['controller'], 'python', 'remote_notebook_fixture.py', 'rpc', str(directory),
            data=json.dumps({'operation': operation, 'payload': payload or {}, 'worker': worker, 'course': course})))
        if not isinstance(reply, dict) or reply.get('ok') is not True:
            print('RPC_REFUSAL_CONTEXT ' + json.dumps(rpc_refusal_context(operation, reply), sort_keys=True), flush=True)
        return reply
    def learners(suffix): return command('ps', '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix,
        '--filter', 'label=educloud.workspace.kind=learner').split()
    def ready(suffix):
        worker = 'worker-two' if suffix == 'receiver' else 'worker-one'; course = 'course-peer' if suffix == 'peer' else 'course-main'
        runtime = root / worker / 'credentials/runtime.json'
        value = json.loads(command('exec', name + '-' + worker + '-service', 'python', 'remote_notebook_fixture.py', 'worker-status', str(runtime)))
        return value[course]['ready']
    def start_service():
        command('start', central)
        command('exec', '-d', central, 'python', 'remote_notebook_fixture.py', 'serve-fenced-fault', str(root))
        wait(lambda: rpc('ownership.inventory')['ok'], 45)
    def notebook(suffix):
        wait(lambda: ready(suffix), 30); base = bases[suffix]; session = requests.Session(); session.max_redirects = 8
        await_hub_readiness(session, base)
        session.get(base + '/hub/login', timeout=10).raise_for_status()
        session.post(base + '/hub/login', data={'username': 'alice', 'password': request['password'], '_xsrf': xsrf(session, base + '/hub/login')},
            timeout=30, allow_redirects=False).raise_for_status()
        request_notebook_start(session, base, previous_runtime_stopped=not learners(suffix))
        def established():
            session.get(base + '/user/alice/lab', timeout=10)
            response = session.get(base + '/user/alice/api/contents', timeout=10)
            return response.status_code == 200 and 'application/json' in response.headers.get('Content-Type', '')
        wait(established, 180); return session
    def read(session, suffix):
        value = session.get(bases[suffix] + '/user/alice/api/contents/coursework.txt', timeout=10)
        value.raise_for_status(); assert value.json()['content'] == 'synthetic remote-channel coursework\n'
    def kernels(session, suffix):
        exercise_kernels(session, bases[suffix])
    def stop(session, suffix):
        change(session, 'DELETE', bases[suffix] + '/hub/api/users/alice/server').raise_for_status()
        wait_for_learner_removal(name, suffix)
    def credential(payload):
        review = central_helper('credential-preview', payload)
        return central_helper('credential-apply', {key: review[key] for key in ('review_id', 'review_sha256')})
    try:
        # Execute exact reviewed source bytes, not merely a matching tag.
        mapping = {str(ROOT / path): '/srv/authority/' + path for path in CONTROLLER_FILES}
        mapping.update({str(ROOT / 'hub/authority_client.py'): '/srv/authority/hub/authority_client.py',
            str(ROOT / 'tests/authority_test_pki.py'): '/srv/authority/authority_test_pki.py',
            **{str(ROOT / 'rehearsal' / path): '/srv/authority/' + path for path in
                ('authenticated_fixture.py', 'authenticated_evidence.py', 'remote_notebook_fixture.py')}})
        for image, files in [(images['controller'], mapping), (images['hub'], {str(path): '/srv/jupyterhub/' + path.name for path in (ROOT / 'hub').glob('*.py')})]:
            expected = {target: evidence.sha256_file(source) for source, target in files.items()}
            probe = 'import sys,json,hashlib;from pathlib import Path;print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in json.loads(sys.argv[1])}))'
            assert json.loads(command('run', '--rm', '--network', 'none', '--entrypoint', 'python', image, '-c', probe, json.dumps(list(expected)))) == expected
        assert_fresh_namespace(name, request)
        resources = True
        for network in [request['network'], request['edge_network'], *request['worker_networks'].values()]:
            command('network', 'create', *(['--internal'] if network != request['edge_network'] else []), '--label', 'educloud.fixture=' + name, network)
        for volume in volumes: command('volume', 'create', '--label', 'educloud.fixture=' + name, volume)
        observed = helper('bootstrap', request); assert observed['allocation'] == allocation
        authority_id = observed['authority_id']
        helper('launch-workers')
        command('run', '-d', '--name', central, '--network', request['network'], '--network-alias', 'authority.local.test',
            '--label', 'educloud.fixture=' + name, '--mount', 'type=volume,src=' + volumes[0] + ',dst=' + str(root / 'control') + ',volume-nocopy',
            '--memory', '256m', '--cpus', str(allocation['central_cpu']), images['controller'], 'python', '-c', 'import time;time.sleep(86400)')
        for network in request['worker_networks'].values(): command('network', 'connect', network, central)
        central_mounts = inspect(central)['Mounts']; assert len(central_mounts) == 1 and central_mounts[0]['Destination'] == str(root / 'control')
        assert central_mounts[0]['Name'] == volumes[0]; phase(PHASES[0])
        expected_mounts = mount_plan(root, request)
        for worker in WORKERS:
            service = inspect(name + '-' + worker + '-service')
            assert {mount['Destination'] for mount in service['Mounts']} == {value['bind'] for value in expected_mounts[worker].values()}
            assert configured_cpu_limit(service['HostConfig']) == allocation['worker_service_cpu']
            assert service['HostConfig']['Memory'] == 256 * 1024**2
        assert configured_cpu_limit(inspect(central)['HostConfig']) == allocation['central_cpu']
        await_worker_readiness(lambda: central_helper('worker-ready'))
        central_helper('register'); central_helper('enrollment-create')
        for worker in WORKERS: assert credential({'action': 'enroll', 'worker': worker})['status'] == 'COMPLETE'
        start_service(); helper('launch-users')
        for worker in WORKERS:
            guardian = inspect(name + '-' + worker + '-guardian')
            assert configured_cpu_limit(guardian['HostConfig']) == allocation['guardian_cpu']
            assert all('/homes' not in mount['Destination'] and 'control' not in mount['Destination'] for mount in guardian['Mounts'])
        assert_hub_cpu_caps(name, images, authority_id, allocation, inspect)
        phase(PHASES[1])
        assert rpc('hub.authorize', identity='source')['ok'] is True
        assert all(value == {'quarantined': False} for value in central_helper('worker-quarantine').values())
        require_refusal(rpc('hub.authorize', identity='source', worker='worker-two'), 'authority_access_refused')
        require_refusal(rpc('hub.authorize', identity='receiver', worker='worker-two'), 'authority_operation_refused')
        require_refusal(rpc('ownership.inventory', identity='source'), 'authority_access_refused')
        assert rpc('hub.authorize', identity='source')['ok'] is True
        assert all(value == {'quarantined': False} for value in central_helper('worker-quarantine').values()); phase(PHASES[2])
        sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
        for suffix, session in sessions.items():
            kernels(session, suffix)
            change(session, 'PUT', bases[suffix] + '/user/alice/api/contents/coursework.txt',
                json={'type': 'file', 'format': 'text', 'content': 'synthetic remote-channel coursework\n'}).raise_for_status()
            read(session, suffix)
            hub = inspect(name + '-' + suffix + '-hub'); learner = inspect(learners(suffix)[0])
            assert not any(mount['Destination'] == '/var/run/docker.sock' or '/homes' in mount['Destination'] for mount in hub['Mounts'])
            limits = learner['HostConfig']; assert limits['ReadonlyRootfs'] and limits['PidsLimit'] == 128 and not limits['Privileged']
            assert limits['Memory'] == limits['MemorySwap'] == 512 * 1024**2 and configured_cpu_limit(limits) == allocation['learner_cpu']
            assert len(learner['Mounts']) == 1 and learner['Mounts'][0]['Destination'] == '/home/learner'
            assert len(learner['NetworkSettings']['Networks']) == 1 and not any('AUTHORITY' in item for item in learner['Config']['Env'])
        phase(PHASES[3])
        command('stop', central); wait(lambda: not learners('source') and not learners('peer'), 25)
        start_service(); sessions = {suffix: notebook(suffix) for suffix in ('source', 'peer')}
        for suffix, session in sessions.items(): read(session, suffix)
        phase(PHASES[4])
        stop(sessions['source'], 'source'); assert helper('copy-home') == {'copied': True, 'local_recovery_staging': True, 'network_file_transfer': False}
        peer_ids = learners('peer'); read(sessions['peer'], 'peer'); phase(PHASES[5])
        plan = rpc('ownership.preview', {'receiver_worker_id': 'worker-two'}); assert plan['ok'] is True
        reference = {key: plan['result'][key] for key in ('plan_id', 'plan_sha256')}
        require_refusal(rpc('ownership.apply', reference), 'authority_operation_refused')
        assert rpc('ownership.inventory')['result']['pending'] is True
        assert all(value == {'quarantined': True} for value in central_helper('worker-quarantine').values())
        command('restart', name + '-worker-two-service')
        wait(lambda: all(value == {'quarantined': True} for value in central_helper('worker-quarantine').values()), 30)
        for worker in WORKERS:
            require_refusal(rpc('hub.authorize', identity='source' if worker == 'worker-one' else 'receiver', worker=worker), 'authority_operation_refused')
        assert learners('peer') == peer_ids; read(sessions['peer'], 'peer'); phase(PHASES[6])
        helper('drift-snapshot'); require_refusal(rpc('ownership.apply', reference), 'authority_operation_refused')
        recovery = rpc('ownership.reconcile_preview', {'selected_worker_id': 'worker-two'}); assert recovery['ok'] is True
        ref = {key: recovery['result'][key] for key in ('plan_id', 'plan_sha256')}
        completed = rpc('ownership.reconcile_apply', ref); assert completed['ok'] is True
        assert all(value == {'quarantined': False} for value in central_helper('worker-quarantine').values())
        assert learners('peer') == peer_ids; kernels(sessions['peer'], 'peer'); read(sessions['peer'], 'peer'); phase(PHASES[7])
        assert not learners('receiver'); receiver = notebook('receiver'); kernels(receiver, 'receiver'); read(receiver, 'receiver'); phase(PHASES[8])
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        command('restart', name + '-source-hub'); require_refusal(rpc('hub.authorize', identity='source'), 'authority_operation_refused')
        wait(lambda: requests.get(bases['source'] + '/hub/login', timeout=3).status_code == 200, 60)
        verify_source_login_refused(requests.Session(), bases['source'], request['password'])
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        assert not learners('source') and learners('peer') == peer_ids; phase(PHASES[9])
        worker_two = name + '-worker-two-service'; guardian_two = name + '-worker-two-guardian'
        guardian_before = inspect(guardian_two)
        command('kill', '--signal', 'KILL', require_owned_container(inspect(worker_two), name, images, authority_id))
        wait(lambda: not learners('receiver'), 25)
        assert same_running_enforcer(guardian_before, inspect(guardian_two))
        assert learners('peer') == peer_ids; read(sessions['peer'], 'peer'); phase(PHASES[10])
        command('start', worker_two); receiver = notebook('receiver'); read(receiver, 'receiver'); phase(PHASES[11])
        worker_before = inspect(worker_two)
        command('kill', '--signal', 'KILL', require_owned_container(inspect(guardian_two), name, images, authority_id))
        wait(lambda: not learners('receiver'), 25)
        assert same_running_enforcer(worker_before, inspect(worker_two))
        assert learners('peer') == peer_ids; read(sessions['peer'], 'peer'); phase(PHASES[12])
        command('start', guardian_two); receiver = notebook('receiver'); read(receiver, 'receiver')
        stage = {'worker': 'worker-two', 'slot': 'hub:course-main', 'identity': 'receiver-rotated'}
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        require_refusal(rpc('hub.authorize', identity='receiver-rotated', worker='worker-two'), 'authority_access_refused')
        credential(dict(stage, action='stage')); require_refusal(rpc('hub.authorize', identity='receiver-rotated', worker='worker-two'), 'authority_access_refused')
        assert rpc('hub.authorize', identity='receiver', worker='worker-two')['ok'] is True
        credential(dict(stage, action='activate', overlap_seconds=300))
        for identity in ('receiver', 'receiver-rotated'): assert rpc('hub.authorize', identity=identity, worker='worker-two')['ok'] is True
        credential({'action': 'retire', 'worker': 'worker-two', 'target_identity': 'receiver'})
        require_refusal(rpc('hub.authorize', identity='receiver', worker='worker-two'), 'authority_access_refused')
        assert rpc('hub.authorize', identity='receiver-rotated', worker='worker-two')['ok'] is True; phase(PHASES[13])
        # Old certificate is retired: trusted fixture stops its exact learner
        # through the newly authorized identity, then installs while Hub is off.
        learner = inspect(learners('receiver')[0])
        assert rpc('hub.stop', {'subject': 'alice', 'server_id': learner['Id'], 'epoch': completed['result']['epoch']},
            identity='receiver-rotated', worker='worker-two')['ok'] is True
        wait_for_learner_removal(name, 'receiver'); command('stop', name + '-receiver-hub')
        helper('install-rotated-hub'); command('start', name + '-receiver-hub')
        receiver = notebook('receiver'); kernels(receiver, 'receiver'); read(receiver, 'receiver'); assert learners('peer') == peer_ids; phase(PHASES[14])
        credential({'action': 'revoke', 'worker': 'worker-two', 'target_identity': 'guardian-two'})
        wait(lambda: not learners('receiver'), 25); assert learners('peer') == peer_ids; kernels(sessions['peer'], 'peer'); read(sessions['peer'], 'peer'); phase(PHASES[15])
    except BaseException as exc:
        print('FAILURE_CONTEXT ' + json.dumps(failure_context(exc, phases), sort_keys=True), flush=True)
        try: capture_runtime_diagnostics(output.resolve() / 'RUNTIME-DIAGNOSTICS.json', exc, name, images, authority_id)
        except Exception: pass
        failure = type(exc).__name__ if type(exc).__name__ in {'RuntimeError', 'AssertionError', 'ValueError', 'OSError', 'KeyError', 'TypeError',
            'TimeoutExpired', 'CalledProcessError', 'KeyboardInterrupt'} else 'OtherFailure'
    finally:
        try:
            if resources:
                # Invocation-owned participant containers only; the privileged
                # cleanup helper runs AFTER every runtime participant is gone.
                cleanup_participant_containers(name, images, authority_id)
                if (root / 'fixture-owner.json').exists(): helper('cleanup-permissions')
                for kind, flag in [('network', 'label=educloud.fixture=' + name), ('volume', 'label=educloud.fixture=' + name)]:
                    for item in command(kind, 'ls', '-q', '--filter', flag).split(): command(kind, 'rm', item)
                for suffix in ('source', 'peer', 'receiver'):
                    for kind in ('network', 'volume'):
                        for item in command(kind, 'ls', '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix).split(): command(kind, 'rm', item)
                assert not command('ps', '-aq', '--filter', 'label=educloud.fixture=' + name)
                for kind in ('network', 'volume'): assert not command(kind, 'ls', '-q', '--filter', 'label=educloud.fixture=' + name)
                for _, _, suffix in ENDPOINTS:
                    for kind in ('container', 'network', 'volume'):
                        assert not command(kind, 'ls', '-aq' if kind == 'container' else '-q', '--filter', 'label=educloud.workspace.instance=' + name + '-' + suffix)
            shutil.rmtree(root); cleaned = True
        except Exception: cleaned = False
    report['cleanup_verified'] = cleaned
    report['flags'] = observed_flags(phases)
    if failure is None and cleaned:
        try:
            assert evidence._source(ROOT) == source and {path: evidence.sha256_file(ROOT / path) for path in CODE_FILES} == proof
            phase(PHASES[16]); report.update(status='PASS', acceptance_eligible=True)
        except Exception: failure = 'ValueError'
    if report['status'] != 'PASS': report['failure'] = failure or 'RuntimeError'
    validate_report(report)
    from authority_access import publish
    publish(output.resolve() / 'REMOTE-NOTEBOOKS.json', report)
    if report['status'] != 'PASS': raise RuntimeError('remote_notebook_rehearsal_failed')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('course', 'hub', 'controller'): parser.add_argument('--' + key + '-image', default='waypoint-authority-' + key + ':local')
    parser.add_argument('--output', required=True)
    try: run(parser.parse_args())
    except BaseException: raise SystemExit('remote_notebook_rehearsal_failed') from None
