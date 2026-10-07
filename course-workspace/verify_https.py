#!/usr/bin/env python3
"""Exercise rendered nginx TLS/proxy configuration on loopback, with a synthetic backend."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time

from public_course import render


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Backend(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get('Upgrade') == 'websocket':
            assert self.headers.get('Connection', '').lower() == 'upgrade'
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.end_headers()
            self.close_connection = True
        else:
            body = json.dumps(dict(self.headers)).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def main():
    nginx = shutil.which('nginx')
    if not nginx:
        raise SystemExit('install nginx and openssl on a disposable test runner')
    backend = ThreadingHTTPServer(('127.0.0.1', 0), Backend)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix='educloud-https-') as d:
        root = Path(d)
        http_port, tls_port = free_port(), free_port()
        conf, _, _ = render('work.example.test', 'https://auth.example.test/realms/test',
                             'course', 'trial-https', ['subject-a'], '/srv/homes', backend.server_port)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=work.example.test', '-keyout', str(root / 'privkey.pem'),
                        '-out', str(root / 'fullchain.pem')], check=True, capture_output=True)
        (root / 'privkey.pem').chmod(0o600)
        conf = conf.replace('listen 80', f'listen 127.0.0.1:{http_port}').replace('listen 443', f'listen 127.0.0.1:{tls_port}')
        conf = conf.replace('/etc/educloud/tls', str(root))
        config = root / 'nginx.conf'
        config.write_text(f'pid {root}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off;\n' + conf + '\n}\n')
        command = [nginx, '-p', str(root), '-c', str(config)]
        subprocess.run(command + ['-t'], check=True)
        process = subprocess.Popen(command + ['-g', 'daemon off;'])
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(('127.0.0.1', http_port), timeout=1):
                        break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError('nginx exited')
                    time.sleep(.1)
            conn = http.client.HTTPConnection('127.0.0.1', http_port, timeout=5)
            conn.request('GET', '/hub/login?next=course', headers={'Host': 'work.example.test'})
            response = conn.getresponse()
            assert response.status == 301
            assert response.getheader('Location') == 'https://work.example.test/hub/login?next=course'
            conn.close()
            context = ssl._create_unverified_context()  # Local disposable self-signed fixture only.
            def connect(host):
                connection = http.client.HTTPSConnection(host, tls_port, timeout=5, context=context)
                connection.sock = context.wrap_socket(socket.create_connection(('127.0.0.1', tls_port), timeout=5), server_hostname=host)
                return connection
            for host in ['work.example.test', 'subject-a.work.example.test']:
                conn = connect(host)
                conn.request('GET', '/probe', headers={'Host': host, 'X-Forwarded-For': '203.0.113.7', 'X-Forwarded-Proto': 'http'})
                response = conn.getresponse()
                assert response.status == 200
                headers = {k.lower(): v for k, v in json.loads(response.read()).items()}
                assert headers['host'] == host and headers['x-forwarded-host'] == host
                assert headers['x-forwarded-proto'] == 'https' and headers['x-forwarded-for'] == '127.0.0.1'
                conn.close()
            conn = connect('subject-a.work.example.test')
            conn.request('GET', '/kernel', headers={'Host': 'subject-a.work.example.test', 'Upgrade': 'websocket', 'Connection': 'Upgrade'})
            response = conn.getresponse()
            assert response.status == 101 and response.getheader('Upgrade') == 'websocket'
            conn.close()
            try:
                connect('unrelated.example.test')
            except ssl.SSLError:
                pass
            else:
                raise AssertionError('unknown TLS host was accepted')
            print('PASS: nginx syntax, HTTPS redirect, root/user TLS routes, forwarded headers, WebSocket upgrade, unknown-host rejection.')
        finally:
            process.terminate()
            process.wait(timeout=10)
            backend.shutdown()
            backend.server_close()


if __name__ == '__main__':
    main()
