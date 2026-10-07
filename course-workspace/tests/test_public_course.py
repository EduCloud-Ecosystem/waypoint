from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'hub')]
from public_course import render
from settings import load


class PublicCourseTests(unittest.TestCase):
    def test_configuration_matches_the_hub_and_is_not_startable_without_secret(self):
        nginx, client, env = render('work.example.test', 'https://auth.example.test/realms/educloud',
                                    'course', 'course-test', ['subject-b', 'subject-a'], '/srv/homes')
        self.assertEqual(client['redirectUris'], ['https://work.example.test/hub/oauth_callback'])
        self.assertEqual(client['attributes']['pkce.code.challenge.method'], 'S256')
        self.assertFalse(client['publicClient'])
        self.assertFalse(client['directAccessGrantsEnabled'])
        self.assertIn('server_name work.example.test *.work.example.test;', nginx)
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', nginx)
        self.assertIn('proxy_pass http://127.0.0.1:18000;', nginx)
        with self.assertRaises(ValueError):
            load(env)
        env['WORKSPACE_OIDC_CLIENT_SECRET'] = 'synthetic-unit-test'
        self.assertEqual(load(env)['subjects'], {'subject-a', 'subject-b'})

    def test_configuration_injection_and_non_identity_roster_are_refused(self):
        base = dict(domain='work.example.test', issuer='https://auth.example.test/realms/educloud',
                    client_id='course', instance='course-test', subjects=['opaque-subject'], home_root='/srv/homes')
        for value in [dict(domain='example.test; include /tmp/*;'), dict(issuer='http://auth.example.test/realms/a'),
                      dict(subjects=['student@example.test']), dict(home_root='/srv/../etc')]:
            with self.assertRaises(ValueError):
                render(**dict(base, **value))


if __name__ == '__main__':
    unittest.main()
