"""Real Keycloak/Hub TLS rehearsal on a disposable Linux XFS mount."""
from contextlib import nullcontext
import copy
import hashlib
import json
import os
import re
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
from deployment import read_environment, preflight
from course_manifest import render_manifest, validate_manifest
from smoke import change
from cairn_journey import run, port, SOLUTION

KEYCLOAK_IMAGE = 'quay.io/keycloak/keycloak:26.8.0@sha256:b0f60d489d51c5d113390bdf5461d4c06e6051be026c05549f2e1e10ec352bcc'
NGINX_IMAGE = 'nginx:1.28-alpine@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236'
SUBJECTS = {user: str(uuid.uuid5(uuid.NAMESPACE_DNS, 'educloud-synthetic-' + user)) for user in ('alice', 'bob', 'carol', 'mallory')}


class Notebook:
    def __init__(self, root, extra_users=(), *, hub_image=None, course_image=None, synthetic_candidate_images=False,
                 domain='work.rehearsal.test', auth_domain='auth.rehearsal.test', client_id='course',
                 capacity=None, allocation_policy=None):
        if bool(hub_image) != bool(course_image) or (synthetic_candidate_images and hub_image):
            raise ValueError('supply paired immutable image references or synthetic candidate images')
        for reference in (hub_image, course_image):
            if reference is not None and not re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._:/-]{0,254}@sha256:[0-9a-f]{64})', reference):
                raise ValueError('preloaded candidate images require immutable references')
        for hostname in (domain, auth_domain):
            if not re.fullmatch(r'[a-z][a-z0-9-]*\.rehearsal\.test', hostname):
                raise ValueError('synthetic notebook hosts must be distinct rehearsal DNS names')
        if domain == auth_domain or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', client_id):
            raise ValueError('synthetic notebook identity hosts and client ID invalid')
        default_capacity = {'memory_mb':1024, 'cpu':1, 'active_limit':2, 'idle_seconds':1800, 'max_age_seconds':28800}
        if capacity is not None and (not isinstance(capacity, dict) or set(capacity)-set(default_capacity)):
            raise ValueError('synthetic capacity overrides must use manifest capacity fields')
        self.capacity = dict(default_capacity, **(capacity or {}))
        self.domain, self.auth_domain, self.client_id = domain, auth_domain, client_id
        self.allocation_policy = allocation_policy
        self.requested_hub_image = hub_image
        self.synthetic_candidate_images = synthetic_candidate_images
        self.users = ("alice", "bob", *extra_users)
        self.load_report = {}
        self.root = root
        self.instance = 'rehearsal-' + uuid.uuid4().hex[:10]
        self.port = port()
        self.origin = f'https://{self.domain}:{self.port}'
        self.auth = f'https://{self.auth_domain}:{self.port}'
        self.password = secrets.token_urlsafe(32)
        self.image = course_image or 'educloud-python-r:2026-10-06'
        self.mounted = False
        self.network = self.instance + '-fixture'
        self.original_dns = socket.getaddrinfo
        self.report = []
        self.revoked = None
        self.cleanup_verified = False
        self.cairn_origin = None
        self.deployment = self.root / 'deployment'
        self.env_file = self.root / 'bootstrap.env'
        self.manifest_path = None
        self.manifest_digest = None

    def compose(self, *args):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('WORKSPACE_', 'COMPOSE_'))}
        context = nullcontext()
        if self.allocation_policy is not None and args and args[0] in ('up','start','restart','run','create'):
            import allocation
            context = allocation.admission(self.deployment, read_environment(self.env_file))
        with context:
            return run('docker', 'compose', '--env-file', str(self.env_file), '-f', str(ROOT / 'compose.yaml'),
                       '-f', str(ROOT / 'quota-compose.yaml'), '-f', str(self.root / 'override.json'), *args,
                       cwd=ROOT, env=env)

    def write_env(self):
        self.env_file.write_text(''.join(f'{k}={v}\n' for k,v in self.env.items()))
        self.env_file.chmod(0o600)

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
        _, _, self.env = render(self.domain, self.auth + '/realms/course', self.client_id, self.instance,
                                        [SUBJECTS[user] for user in self.users], str(self.homes), port())
        self.env.update(WORKSPACE_PUBLIC_URL=self.origin, WORKSPACE_OIDC_CLIENT_SECRET=secrets.token_urlsafe(32),
                        WORKSPACE_HOME_QUOTA_MB='256', WORKSPACE_MEMORY_MB=str(self.capacity['memory_mb']),
                        WORKSPACE_CPU_LIMIT=str(self.capacity['cpu']), WORKSPACE_ACTIVE_LIMIT=str(self.capacity['active_limit']),
                        WORKSPACE_ENV_FILE=str(self.env_file))
        self.env['WORKSPACE_IMAGE'] = self.image
        if self.requested_hub_image:
            self.env['WORKSPACE_HUB_IMAGE'] = self.requested_hub_image
        self.write_env()
        provision(self.homes, self.instance, [SUBJECTS[user] for user in self.users], 256)
        # A private fixture CA is trusted explicitly by Hub and HTTP clients.
        run('openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-subj', '/CN=EduCloud synthetic CA',
            '-keyout', str(self.root/'ca.key'), '-out', str(self.root/'ca.pem'))
        run('openssl', 'req', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN='+self.domain,
            '-keyout', str(self.root/'privkey.pem'), '-out', str(self.root/'server.csr'))
        (self.root/'extensions').write_text('subjectAltName=DNS:'+self.domain+',DNS:*.'+self.domain+',DNS:'+self.auth_domain+'\nextendedKeyUsage=serverAuth\n')
        run('openssl', 'x509', '-req', '-in', str(self.root/'server.csr'), '-CA', str(self.root/'ca.pem'),
            '-CAkey', str(self.root/'ca.key'), '-CAcreateserial', '-days', '1', '-extfile', str(self.root/'extensions'),
            '-out', str(self.root/'fullchain.pem'))
        for name in ('ca.key','privkey.pem'):
            (self.root/name).chmod(0o600)
        run('docker','network','create','--label','educloud.rehearsal='+self.instance,self.network)
        override = {'networks': {'default':{'external':True,'name':self.network}},'services':{'hub':{
            'environment':{'SSL_CERT_FILE':'/fixture-ca.pem','REQUESTS_CA_BUNDLE':'/fixture-ca.pem'},
            'volumes':[str(self.root/'ca.pem')+':/fixture-ca.pem:ro']}}}
        (self.root/'override.json').write_text(json.dumps(override))
        self.prepare_images()
        self.render_setup()
        if self.allocation_policy is not None:
            import allocation
            allocation.enroll(self.deployment, self.allocation_policy)
        self.verify_startup_preflight(self.deployment)
        self.configure_fixture_edges()
        self.compose('up','-d','--no-build','--pull','never','hub')
        run('docker','run','-d','--name',self.instance+'-proxy','--label','educloud.rehearsal='+self.instance,'--network',self.network,
            '--network-alias',self.auth_domain,'--memory','128m','-p',f'127.0.0.1:{self.port}:{self.port}',
            *(['--cpus','0.1'] if self.allocation_policy is not None else []),
            '-v',str(self.root/'nginx.conf')+':/etc/nginx/nginx.conf:ro',
            '-v',str(self.root/'fullchain.pem')+':/etc/educloud/tls/fullchain.pem:ro',
            '-v',str(self.root/'privkey.pem')+':/etc/educloud/tls/privkey.pem:ro',NGINX_IMAGE)
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

    def prepare_images(self):
        if not self.requested_hub_image:
            print('Building the current notebook images.', flush=True)
            self.compose('--profile', 'build', 'build')
        else:
            print('Using the preloaded immutable notebook images.', flush=True)

    def configure_fixture_edges(self):
        # The operator-rendered client drives the real fixture realm. The sole
        # secret is inserted privately into the disposable Keycloak import.
        client = json.loads((self.deployment / 'keycloak-client.json').read_text())
        client['secret'] = self.env['WORKSPACE_OIDC_CLIENT_SECRET']
        realm = {'realm':'course', 'enabled':True, 'sslRequired':'all', 'registrationAllowed':False,
                 'clients':[client], 'users':[{'id': subject, 'username': user, 'enabled':True,
                 'email':user+'@synthetic.invalid', 'emailVerified':True, 'firstName':'Synthetic', 'lastName':user,
                 'credentials':[{'type':'password','value':self.password,'temporary':False}]} for user,subject in SUBJECTS.items()]}
        (self.root/'realm.json').write_text(json.dumps(realm))
        # Keycloak runs as UID 1000 and must read its disposable import file.
        # Its parent fixture directory remains private and is never published.
        (self.root/'realm.json').chmod(0o644)
        run('docker','run','-d','--name',self.instance+'-identity','--label','educloud.rehearsal='+self.instance,'--network',self.network,'--network-alias','identity',
            '--memory','1g','--cpus','0.25' if self.allocation_policy is not None else '1','-v',str(self.root/'realm.json')+':/opt/keycloak/data/import/course.json:ro',
            '-e','KC_HOSTNAME='+self.auth,'-e','KC_PROXY_HEADERS=xforwarded',KEYCLOAK_IMAGE,'start-dev','--import-realm')
        # Keep the generated TLS/DNS policy. Only the private Compose-network
        # upstream and separate synthetic IdP virtual host are fixture adapters.
        nginx = (self.deployment / 'nginx.conf').read_text()
        nginx = nginx.replace('http://127.0.0.1:'+self.env['WORKSPACE_PORT'], f'http://{self.instance}-hub:8000')
        nginx += f'''\nserver {{ listen {self.port} ssl; server_name {self.auth_domain};
ssl_certificate /etc/educloud/tls/fullchain.pem; ssl_certificate_key /etc/educloud/tls/privkey.pem;
location / {{ proxy_pass http://identity:8080; proxy_set_header Host $http_host;
proxy_set_header X-Forwarded-Proto https; proxy_set_header X-Forwarded-Host $http_host;
proxy_set_header X-Forwarded-Port {self.port}; proxy_set_header X-Forwarded-For $remote_addr; }} }}\n'''
        (self.root/'nginx.conf').write_text('events {}\nhttp { access_log off;\n'+nginx+'\n}\n')
        self.report += ['course-manifest-identity-consumed', 'course-manifest-proxy-consumed']

    def render_setup(self):
        """Consume the same strict manifest/rendering path used by operators."""
        self.hub_image = run('docker', 'image', 'inspect', self.requested_hub_image or self.instance + '-hub:pilot', '--format', '{{.Id}}')
        self.image = run('docker', 'image', 'inspect', self.image, '--format', '{{.Id}}')
        if self.synthetic_candidate_images:
            self.hub_image = self.build_synthetic_candidate('hub', self.hub_image)
            self.image = self.build_synthetic_candidate('course', self.image)
        secret_file = self.root / 'oidc-secret'
        secret_file.write_text(self.env['WORKSPACE_OIDC_CLIENT_SECRET'])
        secret_file.chmod(0o600)
        backup_file = self.root / 'backup-secret'
        backup_file.write_text(secrets.token_urlsafe(32))
        backup_file.chmod(0o600)
        self.manifest = {
            'version': 1, 'scope': 'synthetic', 'instance': self.instance,
            'images': {'hub': self.hub_image, 'course': self.image},
            'identity': {'issuer': self.auth + '/realms/course', 'client_id': self.client_id,
                         'client_secret_file': str(secret_file),
                         'subjects': [SUBJECTS[user] for user in self.users]},
            'workspace': {'origin': self.origin, 'port': int(self.env['WORKSPACE_PORT'])},
            'storage': {'home_root': str(self.homes), 'quota_mb': 256, 'inode_limit': 100000},
            'capacity': dict(self.capacity),
            'backup': {'repository': str(self.root / 'encrypted-repository'),
                       'password_file': str(backup_file), 'staging_dir': str(self.deployment),
                       'keep_last': 7, 'keep_daily': 7, 'keep_weekly': 4},
            'cairn': {'enabled': False},
            'owners': {'support': 'Synthetic rehearsal operator', 'restore': 'Synthetic rehearsal operator'},
        }
        render_manifest(self.manifest, self.deployment)
        self.env_file = self.deployment / '.env'
        self.env = read_environment(self.env_file)
        self.freeze_manifest()
        self.report.append('course-manifest-rendered')

    def build_synthetic_candidate(self, role, base_image):
        """Derive distinct local canary pins without changing executable bytes."""
        if role not in ('hub', 'course') or not re.fullmatch(r'sha256:[0-9a-f]{64}', base_image):
            raise ValueError('synthetic candidates require verified local image identities')
        directory = self.root / ('candidate-' + role)
        directory.mkdir(mode=0o700)
        (directory / 'Dockerfile').write_text('FROM ' + base_image + '\nLABEL educloud.upgrade.candidate=synthetic-v1\n')
        reference = self.instance + '-' + role + ':synthetic-canary'
        # Classic Docker accepts a local immutable image ID in FROM without
        # resolving it as a registry name. This fixture has no RUN/network step.
        env = dict(os.environ, DOCKER_BUILDKIT='0')
        run('docker', 'build', '--pull=false', '--network=none', '--tag', reference, str(directory), env=env)
        identity = run('docker', 'image', 'inspect', reference, '--format', '{{.Id}}')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', identity) or identity == base_image:
            raise AssertionError('synthetic candidate must have a distinct immutable image ID')
        return identity

    def verify_startup_preflight(self, directory):
        result = preflight(directory)
        if result.get('status') != 'ready' or result.get('images') != {'hub': self.hub_image, 'course': self.image}:
            raise AssertionError('manifest startup preflight did not verify configured images')
        if 'course-manifest-startup-preflight' not in self.report:
            self.report.append('course-manifest-startup-preflight')

    def freeze_manifest(self):
        self.manifest_path = self.deployment / 'manifest.json'
        self.manifest_digest = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()

    def verify_manifest_frozen(self):
        if (not self.manifest_path or self.manifest_path.is_symlink()
                or hashlib.sha256(self.manifest_path.read_bytes()).hexdigest() != self.manifest_digest):
            raise AssertionError('consumed course manifest changed during rehearsal')

    def bind_classroom(self, classroom_id, workspace_url):
        """Render actual classroom binding before the first learner starts."""
        self.verify_manifest_frozen()
        if not self.cairn_origin or workspace_url != self.origin:
            raise AssertionError('Cairn binding does not identify this workspace')
        manifest = copy.deepcopy(self.manifest)
        manifest['cairn'] = {'enabled': True, 'origin': self.cairn_origin,
                             'classroom_id': classroom_id, 'workspace_url': workspace_url,
                             'max_files': 32, 'max_file_bytes': 1048576, 'max_total_bytes': 4194304}
        if validate_manifest(manifest):
            raise AssertionError('actual Cairn binding failed course manifest validation')
        destination = self.root / 'bound-deployment'
        manifest['backup']['staging_dir'] = str(destination)
        render_manifest(manifest, destination)
        for name in ('nginx.conf', 'keycloak-client.json'):
            if (destination / name).read_bytes() != (self.deployment / name).read_bytes():
                raise AssertionError('classroom binding changed already-consumed identity or proxy policy')
        binding = json.loads((destination / 'cairn-binding.json').read_text())
        if binding.get('classroom_id') != classroom_id or binding.get('workspace_url') != workspace_url:
            raise AssertionError('rendered Cairn binding differs from the actual classroom')
        operator_env = binding.get('operator_environment')
        if operator_env != {'CAIRN_SUBMISSIONS': 'true', 'CAIRN_SUBMISSION_CLASSROOMS': classroom_id}:
            raise AssertionError('rendered Cairn approval differs from the actual classroom')
        self.verify_startup_preflight(destination)
        self.compose('stop', 'hub')
        self.manifest = manifest
        self.deployment = destination
        self.env_file = destination / '.env'
        self.env = read_environment(self.env_file)
        self.freeze_manifest()
        self.compose('up', '-d', '--force-recreate', '--no-build', '--pull', 'never', 'hub')
        for _ in range(60):
            try:
                if self.session.get(self.origin + '/hub/login', timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        else:
            raise AssertionError('manifest-bound Hub did not become ready')
        self.report.append('course-manifest-cairn-bound')
        return operator_env

    def verify_runtime_images(self):
        # Unit navigation controls do not construct a deployment. Every actual
        # rehearsal does, and must inspect both started Hub and learner bytes.
        if self.manifest_digest is None:
            return
        hub = json.loads(docker('inspect', self.instance + '-hub'))[0]
        if hub.get('Image') != self.hub_image:
            raise AssertionError('Hub did not consume the pinned manifest image')
        identities = docker('ps', '-q', '--filter', 'label=educloud.workspace.instance=' + self.instance,
                            '--filter', 'label=educloud.workspace.kind=learner').split()
        if not identities or any(json.loads(docker('inspect', identity))[0].get('Image') != self.image
                                 for identity in identities):
            raise AssertionError('learner did not consume the pinned manifest image')
        if 'course-manifest-images-consumed' not in self.report:
            self.report.append('course-manifest-images-consumed')

    def login(self, page, user):
        page.goto(self.origin+'/hub/login')
        page.get_by_role('button', name='Sign in with').click()
        page.locator('input[name="username"]').fill(user)
        page.locator('input[name="password"]').fill(self.password)
        page.locator('input[type="submit"],button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).hostname != self.auth_domain)

    def http_session(self, context):
        session = requests.Session()
        session.trust_env = False
        session.verify = str(self.root/'ca.pem')
        for cookie in context.cookies():
            session.cookies.set(cookie['name'], cookie['value'], domain=cookie['domain'], path=cookie['path'])
        return session

    def ready(self, page, user):
        subject = SUBJECTS[user]
        # Synthetic subjects are UUIDs, which the pinned Hub preserves as DNS
        # labels. Require this learner's exact HTTPS origin and server path.
        origin = urlsplit(self.origin)
        expected = origin._replace(netloc=subject + '.' + origin.netloc,
                                   path='/user/' + subject + '/', query='', fragment='').geturl()
        page.goto(self.origin+'/hub/spawn/'+subject)
        for _ in range(60):
            session = self.http_session(page.context)
            response = session.get(self.origin+'/hub/api/users/'+subject, timeout=10)
            if response.ok and response.json().get('server'):
                if response.json()['server'] != expected:
                    raise AssertionError('Hub returned an unexpected learner server URL')
                # /hub/spawn already redirects when readiness completes. A
                # second goto races that navigation and can interrupt OAuth.
                page.wait_for_url(expected + 'lab', wait_until='domcontentloaded', timeout=60000)
                url = expected.rstrip('/')
                session = self.http_session(page.context)
                contents = session.get(url+'/api/contents', timeout=10, allow_redirects=False)
                if (contents.status_code == 200 and 'application/json' in contents.headers.get('Content-Type', '')
                        and contents.json().get('type') == 'directory'):
                    self.verify_runtime_images()
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
        session,url=self.restart_saved_file(notebook,session,url)
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

    def restart_saved_file(self, page, session, url):
        """Replace Alice's only server and verify saved bytes through the new server."""
        def active():
            return set(run('docker','ps','-q','--no-trunc','--filter',
                           'label=educloud.workspace.instance='+self.instance,'--filter',
                           'label=educloud.workspace.kind=learner').split())
        before=active()
        if len(before)!=1:
            raise AssertionError('persistence fixture requires exactly one active learner')
        response=change(session,'DELETE',self.origin+'/hub/api/users/'+SUBJECTS['alice']+'/server')
        if response.status_code not in (202,204):
            raise AssertionError('notebook server stop was rejected')
        for _ in range(60):
            state=session.get(self.origin+'/hub/api/users/'+SUBJECTS['alice'],timeout=10)
            if state.status_code==200 and not state.json().get('server') and not active():
                break
            time.sleep(1)
        else:
            raise AssertionError('old notebook server did not stop')
        inspect(self.homes,self.instance)
        resumed,resumed_url=self.ready(page,'alice')
        after=active()
        if len(after)!=1 or before & after:
            raise AssertionError('resume did not create a new notebook container')
        saved=resumed.get(resumed_url+'/api/contents/solution.py',timeout=20)
        saved.raise_for_status()
        if saved.json().get('content')!=SOLUTION:
            raise AssertionError('saved submission changed across notebook stop/resume')
        self.kernel(resumed,resumed_url,'python3',
                    'from pathlib import Path\nassert Path("solution.py").read_text() == '+repr(SOLUTION)+'\nprint("COURSE_OK")')
        self.report += ['server-stop-resume','saved-file-after-server-restart']
        return resumed,resumed_url

    def submission_page(self, context, item):
        """Use the actual saved-file bridge in the authenticated notebook."""
        from playwright.sync_api import expect
        _, page, session, url = self.revoked
        configuration = session.get(url + '/cairn/config', timeout=15)
        assert configuration.status_code == 200, f'authenticated notebook handoff route returned {configuration.status_code}'
        assert configuration.json()['origin'] == self.cairn_origin
        # The application provides a discoverable link independent of home files.
        with page.expect_popup() as bridge:
            page.get_by_role('link', name='Submit saved files to Cairn', exact=True).click()
        page = bridge.value
        page.wait_for_url(url + '/cairn/')
        page.get_by_role('checkbox', name='solution.py', exact=False).check()
        page.get_by_role('button', name='Prepare saved files', exact=True).click()
        with context.expect_page() as opened:
            page.get_by_role('button', name='Continue in Cairn', exact=True).click()
        receiver = opened.value
        receiver.get_by_label('Assignment', exact=True).select_option(item['submission_id'])
        receiver.get_by_role('button', name='Receive saved files from Waypoint', exact=True).click()
        expect(receiver.locator('#bridge-status')).to_contain_text('Nothing has been submitted yet.')
        self.report += ['waypoint-selected-file-bridge', 'separate-cairn-confirmation']
        return receiver

    def bounded_load(self, browser, alice, alice_url, bob, bob_url):
        from concurrent.futures import ThreadPoolExecutor
        # Independent HTTP sessions per request; no browser object crosses threads.
        def request(n):
            original,url=(alice,alice_url) if n%2==0 else (bob,bob_url)
            with requests.Session() as client:
                client.trust_env=False; client.verify=str(self.root/'ca.pem')
                client.cookies.update(original.cookies)
                started=time.monotonic()
                response=client.get(url+'/api/contents',timeout=15)
                assert response.status_code==200, f'bounded notebook request: {response.status_code}'
                return time.monotonic()-started
        with ThreadPoolExecutor(max_workers=6) as pool:
            durations=sorted(pool.map(request,range(24)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(self.kernel,alice,alice_url,'python3','print("COURSE_OK")'),
                     pool.submit(self.kernel,bob,bob_url,'ir','cat("COURSE_OK\\n")')]
            for future in futures: future.result()
        context=browser.new_context(ignore_https_errors=True); page=context.new_page()
        self.login(page,'carol')
        carol=self.http_session(context)
        response=change(carol,'POST',self.origin+'/hub/api/users/'+SUBJECTS['carol']+'/server')
        assert response.status_code==429, f'capacity denial returned {response.status_code}'
        def active():
            return docker('ps','-q','--filter','label=educloud.workspace.instance='+self.instance,
                          '--filter','label=educloud.workspace.kind=learner').splitlines()
        assert len(active())==2
        response=change(bob,'DELETE',self.origin+'/hub/api/users/'+SUBJECTS['bob']+'/server')
        assert response.status_code in (202,204)
        for _ in range(60):
            if len(active())==1: break
            time.sleep(1)
        else: raise AssertionError('notebook slot did not free')
        inspect(self.homes,self.instance)
        carol,carol_url=self.ready(page,'carol')
        self.kernel(carol,carol_url,'python3','print("COURSE_OK")')
        assert len(active())==2
        assert alice.get(alice_url+'/api/contents/solution.py',timeout=15).json()['content']==SOLUTION
        self.load_report={'active_limit':2,'http_requests':24,'parallel_http_requests':6,
                          'http_p50_seconds':round(durations[12],4),'http_p95_seconds':round(durations[22],4),
                          'overflow_status':429,'freed_slot_reused':True,'simultaneous_python_r':True}
        self.report+=['bounded-notebook-traffic','active-limit-429','freed-slot-reused']
        context.close()

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
        if 'carol' in self.users:
            self.bounded_load(browser,session,url,bob_session,bob_url)
        bob_context.close()
        self.report.append('authenticated-peer-file-denied')
        self.env['WORKSPACE_ALLOWED_SUBJECTS']=SUBJECTS['bob']
        self.write_env()
        self.compose('stop','hub')  # Graceful stop terminates active learners.
        self.compose('up','-d','--force-recreate','--no-build','--pull','never','hub')
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
        (self.root/'identity-phase-evidence.json').write_text(json.dumps({'checks':self.report},indent=2)+'\n')
        print('Phase verified: OIDC allowed/denied identities, TLS notebook handoff, active-session roster revocation.',flush=True)

    def close(self):
        """Attempt every owned cleanup, then verify absence before reporting success."""
        self.cleanup_verified=False
        socket.getaddrinfo=self.original_dns
        failures=[]
        labels=('educloud.workspace.instance='+self.instance,
                'educloud.rehearsal='+self.instance,
                'com.docker.compose.project='+self.instance)

        def command(*args):
            try:
                result=subprocess.run(args,capture_output=True,text=True,timeout=30)
            except (OSError,subprocess.TimeoutExpired):
                failures.append('cleanup command unavailable or timed out')
                return None
            if result.returncode:
                failures.append('cleanup command failed')
                return None
            return result.stdout.strip()

        def inventory(kind):
            identities=set()
            for label in labels:
                args=('ps','-aq','--no-trunc') if kind=='container' else (kind,'ls','-q')
                output=command('docker',*args,'--filter','label='+label)
                if output is not None:
                    identities.update(output.split())
            return identities

        # Hub, learners and fixture proxy/IdP all carry invocation ownership.
        # Stop/remove containers first; never force-remove mounted volumes.
        for kind in ('container','volume','network'):
            for identity in sorted(inventory(kind)):
                args=('rm','--force',identity) if kind=='container' else (kind,'rm',identity)
                command('docker',*args)
        if hasattr(self,'homes') and (self.mounted or os.path.ismount(self.homes)):
            command('umount',str(self.homes))
        for kind in ('container','volume','network'):
            if inventory(kind):
                failures.append('owned '+kind+' remains')
        if hasattr(self,'homes') and os.path.ismount(self.homes):
            failures.append('synthetic filesystem remains mounted')
        else:
            self.mounted=False
        if hasattr(self,'session'):
            try:
                self.session.close()
            except Exception:
                failures.append('notebook HTTP session cleanup failed')
        if failures:
            raise RuntimeError('notebook cleanup unverified: '+'; '.join(sorted(set(failures))))
        self.cleanup_verified=True
        return True
