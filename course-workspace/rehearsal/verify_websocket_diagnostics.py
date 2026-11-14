"""One finite TLS/CHP experiment; no notebooks, Docker or learner credentials."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import websocket
from smoke import websocket_url
from verify_remote_notebooks import diagnostic_log_counts


def run(module_root, output):
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'version': 'waypoint-websocket-diagnostics-v1', 'status': 'FAIL',
              'scope': 'synthetic-loopback-tls-proxy-only', 'kernel_execution': False,
              'historical_503_cause_proven': False, 'student_release': False,
              'checks': [], 'cleanup_verified': False}
    root = Path(__file__).resolve().parents[2]
    report['source'] = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    report['source_dirty'] = bool(subprocess.check_output(['git', '-C', str(root), 'status', '--porcelain'], text=True))
    report['code_sha256'] = {name: hashlib.sha256((root / 'course-workspace' / name).read_bytes()).hexdigest()
        for name in ('smoke.py', 'rehearsal/verify_websocket_diagnostics.py',
                     'rehearsal/websocket_proxy_fixture.mjs', 'rehearsal/verify_remote_notebooks.py')}
    child = None
    port = None
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix='waypoint-proxy-control-') as private:
            work = Path(private)
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                '-keyout', str(work / 'key.pem'), '-out', str(work / 'cert.pem'), '-days', '1',
                '-subj', '/CN=127.0.0.1', '-addext', 'subjectAltName=IP:127.0.0.1'],
                check=True, capture_output=True, timeout=15)
            with (work / 'proxy.log').open('w+') as logs:
                child = subprocess.Popen(['node', str(Path(__file__).with_name('websocket_proxy_fixture.mjs')),
                    str(module_root.resolve()), str(work)], stdout=logs, stderr=subprocess.STDOUT,
                    env={**os.environ, 'NO_PROXY': '127.0.0.1,localhost'})
                deadline = time.monotonic() + 10
                while not (work / 'ready.json').exists():
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('proxy_start_refused')
                    time.sleep(.05)
                ready = json.loads((work / 'ready.json').read_text()); port = ready['port']
                report['runtime'] = {'node': ready['node'], 'proxy': ready['proxy']}
                report['proxy_source_sha256'] = hashlib.sha256(
                    (module_root / 'lib/configproxy.js').read_bytes()).hexdigest()
                base = f'https://127.0.0.1:{port}'
                for name in ('good', 'refused', 'upstream'):
                    url = websocket_url(base + f'/user/alice/api/kernels/synthetic-{name}/channels')
                    try:
                        connection = websocket.create_connection(url, origin=base, timeout=5,
                            sslopt={'ca_certs': str(work / 'cert.pem')}, http_no_proxy=['127.0.0.1'])
                    except websocket.WebSocketBadStatusException as exc:
                        if name == 'good' or exc.status_code != 503: raise
                        # Report only controlled categories, never headers/body or URLs.
                        observed = {'case': name, 'status': exc.status_code}
                    else:
                        try:
                            if name != 'good' or connection.recv() != 'PROXY_OK':
                                raise AssertionError('proxy_control_mismatch')
                        finally:
                            connection.close()
                        observed = {'case': name, 'status': 101, 'tls_verified': True}
                    logs.flush()
                    logs.seek(0)
                    counts = diagnostic_log_counts(logs.read(131072))['proxy_transport']
                    observed['cumulative_transport_counts'] = counts
                    report['checks'].append(observed)
                    expected = 0 if name == 'good' else 1
                    if counts['ECONNREFUSED'] != expected or sum(counts.values()) != expected:
                        raise AssertionError('proxy_diagnostic_count_mismatch')
                report['status'] = 'PASS'
    except Exception as exc:
        report['failure_type'] = type(exc).__name__  # No exception message/private material.
    finally:
        if child is not None:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
        closed = port is None
        if port is not None:
            with socket.socket() as probe:
                probe.settimeout(1)
                closed = probe.connect_ex(('127.0.0.1', port)) != 0
        report['cleanup_verified'] = (child is None or child.poll() is not None) and closed
        if not report['cleanup_verified']: report['status'] = 'FAIL'
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        (output / 'RESULT.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--proxy-module', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.exit(run(args.proxy_module, args.output))
