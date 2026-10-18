"""Owned, disposable synthetic OIDC stack. Never launches the general dev stack."""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import datetime as dt
import json
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import uuid

from research_workspace.runtime import Broker
from research_workspace.store import Store, canonical, private_write

HERE = Path(__file__).resolve().parent
PINS = {
    'keycloak': 'quay.io/keycloak/keycloak@sha256:0aae0de7fca85525f727d3354df17896092de8bb26ae4c12d89c77e5df8cbce4',
    'proxy': 'quay.io/oauth2-proxy/oauth2-proxy@sha256:10a1165743a192e1940b4708fb9647027185ce11a681a1c5519b442ff7f1f561',
    'traefik': 'traefik@sha256:aaf0f6185419a50c74651448c1a5bf4606bd2d2ddb7b8749eed505d55bf8b8ea',
    'docker': 'docker@sha256:9190b0613792e658a7783cf14b2d5ace5941bb68ede7276922ea36ee457d76ad',
    'python': 'python@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36',
}


def save(path, value, readable=False):
    private_write(path, (canonical(value) + '\n').encode())
    if readable:
        path.chmod(0o644)  # Parent is 0700; non-root container can read its exact bind mount.


def compose(root, *args, timeout=300):
    return subprocess.run(['docker', 'compose', '-f', str(root / 'compose.private.json'), *args],
                          capture_output=True, timeout=timeout, check=True)


def prepare(root, broker, linux_broker):
    root = root.resolve()
    if not root.is_relative_to(Path('/tmp').resolve()):
        raise ValueError('Use a fresh /tmp directory; no Documents, shared or production state.')
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    name = 'waypoint-oidc-' + secrets.token_hex(6)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    realm = 'synthetic-' + secrets.token_hex(6)
    issuer = origin + '/idp/realms/' + realm
    private = 'http://keycloak:8080/idp/realms/' + realm + '/protocol/openid-connect'
    client_secret, boundary = secrets.token_urlsafe(36), secrets.token_urlsafe(36)
    users = {role: {'sub': str(uuid.uuid4()), 'subject': 'r_' + secrets.token_hex(8),
                    'password': secrets.token_urlsafe(24)} for role in ('owner', 'editor', 'viewer', 'outsider')}
    cookies = {'app': 'research_' + secrets.token_hex(6), 'proxy': 'proxy_' + secrets.token_hex(6)}
    identity = {'issuer': issuer, 'audience': 'research-pilot', 'client_id': 'research-pilot',
                'client_secret': client_secret, 'boundary_secret': boundary, 'public_origin': origin,
                'proxy_auth_url': 'http://oauth2-proxy:4180/oauth2/auth',
                'introspection_url': private + '/token/introspect', 'subjects': {u['sub']: u['subject'] for u in users.values()},
                'cookie_name': cookies['app'], 'proxy_cookie_name': cookies['proxy']}
    state = root / 'state'
    store = Store(state, create=True)
    for user in users.values():
        store.add_user(user['subject'])  # Generated key is discarded; this mode never accepts it.
    config = {'version': 1, 'mode': 'local-synthetic-oidc', 'broker': '/opt/outfitter',
              'images': json.loads((HERE / 'images.lock.json').read_text()), 'total_budget_cpu_seconds': 600,
              'identity': identity}
    save(state / 'config.json', config)
    compute = Broker(broker, state / 'broker')
    projects = []
    for index, owner in enumerate(('owner', 'outsider')):
        project = store.create_project(users[owner]['subject'], {'name': f'Synthetic study {index+1}',
            'question': 'How does trimming change a descriptive summary?', 'success_criterion': 'Independently verified counts and means.',
            'ends_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(), 'budget_cpu_seconds': 300}, compute, 600)
        projects.append(project['id'])
    for role in ('editor', 'viewer'):
        store.membership(projects[0], users['owner']['subject'], users[role]['subject'], role)
    admin_password = secrets.token_urlsafe(32)
    realm_data = {'realm': realm, 'enabled': True, 'sslRequired': 'none', 'registrationAllowed': False,
        'resetPasswordAllowed': False, 'accessTokenLifespan': 600, 'ssoSessionIdleTimeout': 1200, 'ssoSessionMaxLifespan': 1800,
        'clients': [{'clientId': 'research-pilot', 'enabled': True, 'protocol': 'openid-connect',
            'publicClient': False, 'secret': client_secret, 'standardFlowEnabled': True,
            'directAccessGrantsEnabled': False, 'serviceAccountsEnabled': False, 'implicitFlowEnabled': False,
            'redirectUris': [origin + '/oauth2/callback'], 'webOrigins': [origin],
            'attributes': {'post.logout.redirect.uris': origin + '/', 'use.refresh.tokens': 'false',
                           'pkce.code.challenge.method': 'S256'},
            'defaultClientScopes': ['basic', 'profile', 'email'], 'optionalClientScopes': [],
            'protocolMappers': [{'name': 'audience', 'protocol': 'openid-connect', 'protocolMapper': 'oidc-audience-mapper',
                'config': {'included.client.audience': 'research-pilot', 'access.token.claim': 'true',
                           'introspection.token.claim': 'true', 'id.token.claim': 'false'}}]}],
        'users': [{'id': u['sub'], 'username': role, 'enabled': True, 'emailVerified': True,
                   'firstName': role.title(), 'lastName': 'Synthetic', 'email': role + '@example.test',
                   'credentials': [{'type': 'password', 'value': u['password'], 'temporary': False}]} for role, u in users.items()]}
    save(root / 'realm.private.json', realm_data, readable=True)
    proxy = {'http_address': '0.0.0.0:4180', 'provider': 'keycloak-oidc', 'client_id': 'research-pilot',
        'client_secret': client_secret, 'cookie_secret': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'cookie_name': cookies['proxy'], 'cookie_secure': False, 'cookie_httponly': True, 'cookie_samesite': 'lax',
        'cookie_expire': '10m', 'cookie_refresh': '0s', 'redirect_url': origin + '/oauth2/callback',
        'oidc_issuer_url': issuer, 'skip_oidc_discovery': True, 'login_url': issuer + '/protocol/openid-connect/auth',
        'redeem_url': private + '/token', 'oidc_jwks_url': private + '/certs', 'profile_url': private + '/userinfo',
        'email_domains': ['*'], 'scope': 'openid profile email', 'upstreams': ['static://200'],
        'set_xauthrequest': True, 'pass_access_token': True, 'reverse_proxy': True, 'skip_provider_button': True,
        'insecure_oidc_skip_nonce': False, 'code_challenge_method': 'S256', 'prompt': 'login',
        'whitelist_domains': [f'127.0.0.1:{port}'], 'request_logging': False, 'auth_logging': False, 'standard_logging': False}
    # TOML scalar types are shared with JSON for this bounded configuration.
    private_write(root / 'proxy.private.cfg', ('\n'.join(k + ' = ' + json.dumps(v) for k, v in proxy.items()) + '\n').encode())
    (root / 'proxy.private.cfg').chmod(0o644)
    strip = {key: '' for key in ('Authorization', 'X-Auth-Request-Access-Token', 'X-Auth-Request-User',
             'X-Auth-Request-Email', 'X-Auth-Request-Groups', 'X-Forwarded-Access-Token', 'X-Forwarded-User',
             'X-Forwarded-Email', 'X-Forwarded-Groups')}
    strip['X-Research-Proxy'] = boundary
    routes = {'app': {'rule': 'PathPrefix(`/`)', 'priority': 1, 'service': 'app', 'middlewares': ['boundary']},
        'oauth': {'rule': 'Path(`/oauth2/start`) || Path(`/oauth2/sign_in`) || Path(`/oauth2/callback`)', 'priority': 10, 'service': 'proxy'},
        'idp': {'rule': f'PathPrefix(`/idp/realms/{realm}/protocol/openid-connect/`) || PathPrefix(`/idp/realms/{realm}/login-actions/`) || PathPrefix(`/idp/resources/`)', 'priority': 10, 'service': 'keycloak'}}
    # Only browser endpoints pass the public issuer route. Introspection/token/userinfo
    # stay private; the catch-all app returns 404 for those paths.
    routes['idp']['rule'] = f'Path(`/idp/realms/{realm}/protocol/openid-connect/auth`) || Path(`/idp/realms/{realm}/protocol/openid-connect/logout`) || Path(`/idp/realms/{realm}/protocol/openid-connect/logout/logout-confirm`) || PathPrefix(`/idp/realms/{realm}/login-actions/`) || PathPrefix(`/idp/resources/`)'
    dynamic = {'http': {'routers': routes, 'middlewares': {'boundary': {'headers': {'customRequestHeaders': strip}}},
        'services': {key: {'loadBalancer': {'servers': [{'url': url}]}} for key, url in
                     {'app': 'http://app:18765', 'proxy': 'http://oauth2-proxy:4180', 'keycloak': 'http://keycloak:8080'}.items()}}}
    save(root / 'dynamic.private.json', dynamic, readable=True)
    context = root / 'build'; context.mkdir(mode=0o700)
    for folder in ('research_workspace', 'static', 'recipes'):
        shutil.copytree(HERE / folder, context / folder, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(HERE / 'environments.json', context / 'environments.json')
    shutil.copy2(linux_broker, context / 'outfitter'); (context / 'outfitter').chmod(0o755)
    (context / 'Dockerfile').write_text(f'FROM {PINS["docker"]} AS cli\nFROM {PINS["python"]}\nCOPY --from=cli /usr/local/bin/docker /usr/local/bin/docker\nCOPY outfitter /opt/outfitter\nWORKDIR /app\nCOPY research_workspace research_workspace\nCOPY static static\nCOPY recipes recipes\nCOPY environments.json environments.json\nENV PYTHONDONTWRITEBYTECODE=1\n')
    common = {'restart': 'no', 'cpus': 1, 'mem_limit': '256m', 'pids_limit': 128, 'networks': ['pilot'],
              'logging': {'driver': 'json-file', 'options': {'max-size': '1m', 'max-file': '2'}}}
    services = {
        'traefik': {**common, 'image': PINS['traefik'], 'command': ['--entrypoints.web.address=:80', '--providers.file.filename=/etc/traefik/dynamic.json', '--log.level=ERROR'],
            'networks': ['pilot', 'entrance'], 'ports': [f'127.0.0.1:{port}:80'], 'volumes': [f'{root}/dynamic.private.json:/etc/traefik/dynamic.json:ro']},
        'keycloak': {**common, 'image': PINS['keycloak'], 'cpus': 2, 'mem_limit': '1g', 'pids_limit': 512,
            'command': ['start-dev', '--import-realm'], 'environment': {'KC_HOSTNAME': origin + '/idp', 'KC_HTTP_RELATIVE_PATH': '/idp',
                'KC_PROXY_HEADERS': 'xforwarded', 'KC_BOOTSTRAP_ADMIN_USERNAME': 'operator', 'KC_BOOTSTRAP_ADMIN_PASSWORD': admin_password},
            'volumes': [f'{root}/realm.private.json:/opt/keycloak/data/import/realm.json:ro', 'identity:/opt/keycloak/data']},
        'oauth2-proxy': {**common, 'image': PINS['proxy'], 'command': ['--config=/etc/proxy.cfg'], 'volumes': [f'{root}/proxy.private.cfg:/etc/proxy.cfg:ro']},
        'app': {**common, 'image': name + ':local', 'build': str(context), 'mem_limit': '512m',
            'command': ['python', '-m', 'research_workspace', 'serve', '--state', str(state), '--host', '0.0.0.0'],
            'volumes': [f'{state}:{state}', '/var/run/docker.sock:/var/run/docker.sock']}}
    spec = {'name': name, 'services': services, 'networks': {'pilot': {'internal': True}, 'entrance': {}}, 'volumes': {'identity': {}}}
    save(root / 'compose.private.json', spec)
    save(root / 'operator.private.json', {'users': users, 'admin_password': admin_password, 'projects': projects, 'host_broker': str(Path(broker).resolve())})
    manifest = {'name': name, 'origin': origin, 'realm': realm, 'state': str(state), 'prepared_at': time.time(),
                'deadline': time.time() + 3600, 'pins': PINS, 'budget_cpu_seconds': 600,
                'broker_sha256': hashlib.sha256(Path(broker).read_bytes()).hexdigest(),
                'linux_broker_sha256': hashlib.sha256(Path(linux_broker).read_bytes()).hexdigest(),
                'source_sha256': {str(p.relative_to(HERE)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for folder in ('research_workspace', 'static', 'recipes') for p in sorted((HERE / folder).rglob('*'))
                    if p.is_file() and '__pycache__' not in p.parts}}
    save(root / 'pilot.json', manifest)
    return {'root': str(root), 'origin': origin, 'lifetime_seconds': 3600, 'budget_cpu_seconds': 600}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare', 'start', 'down', 'watch'])
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--broker'); parser.add_argument('--linux-broker')
    args = parser.parse_args(); root = args.root.resolve()
    if args.command == 'prepare':
        print(json.dumps(prepare(root, args.broker, args.linux_broker)))
        return
    manifest = json.loads((root / 'pilot.json').read_text())
    if not root.is_relative_to(Path('/tmp').resolve()) or json.loads((root / 'compose.private.json').read_text())['name'] != manifest['name']:
        raise ValueError('Use the owned, generated pilot directory.')
    if args.command == 'watch':
        while time.time() < manifest['deadline']:
            if (root / 'stopped.json').exists():
                return
            time.sleep(min(15, max(0, manifest['deadline'] - time.time())))
    with lifecycle(root):
        if args.command == 'start':
            start(root, manifest)
            print(json.dumps({'started': True, 'origin': manifest['origin'], 'deadline': manifest['deadline']}))
        else:
            stop(root)
            print(json.dumps({'owned_stack_stopped': True, 'state_retained': str(root)}))


@contextmanager
def lifecycle(root):
    with (root / 'lifecycle.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def stop(root):
    compose(root, 'down', '--volumes', timeout=90)
    if not (root / 'stopped.json').exists():
        save(root / 'stopped.json', {'stopped_at': time.time()})


def start(root, manifest):
    if (root / 'started.json').exists() or (root / 'stopped.json').exists():
        raise ValueError('Pilot lifecycle is one-way. Prepare fresh isolated state.')
    remaining = manifest['deadline'] - time.time()
    if remaining < 60:
        raise ValueError('Too little lifetime remains for bounded startup. Prepare fresh isolated state.')
    save(root / 'started.json', {'started_at': time.time()})
    with (root / 'watch.private.log').open('ab') as log:
        watcher = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'watch', '--root', str(root)],
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    try:
        compose(root, 'up', '-d', '--build', timeout=min(600, remaining))
        if time.time() >= manifest['deadline'] or watcher.poll() is not None:
            raise ValueError('Startup did not retain its finite lifetime supervision.')
    except Exception:
        stop(root)
        raise


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit('Owned stack command failed; inspect private container state.')
