"""Opt-in socketless Hub delegate. No Docker API, runtime keys, or cached grants."""
import asyncio
import ipaddress
import re

from jupyterhub.spawner import Spawner
from traitlets import Integer, Unicode
from authority_client import AuthorityClient

ENV_KEYS = frozenset({'JUPYTERHUB_API_TOKEN', 'JUPYTERHUB_CLIENT_ID', 'JUPYTERHUB_HOST',
    'JUPYTERHUB_USER', 'JUPYTERHUB_OAUTH_CALLBACK_URL', 'JUPYTERHUB_API_URL',
    'JUPYTERHUB_BASE_URL', 'JUPYTERHUB_SERVICE_PREFIX', 'JUPYTERHUB_SERVICE_URL',
    'JUPYTERHUB_ACTIVITY_URL', 'JUPYTERHUB_OAUTH_ACCESS_SCOPES',
    'JUPYTERHUB_OAUTH_CLIENT_ALLOWED_SCOPES', 'JUPYTERHUB_SERVER_NAME',
    'JUPYTERHUB_COOKIE_OPTIONS', 'JUPYTERHUB_OAUTH_SCOPES', 'JUPYTERHUB_COOKIE_HOST_PREFIX_ENABLED',
    'JUPYTERHUB_PUBLIC_URL', 'JUPYTERHUB_PUBLIC_HUB_URL'})


class AuthoritySpawner(Spawner):
    ip = Unicode('0.0.0.0', config=True)
    port = Integer(8888, config=True)
    authority_url = Unicode(config=True)
    authority_id = Unicode(config=True)
    authority_ca = Unicode(config=True)
    authority_cert = Unicode(config=True)
    authority_key = Unicode(config=True)
    worker_id = Unicode(config=True)
    course_id = Unicode(config=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.server_id = None
        self.server_epoch = None

    def client(self):
        return AuthorityClient(self.authority_url, self.authority_id, self.authority_ca,
            self.authority_cert, self.authority_key, self.worker_id, self.course_id, timeout=10)

    async def call(self, operation, payload):
        return await asyncio.to_thread(self.client().call, operation, payload)

    def get_state(self):
        state = super().get_state()
        if self.server_id is not None:
            state.update(server_id=self.server_id, server_epoch=self.server_epoch)
        return state

    def load_state(self, state):
        super().load_state(state)
        server_id, epoch = state.get('server_id'), state.get('server_epoch')
        if server_id is None and epoch is None:
            return
        if (not isinstance(server_id, str) or not re.fullmatch(r'[0-9a-f]{64}', server_id)
                or type(epoch) is not int or not 1 <= epoch <= 10**9):
            raise ValueError('authority_spawner_state_invalid')
        self.server_id, self.server_epoch = server_id, epoch

    def clear_state(self):
        super().clear_state()
        self.server_id = self.server_epoch = None

    async def start(self):
        self.log.info('Authority notebook startup: checking ownership')
        view = await self.call('hub.authorize', {})
        if (set(view) != {'authorized', 'epoch', 'worker_id', 'course_id'} or view['authorized'] is not True
                or view['worker_id'] != self.worker_id or view['course_id'] != self.course_id
                or type(view['epoch']) is not int or not 1 <= view['epoch'] <= 10**9):
            raise ValueError('authority_spawner_view_invalid')
        self.log.info('Authority notebook startup: preparing environment')
        environment = {key: value for key, value in self.get_env().items() if key in ENV_KEYS}
        self.log.info('Authority notebook startup: creating runtime')
        result = await self.call('hub.spawn', {'subject': self.user.name, 'environment': environment,
                                            'expected_epoch': view['epoch']})
        if (set(result) != {'server_id', 'epoch', 'address', 'port'} or result['epoch'] != view['epoch']
                or not isinstance(result['server_id'], str) or not re.fullmatch(r'[0-9a-f]{64}', result['server_id'])
                or result['port'] != 8888):
            raise ValueError('authority_spawner_server_invalid')
        ipaddress.ip_address(result['address'])
        self.log.info('Authority notebook startup: runtime created')
        self.server_id, self.server_epoch = result['server_id'], result['epoch']
        return result['address'], result['port']

    async def poll(self):
        if self.server_id is None:
            return 0
        try:
            result = await self.call('hub.poll', {'subject': self.user.name,
                        'server_id': self.server_id, 'epoch': self.server_epoch})
        except Exception:
            # Runtime guardian/supervisor handles stopping; an unavailable view
            # cannot make an old Hub believe its server is currently authorized.
            return 1
        if set(result) != {'running', 'exit_code'} or type(result['running']) is not bool:
            raise ValueError('authority_spawner_poll_invalid')
        if result['running']:
            if result['exit_code'] is not None: raise ValueError('authority_spawner_poll_invalid')
            return None
        if type(result['exit_code']) is not int: raise ValueError('authority_spawner_poll_invalid')
        return result['exit_code']

    async def stop(self, now=False):
        if self.server_id is None:
            return
        result = await self.call('hub.stop', {'subject': self.user.name,
                    'server_id': self.server_id, 'epoch': self.server_epoch})
        if result != {'stopped': True}: raise ValueError('authority_spawner_stop_invalid')
        self.clear_state()
