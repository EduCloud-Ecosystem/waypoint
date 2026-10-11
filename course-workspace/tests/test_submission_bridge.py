import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'course'), str(ROOT / 'hub')]
import waypoint_cairn as bridge
from settings import load


class SavedFilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / 'analysis').mkdir()
        self.raw = b'print("synthetic")\n\x00\xff'
        (self.root / 'analysis' / 'saved.py').write_bytes(self.raw)

    def test_exact_saved_bytes_hashes_and_nested_selection(self):
        files = bridge.read_saved(self.root, ['analysis/saved.py'])
        self.assertEqual(files, [{'path': 'analysis/saved.py', 'size': len(self.raw), 'sha256': hashlib.sha256(self.raw).hexdigest(), 'content_base64': base64.b64encode(self.raw).decode()}])
        notebook = b'{"cells":[{"outputs":["preserve saved output"]}]}\n'
        (self.root / 'work.ipynb').write_bytes(notebook)
        self.assertEqual(base64.b64decode(bridge.read_saved(self.root, ['work.ipynb'])[0]['content_base64']), notebook)
        (self.root / 'empty.txt').touch()
        self.assertEqual(bridge.read_saved(self.root, ['empty.txt'])[0]['size'], 0)

    def test_unsafe_paths_duplicates_and_count_fail_closed(self):
        for path in ('../outside', '/etc/passwd', '.hidden', 'a/.secret', 'a/../b', 'a//b', 'a/./b', 'a\\b', 'a\n.py', 'résumé.txt', 'a' * 241, 'file ', 'file name.py', 'file..py', 'file.', '_file.py', '-file.py', ''):
            with self.subTest(path=path), self.assertRaises(ValueError):
                bridge.read_saved(self.root, [path])
        for paths in ([], ['analysis/saved.py'] * 2, ['x'] * 33, 'saved.py', [None]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                bridge.read_saved(self.root, paths)

    def test_all_symlink_components_and_special_files_refused(self):
        (self.root / 'link.py').symlink_to(self.root / 'analysis/saved.py')
        (self.root / 'linked').symlink_to(self.root / 'analysis', target_is_directory=True)
        os.mkfifo(self.root / 'pipe')
        for path in ('link.py', 'linked/saved.py', 'pipe', 'analysis'):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                bridge.read_saved(self.root, [path])
        with self.assertRaises(OSError):
            bridge.read_saved(self.root / 'linked', ['saved.py'])
        listed = bridge.list_saved(self.root)
        self.assertEqual([row['name'] for row in listed], ['analysis'])

    def test_file_and_aggregate_byte_limits(self):
        (self.root / 'big').write_bytes(b'a' * (bridge.MAX_FILE_BYTES + 1))
        with self.assertRaises(ValueError):
            bridge.read_saved(self.root, ['big'])
        for index in range(5):
            (self.root / str(index)).write_bytes(b'x' * bridge.MAX_FILE_BYTES)
        self.assertEqual(sum(item['size'] for item in bridge.read_saved(self.root, ['0','1','2','3'])), bridge.MAX_TOTAL_BYTES)
        with self.assertRaises(ValueError):
            bridge.read_saved(self.root, ['0','1','2','3','4'])

    def test_modified_during_read_is_rejected(self):
        original = os.fstat
        calls = 0
        def changed(fd):
            nonlocal calls
            result = original(fd)
            calls += 1
            if calls == 2:
                from types import SimpleNamespace
                return SimpleNamespace(st_size=result.st_size, st_mtime_ns=result.st_mtime_ns + 1, st_ctime_ns=result.st_ctime_ns)
            return result
        with patch.object(bridge.os, 'fstat', side_effect=changed), self.assertRaises(ValueError):
            bridge.read_saved(self.root, ['analysis/saved.py'])

    def test_listing_is_bounded_and_excludes_hidden_unsupported_names(self):
        (self.root / '.secret').write_text('private')
        (self.root / 'unicode-é').touch()
        self.assertEqual([item['name'] for item in bridge.list_saved(self.root)], ['analysis'])
        with patch.object(bridge, 'MAX_ENTRIES', 2), self.assertRaises(ValueError):
            bridge.list_saved(self.root)

    def test_origin_is_exact_and_http_requires_explicit_synthetic_flag(self):
        self.assertEqual(bridge.cairn_origin('https://Cairn.example.edu:443/'), 'https://cairn.example.edu')
        self.assertEqual(bridge.cairn_origin('http://127.0.0.1:8123', True), 'http://127.0.0.1:8123')
        for origin in ('http://127.0.0.1:8123', 'https://user:pass@host', 'https://host/path', 'https://host?token=private', 'https://host#x', 'https://host:0', 'https://host\\evil', 'https://h ost', 'https://host\n', '//host', 'javascript:alert(1)'):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                bridge.cairn_origin(origin)
        with self.assertRaises(ValueError):
            bridge.cairn_origin('http://public.example.edu', True)

    def test_hub_setting_defaults_disabled_and_synthetic_override_does_not_weaken_workspace(self):
        env = {'WORKSPACE_INSTANCE': 'synthetic', 'WORKSPACE_IMAGE': 'image:test', 'WORKSPACE_PUBLIC_URL': 'https://work.example.edu', 'WORKSPACE_OIDC_ISSUER': 'https://auth.example.edu/realms/educloud', 'WORKSPACE_OIDC_CLIENT_ID': 'fixture', 'WORKSPACE_OIDC_CLIENT_SECRET': 'synthetic', 'WORKSPACE_ALLOWED_SUBJECTS': 'fixture', 'WORKSPACE_HOME_ROOT': '/srv/homes', 'WORKSPACE_HOME_QUOTA_MB': '128'}
        self.assertEqual(load(env)['cairn_origin'], '')
        env['WORKSPACE_CAIRN_ORIGIN'] = 'http://127.0.0.1:8123'
        with self.assertRaises(ValueError): load(env)
        env['WORKSPACE_CAIRN_LOCAL_TEST'] = '1'
        self.assertEqual(load(env)['cairn_origin'], env['WORKSPACE_CAIRN_ORIGIN'])
        env['WORKSPACE_PUBLIC_URL'] = 'http://127.0.0.1:18000'
        with self.assertRaises(ValueError): load(env)


if __name__ == '__main__':
    unittest.main()
