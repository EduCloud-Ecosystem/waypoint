"""Roster binding, finite credentials and no-AI dispatch controls."""
import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'course'))
from waypoint_belay import roster, configuration, allowed_route, assignment

class RosterPresenterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve(); self.now = int(time.time())
        self.profile = dict(version=1, issuer='https://identity.test.invalid', audience='belay-tests', subject='verified-subject',
            institution_id='institution', class_id='course', learner_id='a'*32, grant_generation='b'*32, authority_id='c'*32,
            authorization_file=str(self.root/'authorization.json'), credential_file=str(self.root/'credential'),
            expires_at=self.now+300, assistance='hints', exercise_id='echo-1', exercise_version='v1', assignment_title='Synthetic assignment')
        self.snapshot = dict(authority=dict(schema_version=1, authority_id='c'*32, issuer=self.profile['issuer'], audience='belay-tests',
            revision=1, issued_at=self.now, expires_at=self.now+120), exercise_versions={'echo-1':'v1'},
            subjects={'verified-subject':[dict(institution_id='institution',class_id='course',learner_id='a'*32,
                learner_id_kind='cairn_roster_v1',grant_generation='b'*32,assignments={'echo-1':'v1'})]})
        self.write('profile.json',self.profile); self.write('authorization.json',self.snapshot)
        self.claims = dict(iss=self.profile['issuer'],aud='belay-tests',sub='verified-subject',iat=self.now,exp=self.now+120)
        self.token(self.claims)
    def write(self,name,data):
        p=self.root/name;p.write_text(json.dumps(data));p.chmod(0o600)
    def token(self,claims):
        encoded=base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        p=self.root/'credential';p.write_text('e30.'+encoded+'.c2ln');p.chmod(0o600)
    def loaded(self):return roster.load(self.root/'profile.json')
    def test_exact_binding_and_rotation_reads_current_private_credential(self):
        p=self.loaded(); first=roster.credential(p,'echo-1','v1'); self.claims['exp']+=1;self.token(self.claims)
        self.assertNotEqual(first,roster.credential(p,'echo-1','v1'))
    def test_missing_revoked_wrong_class_changed_generation_assignment_and_expiry_refuse(self):
        for mutation in ('revoked','class','generation','assignment','expired','issuer','audience','authority'):
            with self.subTest(mutation=mutation):
                s=json.loads(json.dumps(self.snapshot));m=s['subjects']['verified-subject'][0]
                if mutation=='revoked':s['subjects']={}
                elif mutation=='class':m['class_id']='other'
                elif mutation=='generation':m['grant_generation']='d'*32
                elif mutation=='assignment':m['assignments']={'echo-1':'v2'}
                elif mutation=='expired':s['authority']['expires_at']=self.now
                else:s['authority'][{'issuer':'issuer','audience':'audience','authority':'authority_id'}[mutation]]='other'
                self.write('authorization.json',s)
                with self.assertRaises(ValueError):roster.credential(self.loaded(),'echo-1','v1')
    def test_credential_scope_and_lifetime(self):
        for change in ({'sub':'peer'}, {'iss':'https://other.invalid'}, {'aud':'other'}, {'exp':self.now}, {'iat':self.now+60}, {'exp':self.now+901}, {'iat':True}):
            with self.subTest(change=change):
                self.token(dict(self.claims,**change))
                with self.assertRaises(ValueError):roster.credential(self.loaded(),'echo-1','v1')
    def test_instructor_disables_existing_session_without_restarting(self):
        p=self.loaded();self.profile['assistance']='disabled';self.write('profile.json',self.profile)
        with self.assertRaises(PermissionError):roster.credential(p,'echo-1','v1')
        self.assertEqual(roster.refresh(p)['assistance'],'disabled')
    def test_changed_profile_does_not_rebind_an_existing_notebook(self):
        p=self.loaded();self.profile['subject']='other';self.write('profile.json',self.profile)
        with self.assertRaises(ValueError):roster.credential(p,'echo-1','v1')
    def test_private_file_bounds_and_symlinks(self):
        p=self.root/'credential';p.chmod(0o644)
        with self.assertRaises(ValueError):roster.credential(self.loaded(),'echo-1','v1')
        p.unlink();p.symlink_to(self.root/'profile.json')
        with self.assertRaises(OSError):roster.credential(self.loaded(),'echo-1','v1')
        p.unlink();os.mkfifo(p,0o600)
        with self.assertRaises(ValueError):roster.credential(self.loaded(),'echo-1','v1')
    def test_explicit_namespace_and_assignment_routes(self):
        env=dict(WORKSPACE_BELAY_LOCAL_TEST='1',WORKSPACE_BELAY_HOST_ORIGIN='http://127.0.0.1:5188',
            WORKSPACE_BELAY_API_ORIGIN='http://127.0.0.1:8018',WORKSPACE_BELAY_WORKSPACE_ORIGIN='http://127.0.0.1:5190',
            WORKSPACE_BELAY_ASSET_ROOT=str(self.root),WORKSPACE_BELAY_LEARNER_ID='a'*32,WORKSPACE_BELAY_ROSTER_PROFILE=str(self.root/'profile.json'))
        c=configuration(env);self.assertEqual(assignment(c),('echo-1','v1'))
        self.assertTrue(allowed_route('GET','/api/classroom/echo-1/v1',profile='roster',bound_assignment=assignment(c)))
        self.assertFalse(allowed_route('GET','/api/classroom/other/v1',profile='roster',bound_assignment=assignment(c)))
        env['WORKSPACE_BELAY_LEARNER_ID']='edu:2'
        with self.assertRaises(ValueError):configuration(env)
