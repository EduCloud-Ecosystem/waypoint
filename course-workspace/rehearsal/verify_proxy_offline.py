"""Finite local nginx connection-failure control; no classroom or model service."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from public_course import render

NGINX = 'nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-image', required=True)
    parser.add_argument('--rendered-config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--control', choices=('blocked', 'slow', 'forbidden'), default='blocked')
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', args.python_image): parser.error('require an immutable local Python image')
    args.output.mkdir(parents=True, exist_ok=False)
    name = 'waypoint-proxy-control-' + uuid.uuid4().hex[:12]
    containers = []; network = None
    def docker(*values):
        return subprocess.check_output(['docker', *values], text=True, stderr=subprocess.PIPE, timeout=30).strip()
    report = {'scope': 'synthetic-local-proxy-connection-only', 'control': args.control, 'passed': False, 'cleanup': False,
        'evaluator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        report['images'] = {key: docker('image', 'inspect', value, '--format', '{{.Id}}')
            for key, value in (('nginx', NGINX), ('python', args.python_image))}
        network = docker('network', 'create', '--internal', '--label', 'educloud.fixture=' + name, name)
        # Fill a one-entry TCP accept queue. Further connects cannot establish;
        # no iptables privilege, external address, or classroom traffic involved.
        program = "import socket,time; s=socket.socket(); s.bind(('0.0.0.0',8081)); s.listen(0); c=socket.create_connection(('127.0.0.1',8081)); print('ready',flush=True); time.sleep(180)"
        if args.control != 'blocked':
            program = """import time,socket
# Avoid reverse-DNS startup on this deliberately isolated network.
socket.getfqdn=lambda name: "synthetic.invalid"
from http.server import BaseHTTPRequestHandler,HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(DELAY)
        self.send_response(STATUS); self.end_headers(); self.wfile.write(b'synthetic-control')
    def log_message(self,*args): pass
server=HTTPServer(('0.0.0.0',8081),Handler)
print('ready',flush=True); server.serve_forever()
""".replace('DELAY', '6' if args.control == 'slow' else '0').replace('STATUS', '200' if args.control == 'slow' else '403')
        upstream = docker('run', '-d', '--pull', 'never', '--name', name + '-upstream', '--network', network,
            '--network-alias', 'upstream', '--label', 'educloud.fixture=' + name,
            '--memory', '128m', '--cpus', '.25', args.python_image, 'python', '-c', program)
        containers.append(upstream)
        for _ in range(30):
            if docker('logs', upstream) == 'ready': break
            time.sleep(.1)
        else: raise RuntimeError('control_upstream_not_ready')
        generated = args.rendered_config.read_text() if args.rendered_config else render(
            'work.example.test', 'https://auth.example.test/realms/course', 'course', 'course-test', ['alice'], '/srv/homes')[0]
        report['rendered_sha256'] = hashlib.sha256(generated.encode()).hexdigest()
        # Preserve the actual renderer's entire course server/location policy.
        # This control adapts TLS and loopback only; it does not qualify TLS/SSO.
        mapping = generated[generated.index('map '):generated.index('server {')]
        server = generated[generated.rindex('server {'):]
        server = re.sub(r'^\s*ssl_[^\n]+\n', '', server, flags=re.M)
        server = server.replace('listen 443 ssl;', 'listen 8080;').replace('http://127.0.0.1:18000', 'http://upstream:8081')
        with tempfile.TemporaryDirectory(prefix=name) as temporary:
            config = Path(temporary) / 'nginx.conf'
            config.write_text('events {}\nhttp {\n' + mapping + server + '\n}\n')
            proxy = docker('run', '-d', '--pull', 'never', '--name', name + '-proxy', '--network', network,
                '--label', 'educloud.fixture=' + name, '--memory', '64m', '--cpus', '.25',
                '--network-alias', 'proxy', '-v', str(config) + ':/etc/nginx/nginx.conf:ro', NGINX)
            containers.append(proxy)
            docker('exec', proxy, 'nginx', '-t')
            program = """import json,time,urllib.request,urllib.error
started=time.monotonic(); result={}
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
request=urllib.request.Request('http://proxy:8080/hub/login',headers={'Host':'work.example.test'})
try:
    result['status']=opener.open(request,timeout=10).status
except urllib.error.HTTPError as error:
    result['status']=error.code
except Exception as error:
    result['failure']=type(error).__name__
result['elapsed_seconds']=round(time.monotonic()-started,3)
print(json.dumps(result))
"""
            probe = docker('create', '--pull', 'never', '--name', name + '-probe', '--network', network,
                '--label', 'educloud.fixture=' + name, '--memory', '128m', '--cpus', '.25',
                args.python_image, 'python', '-c', program)
            containers.append(probe)
            report.update(json.loads(docker('start', '-a', probe)))
            expected = {'blocked': 503, 'slow': 200, 'forbidden': 403}[args.control]
            report['passed'] = report.get('status') == expected and report['elapsed_seconds'] < 9
            if args.control == 'slow': report['passed'] = report['passed'] and report['elapsed_seconds'] >= 6
    except Exception as error:
        report['failure'] = type(error).__name__
    finally:
        cleanup_errors = []
        for identifier in reversed(containers):
            try: docker('rm', '-f', identifier)
            except Exception as error: cleanup_errors.append(type(error).__name__)
        if network:
            try: docker('network', 'rm', network)
            except Exception as error: cleanup_errors.append(type(error).__name__)
        try: report['cleanup'] = not cleanup_errors and not docker('ps', '-aq', '--filter', 'label=educloud.fixture=' + name)
        except Exception as error: cleanup_errors.append(type(error).__name__)
        if cleanup_errors: report['cleanup_failures'] = cleanup_errors
        (args.output / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True))
    return 0 if report['passed'] and report['cleanup'] else 1


if __name__ == '__main__': sys.exit(main())
