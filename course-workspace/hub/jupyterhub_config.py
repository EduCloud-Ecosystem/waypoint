"""One course per deployment, with operator-owned images and persistent homes."""
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from settings import load
from home_policy import quota_home

s = load(os.environ)
c = get_config()  # noqa: F821 - provided by JupyterHub
instance = s['instance']
c.JupyterHub.bind_url = 'http://:8000'
c.JupyterHub.hub_bind_url = 'http://0.0.0.0:8081'
c.JupyterHub.hub_connect_url = f'http://{instance}-hub:8081'
c.JupyterHub.db_url = 'sqlite:////data/jupyterhub.sqlite'
c.JupyterHub.cookie_secret_file = '/data/cookie-secret'
c.JupyterHub.cleanup_servers = True
c.JupyterHub.admin_access = False
c.JupyterHub.allow_named_servers = False
c.JupyterHub.active_server_limit = s['active_limit']
c.JupyterHub.concurrent_spawn_limit = 2
c.Authenticator.allow_all = False
c.Authenticator.allow_existing_users = False

if s['mode'] == 'oidc':
    c.JupyterHub.authenticator_class = 'oauthenticator.generic.GenericOAuthenticator'
    c.GenericOAuthenticator.client_id = s['client_id']
    c.GenericOAuthenticator.client_secret = s['client_secret']
    c.GenericOAuthenticator.oauth_callback_url = s['public_url'] + '/hub/oauth_callback'
    c.GenericOAuthenticator.authorize_url = s['issuer'] + '/protocol/openid-connect/auth'
    c.GenericOAuthenticator.token_url = s['issuer'] + '/protocol/openid-connect/token'
    c.GenericOAuthenticator.userdata_url = s['issuer'] + '/protocol/openid-connect/userinfo'
    c.GenericOAuthenticator.scope = ['openid']
    c.GenericOAuthenticator.username_claim = 'sub'
    c.GenericOAuthenticator.allowed_users = s['subjects']
    c.GenericOAuthenticator.allow_existing_users = False
    c.GenericOAuthenticator.validate_server_cert = True
    c.GenericOAuthenticator.enable_pkce = True
    c.Authenticator.refresh_pre_spawn = True
    c.JupyterHub.subdomain_host = s['public_url']
    c.JupyterHub.cookie_host_prefix_enabled = True
else:
    # Synthetic accounts only, in a loopback-only Compose deployment.
    c.JupyterHub.authenticator_class = 'dummy'
    c.DummyAuthenticator.password = s['test_password']
    c.DummyAuthenticator.allowed_users = {'alice', 'bob'}

c.JupyterHub.spawner_class = 'dockerspawner.DockerSpawner'
c.DockerSpawner.image = s['image']
c.DockerSpawner.allowed_images = {}
c.DockerSpawner.prefix = instance + '-learner'
c.DockerSpawner.remove = True
c.DockerSpawner.use_internal_ip = True
c.DockerSpawner.pull_policy = 'never'
c.DockerSpawner.notebook_dir = '/home/learner/work'
c.Spawner.default_url = '/lab'
c.Spawner.disable_user_config = True
c.Spawner.mem_limit = s['memory_mb'] * 1024 * 1024
c.Spawner.cpu_limit = s['cpu']
c.DockerSpawner.extra_host_config = {
    'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
    'read_only': True, 'pids_limit': 128,
    'memswap_limit': s['memory_mb'] * 1024 * 1024,
    'tmpfs': {'/tmp': 'rw,nosuid,nodev,size=128m,mode=1777'},
}


async def prepare_home(spawner):
    # Recheck the current operator roster even for an existing Hub login.
    allowed = s['subjects'] if s['mode'] == 'oidc' else {'alice', 'bob'}
    if spawner.user.name not in allowed:
        raise ValueError('learner is not in this course roster')
    # OIDC subject IDs stay stable across account display-name changes. The
    # course prefix prevents accidental volume sharing between deployments.
    key = hashlib.sha256(spawner.user.name.encode()).hexdigest()[:24]
    labels = {'educloud.workspace.instance': instance, 'educloud.workspace.kind': 'home'}
    volume = f'{instance}-home-{key}'
    quota = quota_home(s, key)
    if quota:
        labels = quota['labels']
    existing = await spawner.docker('volumes', filters={'name': volume})
    found = False
    for item in existing.get('Volumes') or []:
        if item['Name'] == volume:
            found = True
            if item.get('Labels') != labels or (quota and item.get('Options') != quota['options']):
                raise ValueError('refusing a home volume not owned/provisioned for this course')
    if quota and not found:
        raise ValueError('operator must provision this learner home with a hard quota')
    if not quota:
        await spawner.docker('create_volume', name=volume, labels=labels)
    spawner.volumes = {volume: '/home/learner'}
    spawner.extra_create_kwargs = {'user': '1000:1000', 'labels': {
        'educloud.workspace.instance': instance, 'educloud.workspace.kind': 'learner'}}
    # Each learner has an internal Docker network. Only the hub joins it;
    # peers cannot connect directly and the workspace has no Internet egress.
    network = f'{instance}-user-{key}'
    networks = await spawner.docker('networks', names=[network])
    if networks:
        if networks[0].get('Labels') != {'educloud.workspace.instance': instance} or not networks[0]['Internal']:
            raise ValueError('refusing a network not owned by this course')
    else:
        await spawner.docker('create_network', network, internal=True,
                            labels={'educloud.workspace.instance': instance})
    hub = await spawner.docker('inspect_container', instance + '-hub')
    if network not in hub['NetworkSettings']['Networks']:
        await spawner.docker('connect_container_to_network', hub['Id'], network,
                            aliases=[instance + '-hub'])
    spawner.network_name = network


c.Spawner.pre_spawn_hook = prepare_home
c.JupyterHub.load_roles = [{
    'name': 'idle-culler',
    'scopes': ['list:users', 'read:users:activity', 'read:servers', 'delete:servers'],
    'services': ['idle-culler'],
}]
c.JupyterHub.services = [{
    'name': 'idle-culler',
    'command': [sys.executable, '-m', 'jupyterhub_idle_culler',
                f'--timeout={s["idle_seconds"]}', f'--max-age={s["max_age_seconds"]}',
                '--cull-every=60'],
}]
