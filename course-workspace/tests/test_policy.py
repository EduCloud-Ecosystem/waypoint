import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import asyncio
import os
import runpy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'hub')]
from settings import load
from backup import validate_manifest


class PolicyTests(unittest.TestCase):
    def config(self):
        return {'WORKSPACE_INSTANCE':'test-course', 'WORKSPACE_IMAGE':'course:pinned',
                'WORKSPACE_PUBLIC_URL':'https://work.example.edu',
                'WORKSPACE_OIDC_ISSUER':'https://auth.example.edu/realms/educloud',
                'WORKSPACE_OIDC_CLIENT_ID':'course', 'WORKSPACE_OIDC_CLIENT_SECRET':'synthetic',
                'WORKSPACE_ALLOWED_SUBJECTS':'opaque-a,opaque-b'}

    def test_production_requires_roster_and_credentials(self):
        for missing in ['WORKSPACE_ALLOWED_SUBJECTS', 'WORKSPACE_OIDC_CLIENT_SECRET', 'WORKSPACE_OIDC_ISSUER']:
            env = self.config(); del env[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError): load(env)
        self.assertEqual(load(self.config())['subjects'], {'opaque-a', 'opaque-b'})

    def test_local_auth_cannot_use_public_origin(self):
        env = self.config(); env.update(WORKSPACE_AUTH_MODE='local-test', WORKSPACE_TEST_PASSWORD='x'*32)
        with self.assertRaises(ValueError): load(env)
        env['WORKSPACE_PUBLIC_URL'] = 'http://127.0.0.1:18000'
        self.assertEqual(load(env)['mode'], 'local-test')

    def test_spawn_rechecks_roster_for_existing_login(self):
        with patch.dict(os.environ, self.config(), clear=True):
            config = runpy.run_path(str(ROOT/'hub/jupyterhub_config.py'),
                                   init_globals={'get_config': MagicMock})
        stale_login = SimpleNamespace(user=SimpleNamespace(name='removed-learner'))
        with self.assertRaisesRegex(ValueError, 'not in this course roster'):
            asyncio.run(config['prepare_home'](stale_login))

    def test_invalid_resource_limits_rejected(self):
        for key, value in [('WORKSPACE_CPU_LIMIT','nan'), ('WORKSPACE_CPU_LIMIT','inf'),
                           ('WORKSPACE_MEMORY_MB','0'), ('WORKSPACE_ACTIVE_LIMIT','-1'),
                           ('WORKSPACE_IDLE_SECONDS','0'), ('WORKSPACE_INSTANCE','../other')]:
            env = self.config(); env[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError): load(env)

    def test_archive_integrity_and_path_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d); (p/'hub-data.tar.gz').write_bytes(b'synthetic archive')
            item = {'suffix':'hub-data', 'kind':'hub', 'sha256':hashlib.sha256(b'synthetic archive').hexdigest()}
            manifest = {'version':1, 'instance':'source-course', 'volumes':[item]}
            self.assertEqual(validate_manifest(manifest,p), 'source-course')
            for key, value in [('suffix','../escape'), ('kind','home'), ('sha256','wrong')]:
                bad=json.loads(json.dumps(manifest)); bad['volumes'][0][key]=value
                with self.subTest(key=key), self.assertRaises(ValueError): validate_manifest(bad,p)
            manifest['volumes'].append(item.copy())
            with self.assertRaises(ValueError): validate_manifest(manifest,p)

if __name__ == '__main__': unittest.main()
