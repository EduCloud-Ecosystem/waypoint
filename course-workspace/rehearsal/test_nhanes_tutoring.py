"""No network: explicit live admission and immutable source/expiry boundaries."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from live_notebook import notebook_inputs, expected_request, SOURCE, QUESTION
from nhanes_tutoring import NhanesTutoring, validate_tutoring


class NhanesTutoringControls(unittest.TestCase):
    def test_openbao_requires_live_approval_complete_binding_and_no_fallback(self):
        config = ('https://127.0.0.1:8200', Path('/ca'), Path('/token'))
        validate_tutoring('live', Path('/portage'), None, True, 0, openbao=config)
        for mode, keyfile, approved, binding in [
            ('stub', None, True, config), ('none', None, True, config),
            ('live', Path('/fallback'), True, config), ('live', None, False, config),
            ('live', None, True, (config[0], None, config[2])),
        ]:
            with self.assertRaises(ValueError):
                validate_tutoring(mode, Path('/portage'), keyfile, approved, 0, openbao=binding)

    def test_echo_unchanged_and_nhanes_source_authored(self):
        self.assertEqual(notebook_inputs(), {'pack':'_skeleton','exercise':'echo-1','source':SOURCE,'question':QUESTION})
        package = Path(__file__).resolve().parents[1] / 'classroom/info511-nhanes-v1'
        original = json.loads((package / 'operator/approved-notebook-turn.json').read_text())
        inputs = notebook_inputs('nhanes-v1')
        self.assertEqual({k:inputs[k] for k in original}, original)
        self.assertEqual(inputs['pack'], 'datascience')
        with self.assertRaises(ValueError): notebook_inputs('/tmp/unapproved-profile')

    def test_pack_mismatch_refused_before_subprocess(self):
        with patch('live_notebook.subprocess.check_output') as run:
            with self.assertRaises(ValueError):
                expected_request(Path('/unused'), {'TUTOR_PACK':'_skeleton'}, 'nhanes-v1')
        run.assert_not_called()

    def test_live_flags_cannot_enter_no_ai_or_stub_mode(self):
        validate_tutoring('none', None, None, False, 0)
        validate_tutoring('stub', None, None, False, 0)
        for mode in ('none','stub'):
            with self.assertRaises(ValueError): validate_tutoring(mode, Path('/unused'), None, False, 0)
        for values in [(None,None,False,0),(Path('/unused'),None,True,0),(Path('/unused'),Path('/unused'),False,0)]:
            with self.assertRaises(ValueError): validate_tutoring('live', *values)
        for seconds in (-1,241,True):
            with self.assertRaises(ValueError): validate_tutoring('live', Path('/unused'), Path('/unused'), True, seconds)
        validate_tutoring('live', Path('/unused'), Path('/unused'), True, 240)

    def test_restoring_credential_does_not_renew_expiration(self):
        with tempfile.TemporaryDirectory() as directory:
            tutor = NhanesTutoring.__new__(NhanesTutoring)
            tutor.root = Path(directory); tutor.key = object(); tutor.expires = 100
            tutor.binding = {'issuer':'synthetic','audience':'synthetic'}
            with patch('nhanes_tutoring.time.time', return_value=90), patch('nhanes_tutoring.jwt.encode', return_value='synthetic') as encode:
                tutor.issue(expired=True); self.assertEqual(encode.call_args.args[0]['exp'], 89)
                tutor.issue(); self.assertEqual(encode.call_args.args[0]['exp'], 100)
            with patch('nhanes_tutoring.time.time', return_value=99), patch('nhanes_tutoring.jwt.encode', return_value='synthetic') as encode:
                tutor.issue(); self.assertEqual(encode.call_args.args[0]['exp'], 100)


if __name__ == '__main__': unittest.main()
