from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from traitlets.config import Config
from tornado.web import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'hub'))
from settings import load


class RuntimeSettingsTests(unittest.IsolatedAsyncioTestCase):
    def env(self, optin=True):
        env = {'WORKSPACE_INSTANCE': 'course-one', 'WORKSPACE_AUTH_MODE': 'local-test',
            'WORKSPACE_PUBLIC_URL': 'http://127.0.0.1:18000', 'WORKSPACE_TEST_PASSWORD': 'a' * 32,
            'WORKSPACE_IMAGE': 'sha256:' + 'b' * 64}
        if optin:
            env.update(WORKSPACE_RUNTIME_MODE='authority-local', WORKSPACE_AUTHORITY_URL='https://authority.test:8443',
                WORKSPACE_AUTHORITY_ID='a' * 32, WORKSPACE_AUTHORITY_WORKER_ID='worker-one', WORKSPACE_AUTHORITY_COURSE_ID='course-one',
                WORKSPACE_AUTHORITY_CA='/run/authority/ca.pem', WORKSPACE_AUTHORITY_CERT='/run/authority/client.pem', WORKSPACE_AUTHORITY_KEY='/run/authority/client.key')
        return env

    def config(self, env, socket=False):
        config = Config()
        with patch.dict('os.environ', env, clear=True), patch.object(Path, 'exists', return_value=socket):
            namespace = runpy.run_path(str(ROOT / 'hub/jupyterhub_config.py'), init_globals={'get_config': lambda: config})
        return config, namespace

    def test_default_and_optin_configs_preserve_distinct_spawners(self):
        default, _ = self.config(self.env(False))
        self.assertEqual(default.JupyterHub.spawner_class, 'dockerspawner.DockerSpawner')
        optin, _ = self.config(self.env())
        self.assertEqual(optin.JupyterHub.spawner_class, 'authority_spawner.AuthoritySpawner')
        self.assertEqual(optin.Spawner.environment, {})
        self.assertEqual(optin.Authenticator.auth_refresh_age, 1)
        self.assertTrue(optin.Authenticator.refresh_pre_spawn)

    def test_socket_and_bad_authority_scope_refused(self):
        with self.assertRaises(ValueError): self.config(self.env(), socket=True)
        for key, value in [('WORKSPACE_AUTHORITY_URL', 'http://authority'), ('WORKSPACE_AUTHORITY_ID', 'bad'), ('WORKSPACE_AUTHORITY_WORKER_ID', '../worker'), ('WORKSPACE_AUTHORITY_KEY', '/run/../key')]:
            with self.assertRaises(ValueError): load(dict(self.env(), **{key: value}))

    async def test_fresh_login_and_cookie_refresh_fail_closed_on_remote_refusal(self):
        config, namespace = self.config(self.env())
        class Client:
            def __init__(self, *args, **kwargs): pass
            def call(self, op, payload): raise ConnectionError('unavailable fixture')
        hook = config.Authenticator.post_auth_hook
        hook.__globals__['AuthorityClient'] = Client
        with self.assertRaises(HTTPError) as error: await hook(None, None, {'name': 'alice'})
        self.assertEqual(error.exception.status_code, 403)
        authenticator = config.JupyterHub.authenticator_class()
        self.assertFalse(await authenticator.refresh_user(SimpleNamespace(name='alice')))


if __name__ == '__main__': unittest.main()
