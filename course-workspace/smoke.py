#!/usr/bin/env python3
"""Synthetic local-only acceptance. Requires requests and websocket-client."""
import argparse
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import time
import subprocess
import uuid
from urllib.parse import urlsplit

import requests
import websocket


def config(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if line and not line.startswith('#'))


def xsrf(session, url):
    prepared = session.prepare_request(requests.Request('GET', url))
    for part in prepared.headers.get('Cookie', '').split(';'):
        if part.strip().startswith('_xsrf='):
            return part.strip().split('=', 1)[1]
    return ''


def change(session, method, url, **kwargs):
    return session.request(method, url, headers={'X-XSRFToken': xsrf(session, url)}, timeout=60, **kwargs)


def login(base, username, password):
    session = requests.Session()
    session.get(base + '/hub/login', timeout=30).raise_for_status()
    response = session.post(base + '/hub/login', data={'username':username, 'password':password,
                            '_xsrf':xsrf(session, base + '/hub/login')}, timeout=60)
    response.raise_for_status()
    session.get(base + '/hub/spawn/' + username, timeout=60).raise_for_status()
    for _ in range(60):
        # Notebook HTML performs the Hub OAuth redirect. Its JSON API may
        # correctly return 403 until that browser-style exchange completes.
        session.get(base + '/user/' + username + '/lab', timeout=10)
        response = session.get(base + '/user/' + username + '/api/contents', timeout=10)
        if response.status_code == 200 and 'application/json' in response.headers.get('Content-Type', ''):
            print(f'{username}: authenticated notebook server ready', flush=True)
            return session
        time.sleep(1)
    raise RuntimeError(f'{username}: workspace did not become ready')


def kernel(session, base, username, name, code):
    url = f'{base}/user/{username}/api/kernels'
    response = change(session, 'POST', url, json={'name':name}); response.raise_for_status()
    kid = response.json()['id']
    wsurl = url.replace('http://', 'ws://') + '/' + kid + '/channels'
    cookie = session.prepare_request(requests.Request('GET', url)).headers.get('Cookie', '')
    connection = websocket.create_connection(wsurl, cookie=cookie, origin=base, timeout=30)
    msg_id = uuid.uuid4().hex
    connection.send(json.dumps({'header':{'msg_id':msg_id, 'username':username, 'session':uuid.uuid4().hex,
                   'msg_type':'execute_request', 'version':'5.3', 'date':datetime.now(timezone.utc).isoformat()},
                   'parent_header':{}, 'metadata':{}, 'channel':'shell',
                   'content':{'code':code, 'silent':False, 'store_history':False, 'user_expressions':{}, 'allow_stdin':False}}))
    output = ''
    while True:
        msg = json.loads(connection.recv())
        if msg.get('parent_header', {}).get('msg_id') != msg_id: continue
        if msg['msg_type'] == 'stream': output += msg['content']['text']
        if msg['msg_type'] == 'execute_reply':
            if msg['content']['status'] != 'ok': raise AssertionError('kernel execution failed')
        if msg['msg_type'] == 'status' and msg['content']['execution_state'] == 'idle': break
    connection.close()
    change(session, 'DELETE', url + '/' + kid).raise_for_status()
    assert 'CONTINUITY_OK' in output, (name, output)
    print(f'{username}: {name} kernel executed through notebook WebSocket', flush=True)


def runtime_policy(env, users=('alice', 'bob')):
    instance = env['WORKSPACE_INSTANCE']
    def docker(*args):
        return subprocess.check_output(['docker', *args], text=True)
    ids = docker('ps', '-q', '--filter', f'label=educloud.workspace.instance={instance}',
                 '--filter', 'label=educloud.workspace.kind=learner').split()
    assert len(ids) == len(users), 'unexpected number of synthetic learner containers'
    containers = json.loads(docker('inspect', *ids))
    by_user = {}
    for username in users:
        home = instance + '-home-' + hashlib.sha256(username.encode()).hexdigest()[:24]
        by_user[username] = next(c for c in containers if any(m.get('Name') == home for m in c['Mounts']))
    for c in containers:
        h = c['HostConfig']
        assert c['Config']['User'] == '1000:1000'
        assert h['ReadonlyRootfs'] and not h['Privileged'] and h['PidsLimit'] == 128
        assert h['Memory'] == int(env.get('WORKSPACE_MEMORY_MB', '2048')) * 1024 * 1024
        assert h['MemorySwap'] == h['Memory']
        assert h['CpuQuota'] / h['CpuPeriod'] == float(env.get('WORKSPACE_CPU_LIMIT', '1'))
        assert 'ALL' in h['CapDrop'] and 'no-new-privileges:true' in h['SecurityOpt']
        assert len(c['Mounts']) == 1 and c['Mounts'][0]['Destination'] == '/home/learner'
        networks = c['NetworkSettings']['Networks']
        assert len(networks) == 1
        assert json.loads(docker('network', 'inspect', next(iter(networks))))[0]['Internal']
    alice = by_user['alice']
    peer = '1.1.1.1'
    if 'bob' in by_user:
        bob = by_user['bob']
        assert set(alice['NetworkSettings']['Networks']).isdisjoint(bob['NetworkSettings']['Networks'])
        peer = next(iter(bob['NetworkSettings']['Networks'].values()))['IPAddress']
    probe = '''import socket, sys
for host, port in [(sys.argv[1], 8888), ('1.1.1.1', 443)]:
    try:
        sock = socket.create_connection((host, port), timeout=2)
    except OSError:
        continue
    sock.close()
    raise SystemExit('unexpected network access')
'''
    docker('exec', alice['Id'], 'python', '-c', probe, peer)
    print('Runtime limits and Internet egress denial verified' +
          ('; separate networks and peer denial verified' if 'bob' in by_user else ''), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--restored', action='store_true')
    args = parser.parse_args(); env = config(args.env_file)
    assert env.get('WORKSPACE_AUTH_MODE') == 'local-test', 'never run synthetic smoke against production auth'
    base = env['WORKSPACE_PUBLIC_URL']; assert urlsplit(base).hostname == '127.0.0.1'
    sessions = {u:login(base,u,env['WORKSPACE_TEST_PASSWORD']) for u in ('alice','bob')}
    runtime_policy(env)
    for username, session in sessions.items():
        url = f'{base}/user/{username}/api/contents/continuity.txt'
        marker = 'synthetic-private-work-' + username
        if not args.restored:
            change(session, 'PUT', url, json={'type':'file','format':'text','content':marker}).raise_for_status()
        response = session.get(url,timeout=30);response.raise_for_status()
        assert response.json()['content'] == marker
        print(f'{username}: saved file verified' + (' after recovery' if args.restored else ''), flush=True)
    response = sessions['bob'].get(base + '/user/alice/api/contents/continuity.txt', timeout=30)
    assert response.status_code in (403,404), f'cross-user request unexpectedly returned {response.status_code}'
    print('bob cannot read alice workspace',flush=True)
    kernel(sessions['alice'],base,'alice','python3',"import pandas as pd; assert pd.Series([2,3]).sum()==5; print('CONTINUITY_OK')")
    kernel(sessions['alice'],base,'alice','ir',"library(ggplot2); stopifnot(sum(c(2,3))==5); cat('CONTINUITY_OK\\n')")
    for username,session in sessions.items():
        response=change(session,'DELETE',f'{base}/hub/api/users/{username}/server')
        response.raise_for_status()
    print('Learner servers stopped through authenticated Hub API; home volumes retained',flush=True)

if __name__ == '__main__':main()
