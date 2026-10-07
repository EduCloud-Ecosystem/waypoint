from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from worker import cloud_config, units


class WorkerTests(unittest.TestCase):
    def test_setup_pins_source_and_only_opens_operator_ssh(self):
        cfg = cloud_config('ssh-ed25519 c3ludGhldGlj', '192.0.2.5/32', 'a' * 40)
        self.assertFalse(cfg['ssh_pwauth'])
        allows = [cmd for cmd in cfg['runcmd'] if cmd[:2] == ['ufw', 'allow']]
        self.assertEqual(allows, [['ufw', 'allow', 'from', '192.0.2.5/32', 'to', 'any', 'port', '22', 'proto', 'tcp']])
        self.assertTrue(any(cmd[-2:] == ['--detach', 'a' * 40] for cmd in cfg['runcmd']))
        self.assertFalse(any('educloud-backup.timer' in cmd for cmd in cfg['runcmd']))

    def test_mutable_release_and_private_key_refused(self):
        for key, cidr, release in [('ssh-ed25519 c3ludGhldGlj', '192.0.2.1/32', 'main'),
                                   ('-----BEGIN PRIVATE KEY-----', '192.0.2.1/32', 'a' * 40),
                                   ('ssh-ed25519 c3ludGhldGlj', 'not-a-network', 'a' * 40)]:
            with self.assertRaises(ValueError):
                cloud_config(key, cidr, release)

    def test_schedule_is_explicit_and_checks_failure_and_age(self):
        config = units()
        self.assertIn('02:00:00 UTC', config['educloud-backup.timer'])
        self.assertIn('UMask=0077', config['educloud-backup.service'])
        self.assertIn('--apply-retention', config['educloud-backup.service'])
        self.assertIn('recovery.py status', config['educloud-backup-health.service'])


if __name__ == '__main__':
    unittest.main()
