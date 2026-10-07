import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'hub')]
from home_policy import quota_home
from homes import limits, storage, registry


class HomePolicyTests(unittest.TestCase):
    def test_missing_stale_or_wrong_quota_refuses_spawn(self):
        s = {'home_root': '/srv/homes', 'instance': 'trial-test', 'home_quota_mb': 64, 'home_inode_limit': 1000}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(FileNotFoundError):
                quota_home(s, 'abc', root)
            data = {'healthy': True, 'instance': 'trial-test', 'checked_at': time.time(),
                    'volumes': {'trial-test-home-abc': {'quota_bytes': 64 * 1024**2, 'project_id': 10000, 'inode_limit': 1000}}}
            path = root / 'trial-test.json'
            path.write_text(json.dumps(data))
            self.assertEqual(quota_home(s, 'abc', root)['options']['device'], '/srv/homes/trial-test-home-abc')
            for change in [{'checked_at': time.time() - 121}, {'healthy': False}, {'volumes': {}}, {'instance': 'other'}]:
                path.write_text(json.dumps(dict(data, **change)))
                with self.assertRaises(ValueError):
                    quota_home(s, 'abc', root)

    def test_ordinary_trial_does_not_claim_a_disk_quota(self):
        self.assertIsNone(quota_home({'home_root': ''}, 'abc'))

    def test_ext4_or_disabled_enforcement_is_refused(self):
        with patch('homes.os.geteuid', return_value=0), patch('homes.run') as run:
            run.return_value = json.dumps({'filesystems': [{'target': '/srv/homes', 'fstype': 'ext4', 'options': 'rw'}]})
            with self.assertRaisesRegex(ValueError, 'dedicated XFS'):
                storage(Path('/srv/homes'))
            run.side_effect = [json.dumps({'filesystems': [{'target': '/srv/homes', 'fstype': 'xfs', 'options': 'rw,prjquota'}]}),
                               'Accounting: ON\nEnforcement: OFF']
            with self.assertRaisesRegex(ValueError, 'both be ON'):
                storage(Path('/srv/homes'))

    def test_unmanaged_data_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'existing.txt').write_text('retain')
            with self.assertRaises(ValueError):
                registry(root)
            self.assertEqual((root / 'existing.txt').read_text(), 'retain')

    def test_parse_numeric_quota_report(self):
        with patch('homes.run', return_value='#10000 24 65536 65536 00 [--------]\n#10001 0 65536 65536 00 [--------]'):
            self.assertEqual(limits(Path('/srv/homes'), 'b')[10000], {'used': 24, 'hard': 65536})


if __name__ == '__main__':
    unittest.main()
