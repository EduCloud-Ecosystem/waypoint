import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup import restore
from deployment import read_environment, settings
from public_course import render
from recovery import current_institutional_environment


class InstitutionalRecoveryTests(unittest.TestCase):
    def environment(self, root):
        _, _, env = render('course.example.org','https://identity.example.org/realms/course',
                           'course','course-old',['alice-id','bob-id'],'/srv/homes')
        env.update(WORKSPACE_OIDC_CLIENT_SECRET='old-client-credential',WORKSPACE_ENV_FILE=str(root/'.env'))
        return env

    def write(self, path, env):
        path.write_text(''.join(f'{k}={v}\n' for k,v in env.items()))
        path.chmod(0o600)

    def test_current_roster_and_credential_replace_stale_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            saved=self.environment(root)
            current=dict(saved,WORKSPACE_ALLOWED_SUBJECTS='bob-id',WORKSPACE_OIDC_CLIENT_SECRET='rotated-client-credential')
            self.write(root/'.env',current)
            restored=current_institutional_environment(saved,root/'.env')
            self.assertEqual(restored['WORKSPACE_ALLOWED_SUBJECTS'],'bob-id')
            self.assertEqual(restored['WORKSPACE_OIDC_CLIENT_SECRET'],'rotated-client-credential')
            self.assertEqual(settings(root),current)
            with self.assertRaisesRegex(ValueError,'current-env'):
                current_institutional_environment(saved,None)
            for key,value in [('WORKSPACE_OIDC_ISSUER','https://other.example.org/realms/course'),
                              ('WORKSPACE_OIDC_CLIENT_ID','another-client'),
                              ('WORKSPACE_PUBLIC_URL','https://other.example.org'),
                              ('WORKSPACE_HOME_QUOTA_MB','1024'),
                              ('WORKSPACE_AUTH_MODE','local-test')]:
                self.write(root/'.env',dict(current,**{key:value}))
                with self.assertRaises(ValueError):
                    current_institutional_environment(saved,root/'.env')

    def test_private_literal_environment_cannot_redirect_compose(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'.env'
            for content in ['WORKSPACE_PORT=${PORT}\n','WORKSPACE_PORT=18000\nWORKSPACE_PORT=19000\n',
                            'COMPOSE_FILE=other.yml\n','WORKSPACE_OIDC_CLIENT_SECRET=value # comment\n']:
                path.write_text(content); path.chmod(0o600)
                with self.assertRaises(ValueError):
                    read_environment(path)
            path.write_text('WORKSPACE_PORT=18000\n'); path.chmod(0o644)
            with self.assertRaises(ValueError):
                read_environment(path)

    def test_fresh_hub_restore_never_unpacks_archived_sessions(self):
        manifest={'volumes':[{'suffix':'hub-data','kind':'hub'},
                    {'suffix':'home-'+'a'*24,'kind':'home'}]}
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'manifest.json').write_text(json.dumps(manifest))
            (root/('home-'+'a'*24+'.tar.gz')).write_bytes(b'fixture')
            with patch('backup.validate_manifest'),patch('backup.docker',return_value='') as docker, \
                    patch('backup.helper') as unpack:
                restore('recovered-test',root,'image',fresh_hub=True)
            self.assertEqual(unpack.call_count,1)
            self.assertEqual(unpack.call_args.args[2],'recovered-test-home-'+'a'*24)
            self.assertFalse(any('hub-data' in str(call) for call in docker.call_args_list))


if __name__=='__main__':
    unittest.main()
