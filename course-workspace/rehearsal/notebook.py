"""Real Keycloak/Hub TLS rehearsal on a disposable Linux XFS mount."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

import requests
import websocket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backup import docker
from homes import provision, inspect, run as xfs_run
from public_course import render
from smoke import change
from verify import cleanup
from cairn_journey import run, port, SOLUTION

KEYCLOAK_IMAGE = 'quay.io/keycloak/keycloak:26.8.0@sha256:b0f60d489d51c5d113390bdf5461d4c06e6051be026c05549f2e1e10ec352bcc'
NGINX_IMAGE = 'nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
SUBJECTS = {user: str(uuid.uuid5(uuid.NAMESPACE_DNS, 'educloud-synthetic-' + user)) for user in ('alice', 'bob', 'mallory')}


class Notebook:
    def __init__(self, root):
        self.root = root
        self.instance = 'rehearsal-' + uuid.uuid4().hex[:10]
        self.port = port()
        self.origin = f'https://work.rehearsal.test:{self.port}'
        self.auth = f'https://auth.rehearsal.test:{self.port}'
        self.password = secrets.token_urlsafe(32)
        self.image = 'educloud-python-r:2026-10-06'
        self.mounted = False
        self.network = self.instance + '-fixture'
        self.original_dns = socket.getaddrinfo
        self.report = []
        self.revoked = None

    def compose(self, *args):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('WORKSPACE_', 'COMPOSE_'))}
        return run('docker', 'compose', '--env-file', str(self.root / '.env'), '-f', str(ROOT / 'compose.yaml'),
                   '-f', str(ROOT / 'quota-compose.yaml'), '-f', str(self.root / 'override.json'), *args,
                   cwd=ROOT, env=env)

    def write_env(self):
        (self.root / '.env').write_text(''.join(f'{k}={v}\n' for k,v in self.env.items()))
        (self.root / '.env').chmod(0o600)

    def start(self):
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise ValueError('full notebook rehearsal needs root on disposable Linux for real XFS quotas')
        # Only this newly created loop-image is formatted; never a caller-provided device.
        disk = self.root / 'synthetic.img'
        with disk.open('xb') as out:
            out.truncate(2 * 1024**3)
        self.homes = self.root / 'homes'
        self.homes.mkdir()
        xfs_run('mkfs.xfs', '-f', str(disk))
        xfs_run('mount', '-o', 'loop,prjquota', str(disk), str(self.homes))
        self.mounted = True
        nginx, client, self.env = render('work.rehearsal.test', self.auth + '/realms/course', 'course', self.instance,
                                        [SUBJECTS['alice'], SUBJECTS['bob']], str(self.homes), port())
        self.env.update(WORKSPACE_PUBLIC_URL=self.origin, WORKSPACE_OIDC_CLIENT_SECRET=secrets.token_urlsafe(32),
                        WORKSPACE_HOME_QUOTA_MB='256', WORKSPACE_MEMORY_MB='1024', WORKSPACE_ACTIVE_LIMIT='2',
                        WORKSPACE_ENV_FILE=str(self.root / '.env'))
        self.write_env()
        provision(self.homes, self.instance, [SUBJECTS['alice'], SUBJECTS['bob']], 256)
        # A private fixture CA is trusted explicitly by Hub and HTTP clients.
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=EduCloud synthetic CA',
            '-keyout', str(self.root/'ca.key'), '-out', str(self.root/'ca.pem'))
        run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=work.rehearsal.test',
            '-keyout', str(self.root/'privkey.pem'), '-out', str(self.root/'server.csr'))
        (self.root/'extensions').write_text('subjectAltName=DNS:work.rehearsal.test,DNS:*.work.rehearsal.test,DNS:auth.rehearsal.test\nextendedKeyUsage=serverAuth\n')
        run('openssl', 'x509', '-req', '-in', str(self.root/'server.csr'), '-CA', str(self.root/'ca.pem'),
            '-CAkey', str(self.root/'ca.key'), '-CAcreateserial', '-days', '1', '-extfile', str(self.root/'extensions'),
            '-out', str(self.root/'fullchain.pem'))
        for name in ('ca.key','privkey.pem'):
            (self.root/name).chmod(0o600)
        client.update(secret=self.env['WORKSPACE_OIDC_CLIENT_SECRET'], redirectUris=[self.origin+'/hub/oauth_callback'], webOrigins=[self.origin])
        realm = {'realm':'course', 'enabled':True, 'sslRequired':'all', 'registrationAllowed':False,
                 'clients':[client], 'users':[{'id': subject, 'username': user, 'enabled':True,
                 'email':user+'@synthetic.invalid', 'emailVerified':True, 'firstName':'Synthetic', 'lastName':user,
                 'credentials':[{'type':'password','value':self.password,'temporary':False}]} for user,subject in SUBJECTS.items()]}
        (self.root/'realm.json').write_text(json.dumps(realm))
        # Keycloak runs as UID 1000 and must read its disposable import file.
        (self.root/'realm.json').chmod(0o644)
        run('docker','network','create','--label','educloud.rehearsal='+self.instance,self.network)
        run('docker','run','-d','--name',self.instance+'-identity','--network',self.network,'--network-alias','identity',
            '--memory','1g','--cpus','1','-v',str(self.root/'realm.json')+':/opt/keycloak/data/import/course.json:ro',
            '-e','KC_HOSTNAME='+self.auth,'-e','KC_PROXY_HEADERS=xforwarded',KEYCLOAK_IMAGE,'start-dev','--import-realm')
        # Production generator routes are reused; only fixture ports/upstream change.
        nginx = nginx.replace('listen 443',f'listen {self.port}').replace('http://127.0.0.1:'+self.env['WORKSPACE_PORT'], f'http://{self.instance}-hub:8000')
        nginx += f'''\nserver {{ listen {self.port} ssl; server_name auth.rehearsal.test;
ssl_certificate /etc/educloud/tls/fullchain.pem; ssl_certificate_key /etc/educloud/tls/privkey.pem;
location / {{ proxy_pass http://identity:8080; proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Proto https; proxy_set_header X-Forwarded-Host $http_host;
proxy_set_header X-Forwarded-Port {self.port}; proxy_set_header X-Forwarded-For $remote_addr; }} }}\n'''
        (self.root/'nginx.conf').write_text('events {}\nhttp { access_log off;\n'+nginx+'\n}\n')
        override = {'networks': {'default':{'external':True,'name':self.network}},'services':{'hub':{
            'environment':{'SSL_CERT_FILE':'/fixture-ca.pem','REQUESTS_CA_BUNDLE':'/fixture-ca.pem'},
            'volumes':[str(self.root/'ca.pem')+':/fixture-ca.pem:ro']}}}
        (self.root/'override.json').write_text(json.dumps(override))
        print('Building the current notebook images.',flush=True)
        self.compose('--profile','build','build')
        self.compose('up','-d','hub')
        run('docker','run','-d','--name',self.instance+'-proxy','--network',self.network,
            '--network-alias','auth.rehearsal.test','--memory','128m','-p',f'127.0.0.1:{self.port}:{self.port}',
            '-v',str(self.root/'nginx.conf')+':/etc/nginx/nginx.conf:ro',
            '-v',str(self.root)+':/etc/educloud/tls:ro',NGINX_IMAGE)
        def resolve(host, *args, **kwargs):
            return self.original_dns('127.0.0.1' if host.endswith('.rehearsal.test') else host, *args, **kwargs)
        socket.getaddrinfo = resolve
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = str(self.root/'ca.pem')
        for _ in range(120):
            try:
                discovery = self.session.get(self.auth+'/realms/course/.well-known/openid-configuration',timeout=2)
                if discovery.status_code==200 and self.session.get(self.origin+'/hub/login',timeout=2).status_code==200:
                    assert discovery.json()['issuer'] == self.auth+'/realms/course'
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise AssertionError('OIDC fixture did not become ready')
        inspect(self.homes,self.instance)
        print('Keycloak discovery, TLS and Hub are ready.',flush=True)

    def login(self, page, user):
        page.goto(self.origin+'/hub/login')
        page.get_by_role('button', name='Sign in with').click()
        page.locator('input[name="username"]').fill(user)
        page.locator('input[name="password"]').fill(self.password)
        page.locator('input[type="submit"],button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).hostname != 'auth.rehearsal.test')

    def http_session(self, context):
        session = requests.Session()
        session.trust_env = False
        session.verify = str(self.root/'ca.pem')
        for cookie in context.cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
        return session

    def ready(self, page, user):
        subject = SUBJECTS[user]
        page.goto(self.origin+'/hub/spawn/'+subject)
        for _ in range(60):
            session = self.http_session(page.context)
            response = session.get(self.origin+'/hub/api/users/'+subject, timeout=10)
            if response.ok and response.json().get('server'):
                page.goto(self.origin+'/user/'+subject+'/lab')
                page.wait_for_url(lambda u: '.work.rehearsal.test' in u)
                url = page.url.split('/lab')[0]
                session = self.http_session(page.context)
                if session.get(url+'/api/contents',timeout=10).status_code==200:
                    return session, url
            time.sleep(1)
        raise AssertionError('notebook did not become ready')

    def kernel(self, session, url, name, code):
        response = change(session,'POST',url+'/api/kernels',json={'name':name})
        response.raise_for_status()
        kid=response.json()['id']
        endpoint=url+'/api/kernels/'+kid+'/channels'
        cookie=session.prepare_request(requests.Request('GET',endpoint)).headers.get('Cookie','')
        conn=websocket.create_connection(endpoint.replace('https://','wss://'),cookie=cookie,
            origin=url.split('/user/')[0],timeout=40,sslopt={'ca_certs':str(self.root/'ca.pem')},http_no_proxy=['*'])
        msgid=uuid.uuid4().hex
        conn.send(json.dumps({'header':{'msg_id':msgid,'username':'synthetic','session':uuid.uuid4().hex,'msg_type':'execute_request','version':'5.3'},
            'parent_header':{},'metadata':{},'channel':'shell','content':{'code':code,'silent':False,'store_history':False,'user_expressions':{},'allow_stdin':False}}))
        output=''; passed=False; replied=False; idle=False
        try:
            while not (replied and idle):
                msg=json.loads(conn.recv())
                if msg.get('parent_header',{}).get('msg_id')!=msgid: continue
                if msg['msg_type']=='stream': output+=msg['content']['text']
                if msg['msg_type']=='execute_reply':
                    replied=True
                    passed=msg['content']['status']=='ok'
                if msg['msg_type']=='status' and msg['content']['execution_state']=='idle': idle=True
        finally:
            conn.close()
            change(session,'DELETE',url+'/api/kernels/'+kid).raise_for_status()
        assert passed and 'COURSE_OK' in output, 'kernel did not return a successful marker'

    def handoff(self, context, page, link):
        inspect(self.homes,self.instance)
        with context.expect_page() as opened:
            link.click()
        notebook=opened.value
        self.login(notebook,'alice')
        session,url=self.ready(notebook,'alice')
        self.kernel(session,url,'python3', 'from pathlib import Path\nPath("solution.py").write_text('+repr(SOLUTION)+')\nprint("COURSE_OK")')
        self.kernel(session,url,'ir','library(ggplot2); stopifnot(sum(c(2,3)) == 5); cat("COURSE_OK\\n")')
        response=session.get(url+'/api/contents/solution.py',timeout=20)
        response.raise_for_status()
        solution=response.json()['content']
        assert solution==SOLUTION
        # The protocol checks above do not prove that the browser finished
        # loading its file list. Open the saved file visibly before acceptance.
        notebook.reload()
        notebook.get_by_text('solution.py',exact=True).first.wait_for(state='visible',timeout=60000)
        if notebook.get_by_role('button',name='No',exact=True).is_visible():
            notebook.get_by_role('button',name='No',exact=True).click()
        notebook.get_by_text('solution.py',exact=True).first.dblclick()
        notebook.locator('.cm-content').filter(has_text='def answer').wait_for(state='visible',timeout=30000)
        notebook.screenshot(path=str(self.root/'notebook.png'))
        self.revoked=(context,notebook,session,url)
        self.report += ['keycloak-pkce-login','per-user-https','python-r-wss','saved-file-handoff','saved-file-visible-in-browser']
        return solution

    def revocation(self, browser):
        context, page, session, url=self.revoked
        denied=browser.new_context(ignore_https_errors=True)
        other=denied.new_page()
        self.login(other,'mallory')
        assert '403' in other.text_content('body') or 'not allowed' in other.text_content('body').lower()
        bob_context=browser.new_context(ignore_https_errors=True)
        bob_page=bob_context.new_page()
        inspect(self.homes,self.instance)
        self.login(bob_page,'bob')
        bob_session,bob_url=self.ready(bob_page,'bob')
        assert bob_url != url
        peer=bob_session.get(url+'/api/contents/solution.py',timeout=15)
        assert peer.status_code in (403,404), f'cross-user file access returned {peer.status_code}'
        bob_context.close()
        self.report.append('authenticated-peer-file-denied')
        self.env['WORKSPACE_ALLOWED_SUBJECTS']=SUBJECTS['bob']
        self.write_env()
        self.compose('stop','hub')  # Graceful stop terminates active learners.
        self.compose('up','-d','--force-recreate','hub')
        inspect(self.homes,self.instance)
        for _ in range(60):
            try:
                if self.session.get(self.origin+'/hub/login',timeout=2).status_code==200: break
            except requests.RequestException: pass
            time.sleep(1)
        # Old authenticated notebook cookie cannot access retained files after removal.
        response=session.get(url+'/api/contents/solution.py',timeout=15,allow_redirects=False)
        assert response.status_code in (302,303,403,404,503), response.status_code
        page.goto(self.origin+'/hub/spawn/'+SUBJECTS['alice'])
        # Spawn uses an asynchronous progress page; wait for the roster denial,
        # not just the initial successful HTTP response for that progress page.
        for _ in range(30):
            message=' '.join(page.locator('body').inner_text().lower().split())
            if any(reason in message for reason in ('403', 'not allowed', 'not in this course roster')):
                break
            time.sleep(1)
        else:
            raise AssertionError('respawn did not reach roster denial: '+message[:600])
        ids=docker('ps','-q','--filter','label=educloud.workspace.instance='+self.instance,
                   '--filter','label=educloud.workspace.kind=learner')
        assert not ids, 'removed learner still has an active server'
        home = self.instance + '-home-' + hashlib.sha256(SUBJECTS['alice'].encode()).hexdigest()[:24]
        assert (self.homes/home/'work/solution.py').read_text() == SOLUTION, 'revocation must retain saved work'
        denied.close()
        self.report += ['unrostered-login-denied','active-server-stopped','old-session-denied','removed-roster-respawn-denied','revocation-retains-files']
        (self.root/'identity-PASS.json').write_text(json.dumps({'checks':self.report},indent=2)+'\n')
        print('PASS: OIDC allowed/denied identities, TLS notebook handoff, active-session roster revocation.',flush=True)

    def close(self):
        socket.getaddrinfo=self.original_dns
        for suffix in ('-proxy','-identity'):
            subprocess.run(['docker','rm','-f',self.instance+suffix],capture_output=True)
        if hasattr(self,'env'):
            cleanup(self.instance,self.root/'.env')
        subprocess.run(['docker','network','rm',self.network],capture_output=True)
        if self.mounted:
            xfs_run('umount',str(self.homes))
