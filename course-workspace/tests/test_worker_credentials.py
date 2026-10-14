"""Real public-certificate verification and private local approval boundaries."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from authority_test_pki import TestPKI
from test_runtime_authority import RuntimeFixture
import worker_credentials as credentials
from worker_enrollment import EnrollmentStore
from authority_access import publish
from ownership_contract import digest


class CredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name).resolve()
        cls.pki = TestPKI(cls.root / 'pki'); cls.foreign = TestPKI(cls.root / 'foreign')
        cls.leaf, cls.key = cls.pki.issue('client')
    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()
    def refuses(self, callback):
        with self.assertRaises(credentials.CredentialError) as error: callback()
        self.assertEqual(error.exception.code, 'worker_credentials_refused')
    def test_real_client_der_spki_validity_and_exact_pinned_ca(self):
        meta = credentials.verify_certificate(self.leaf, self.pki.ca_cert)
        self.assertEqual(set(meta), {'fingerprint', 'spki_sha256', 'not_before', 'not_after'})
        self.assertEqual(meta['fingerprint'], self.pki.fingerprint(self.leaf))
        self.assertTrue(meta['not_before'] <= int(time.time()) < meta['not_after'])
        self.assertEqual(len(meta['spki_sha256']), 64)
        self.assertEqual(credentials.ca_fingerprint(self.pki.ca_cert), self.pki.fingerprint(self.pki.ca_cert))
        self.refuses(lambda: credentials.verify_certificate(self.leaf, self.foreign.ca_cert))
    def test_server_ca_omitted_eku_expired_future_and_invalid_clock_refuse(self):
        self.refuses(lambda: credentials.verify_certificate(self.pki.server_cert, self.pki.ca_cert))
        self.refuses(lambda: credentials.verify_certificate(self.pki.ca_cert, self.pki.ca_cert))
        meta = credentials.verify_certificate(self.leaf, self.pki.ca_cert)
        for now in (meta['not_before'] - 1, meta['not_after'], True, float('nan')):
            self.refuses(lambda now=now: credentials.verify_certificate(self.leaf, self.pki.ca_cert, clock=lambda: now))
        certificate = self.root / 'no-eku.pem'; extensions = self.root / 'no-eku.extensions'
        extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\n'); extensions.chmod(0o600)
        self.pki.run('x509', '-req', '-days', '2', '-in', self.pki.root / 'client.csr', '-CA', self.pki.ca_cert,
            '-CAkey', self.pki.ca_key, '-set_serial', '0x' + uuid.uuid4().hex, '-extfile', extensions, '-out', certificate)
        certificate.chmod(0o600)
        self.refuses(lambda: credentials.verify_certificate(certificate, self.pki.ca_cert))
    def test_same_key_new_certificate_has_different_der_but_same_spki(self):
        renewed = self.root / 'renewed.pem'
        self.pki.run('x509', '-req', '-days', '2', '-in', self.pki.root / 'client.csr', '-CA', self.pki.ca_cert,
            '-CAkey', self.pki.ca_key, '-set_serial', '0x' + uuid.uuid4().hex, '-extfile', self.pki.root / 'client.extensions', '-out', renewed)
        renewed.chmod(0o600)
        first = credentials.verify_certificate(self.leaf, self.pki.ca_cert); second = credentials.verify_certificate(renewed, self.pki.ca_cert)
        self.assertNotEqual(first['fingerprint'], second['fingerprint']); self.assertEqual(first['spki_sha256'], second['spki_sha256'])
    def test_point_compression_and_explicit_curve_cannot_hide_same_private_key(self):
        original = credentials.verify_certificate(self.leaf, self.pki.ca_cert)
        for encoding, options in (('compressed', ('-conv_form', 'compressed')), ('explicit', ('-param_enc', 'explicit'))):
            key = self.root / (encoding + '.key'); csr = self.root / (encoding + '.csr'); leaf = self.root / (encoding + '.pem')
            self.pki.run('ec', '-in', self.key, *options, '-out', key)
            self.pki.run('req', '-new', '-key', key, '-subj', '/CN=' + encoding, '-out', csr)
            self.pki.run('x509', '-req', '-days', '2', '-in', csr, '-CA', self.pki.ca_cert, '-CAkey', self.pki.ca_key,
                '-set_serial', '0x' + uuid.uuid4().hex, '-extfile', self.pki.root / 'client.extensions', '-out', leaf)
            leaf.chmod(0o600)
            if encoding == 'explicit':
                self.refuses(lambda: credentials.verify_certificate(leaf, self.pki.ca_cert))
                raw, _ = credentials._read_certificate(leaf)
                public = credentials._openssl('x509', '-pubkey', '-noout', raw=raw)
                self.assertEqual(hashlib.sha256(credentials._canonical_spki(public)).hexdigest(), original['spki_sha256'])
            else:
                metadata = credentials.verify_certificate(leaf, self.pki.ca_cert)
                self.assertNotEqual(metadata['fingerprint'], original['fingerprint'])
                self.assertEqual(metadata['spki_sha256'], original['spki_sha256'])
    def test_rsa_encryption_supported_other_curve_and_rsa_pss_refused(self):
        for name, arguments, supported in (('rsa', ('rsa:2048',), True), ('weak-rsa', ('rsa:1024',), False), ('pss', ('rsa-pss',), False),
                                          ('other-curve', ('ec', '-pkeyopt', 'ec_paramgen_curve:secp384r1'), False)):
            key = self.root / (name + '.key'); csr = self.root / (name + '.csr'); leaf = self.root / (name + '.pem')
            self.pki.run('req', '-new', '-newkey', *arguments, '-nodes', '-subj', '/CN=' + name, '-keyout', key, '-out', csr)
            self.pki.run('x509', '-req', '-days', '2', '-in', csr, '-CA', self.pki.ca_cert, '-CAkey', self.pki.ca_key,
                '-set_serial', '0x' + uuid.uuid4().hex, '-extfile', self.pki.root / 'client.extensions', '-out', leaf)
            leaf.chmod(0o600)
            if supported: self.assertEqual(len(credentials.verify_certificate(leaf, self.pki.ca_cert)['spki_sha256']), 64)
            else: self.refuses(lambda: credentials.verify_certificate(leaf, self.pki.ca_cert))

    def test_key_file_header_rejected_without_reading_private_body(self):
        original = os.read; bytes_read = []
        inode = self.key.stat().st_ino
        def read(fd, count):
            raw = original(fd, count)
            if os.fstat(fd).st_ino == inode: bytes_read.append(raw)
            return raw
        with patch.object(credentials.os, 'read', side_effect=read):
            self.refuses(lambda: credentials.verify_certificate(self.key, self.pki.ca_cert))
        self.assertEqual(b''.join(bytes_read).strip(), b'-----BEGIN PRIVATE KEY-----')
    def test_certificate_key_bundle_refuses_before_private_key_body_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve() / 'bundle.pem'
            certificate = self.leaf.read_bytes(); path.write_bytes(certificate + self.key.read_bytes()); path.chmod(0o600)
            inode = path.stat().st_ino; original = os.read; captured = []
            def read(fd, count):
                raw = original(fd, count)
                if os.fstat(fd).st_ino == inode: captured.append(raw)
                return raw
            with patch.object(credentials.os, 'read', side_effect=read):
                self.refuses(lambda: credentials.verify_certificate(path, self.pki.ca_cert))
            self.assertEqual(b''.join(captured), certificate + b'-')

    def test_symlink_hardlink_world_readable_multicert_and_path_swap_refuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); source = root / 'leaf.pem'
            source.symlink_to(self.leaf); self.refuses(lambda: credentials.verify_certificate(source, self.pki.ca_cert)); source.unlink()
            os.link(self.leaf, source)
            try: self.refuses(lambda: credentials.verify_certificate(source, self.pki.ca_cert))
            finally: source.unlink()
            shutil.copyfile(self.leaf, source); source.chmod(0o644)
            self.refuses(lambda: credentials.verify_certificate(source, self.pki.ca_cert))
            source.write_bytes(self.leaf.read_bytes() * 2); source.chmod(0o600)
            self.refuses(lambda: credentials.verify_certificate(source, self.pki.ca_cert))
            source.write_bytes(self.leaf.read_bytes()); real = os.read; swapped = []
            inode = source.stat().st_ino; consumed = [0]
            def read(fd, count):
                raw = real(fd, count)
                if os.fstat(fd).st_ino == inode:
                    consumed[0] += len(raw)
                if os.fstat(fd).st_ino == inode and consumed[0] >= 100 and not swapped:
                    source.rename(root / 'old.pem'); source.symlink_to(self.foreign.ca_cert); swapped.append(True)
                return raw
            with patch.object(credentials.os, 'read', side_effect=read):
                self.refuses(lambda: credentials.verify_certificate(source, self.pki.ca_cert))
            self.assertTrue(swapped)
    def test_cli_errors_hide_certificate_paths_exception_body_and_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); (root / 'sitecustomize.py').write_text(
                "import runtime_authority\ndef fail(*args,**kwargs):raise RuntimeError('PRIVATE_CREDENTIAL_CANARY')\nruntime_authority.read_record=fail\n")
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root), str(Path(credentials.__file__).parent))))
            result = subprocess.run([sys.executable, credentials.__file__, '--runtime-config', '/private/config', '--service-config', '/private/service', 'status'],
                capture_output=True, text=True, timeout=10, env=env)
            self.assertNotEqual(result.returncode, 0); self.assertEqual(result.stdout, '')
            self.assertEqual(result.stderr.strip(), 'worker_credentials_failed')
            self.assertNotIn('PRIVATE_CREDENTIAL_CANARY', result.stderr); self.assertNotIn('Traceback', result.stderr)
    def test_binding_inspects_registered_workers_actual_roots_and_ca_without_core_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700); f = RuntimeFixture(root)
            service = {'authority_directory': str(f.authority.directory), 'ca_file': str(self.pki.ca_cert)}
            with f.authority._connection() as conn: count = conn.execute('SELECT count(*) FROM events').fetchone()[0]
            binding = credentials.verify_binding(f.config, service, backend=f.backend, authority=f.authority)
            self.assertEqual(binding['runtime_config_sha256'], digest(f.config)); self.assertEqual(binding['ca_sha256'], self.pki.fingerprint(self.pki.ca_cert))
            self.assertEqual(binding['workers']['worker-one']['courses'], ['course-main', 'course-peer'])
            with f.authority._connection() as conn: self.assertEqual(conn.execute('SELECT count(*) FROM events').fetchone()[0], count)
            path = Path(f.config['courses']['worker-one']['course-main']['home_root']); path.rename(path.with_name(path.name + '-old')); path.mkdir(mode=0o700)
            self.refuses(lambda: credentials.verify_binding(f.config, service, backend=f.backend, authority=f.authority))
    def test_runtime_binding_is_fresh_scoped_and_has_no_sql_or_crypto_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700); f = RuntimeFixture(root)
            service = {'authority_directory': str(f.authority.directory), 'ca_file': str(self.pki.ca_cert)}
            binding = credentials.verify_binding(f.config, service, backend=f.backend, authority=f.authority)
            with patch.object(f.authority, '_connection', side_effect=AssertionError('no ownership read')), patch.object(credentials, '_openssl', side_effect=AssertionError('no subprocess')):
                credentials.verify_runtime_binding(f.config, service, f.backend, binding, 'worker-one', 'course-main')
                path = Path(f.config['courses']['worker-one']['course-main']['home_root'])
                path.rename(path.with_name(path.name + '-old')); path.mkdir(mode=0o700)
                self.refuses(lambda: credentials.verify_runtime_binding(f.config, service, f.backend, binding, 'worker-one', 'course-main'))
                credentials.verify_runtime_binding(f.config, service, f.backend, binding, 'worker-one', 'course-peer')
                wrong_ca = dict(service, ca_file=str(self.foreign.ca_cert))
                self.refuses(lambda: credentials.verify_runtime_binding(f.config, wrong_ca, f.backend, binding, 'worker-one', 'course-peer'))
                f.client.info_value['ID'] = 'changed-daemon'
                self.refuses(lambda: credentials.verify_runtime_binding(f.config, service, f.backend, binding, 'worker-one', 'course-peer'))

    def test_public_ca_snapshot_reads_current_bytes_with_bounded_syscalls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700); f = RuntimeFixture(root)
            ca = root / 'public-ca.pem'; shutil.copyfile(self.pki.ca_cert, ca); ca.chmod(0o600)
            service = {'authority_directory': str(f.authority.directory), 'ca_file': str(ca)}
            binding = credentials.verify_binding(f.config, service, backend=f.backend, authority=f.authority)
            verify = credentials.make_runtime_binding_verifier(f.config, service, f.backend, binding)
            original = os.read; counts = []; inode = ca.stat().st_ino
            def read(fd, count):
                if os.fstat(fd).st_ino == inode: counts.append(count)
                return original(fd, count)
            with patch.object(credentials.os, 'read', side_effect=read), patch.object(credentials, '_openssl', side_effect=AssertionError('no hot crypto')), patch.object(f.authority, '_connection', side_effect=AssertionError('no hot SQL')):
                verify('worker-one', 'course-main')
            self.assertLessEqual(len(counts), 40); self.assertIn(ca.stat().st_size - 28, counts)
            raw = ca.read_bytes(); changed = bytearray(raw); changed[100] = ord('A') if changed[100] != ord('A') else ord('B')
            ca.write_bytes(changed)
            self.refuses(lambda: verify('worker-one', 'course-main'))
            ca.write_bytes(raw); verify('worker-one', 'course-main')
            path = Path(f.config['courses']['worker-one']['course-main']['home_root'])
            path.rename(path.with_name(path.name + '-old')); path.mkdir(mode=0o700)
            self.refuses(lambda: verify('worker-one', 'course-main'))
            verify('worker-one', 'course-peer')
            f.client.info_value['ID'] = 'changed-daemon'
            self.refuses(lambda: verify('worker-one', 'course-peer'))

    def test_public_ca_snapshots_are_policy_local_and_reformat_is_not_cached_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700); f = RuntimeFixture(root)
            ca = root / 'public-ca.pem'; raw = self.pki.ca_cert.read_bytes(); ca.write_bytes(raw); ca.chmod(0o600)
            service = {'authority_directory': str(f.authority.directory), 'ca_file': str(ca)}
            binding = credentials.verify_binding(f.config, service, backend=f.backend, authority=f.authority)
            first = credentials.make_runtime_binding_verifier(f.config, service, f.backend, binding)
            ca.write_bytes(raw.replace(b'\n', b'\r\n'))
            self.refuses(lambda: first('worker-one', 'course-main'))
            second = credentials.make_runtime_binding_verifier(f.config, service, f.backend, binding)
            second('worker-one', 'course-main')
            ca.write_bytes(raw)
            first('worker-one', 'course-main'); self.refuses(lambda: second('worker-one', 'course-main'))
            wrong = dict(binding, ca_sha256='0' * 64)
            self.refuses(lambda: credentials.make_runtime_binding_verifier(f.config, service, f.backend, wrong))

    def test_public_ca_hot_reader_refuses_bundle_key_mode_links_and_path_swap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); root.chmod(0o700)
            ca = root / 'ca.pem'; raw = self.pki.ca_cert.read_bytes(); ca.write_bytes(raw); ca.chmod(0o600)
            fingerprint, approved, der = credentials._verified_public_ca(ca)
            read = lambda: credentials._read_pinned_ca(ca, fingerprint, (approved, der))
            original = os.read; captured = []
            def tracked(fd, count):
                result = original(fd, count); captured.append(result); return result
            ca.write_bytes(raw + self.key.read_bytes())
            with patch.object(credentials.os, 'read', side_effect=tracked): self.refuses(read)
            self.assertEqual(captured, [])
            key_header = b'-----BEGIN PRIVATE KEY-----\n'
            ca.write_bytes(key_header + b'X' * (len(raw) - len(key_header)))
            with patch.object(credentials.os, 'read', side_effect=tracked): self.refuses(read)
            self.assertEqual(b''.join(captured), key_header)
            ca.write_bytes(raw); ca.chmod(0o644); self.refuses(read); ca.chmod(0o600)
            os.link(ca, root / 'alias'); self.refuses(read); (root / 'alias').unlink()
            ca.rename(root / 'original'); ca.symlink_to(root / 'original'); self.refuses(read)
            ca.unlink(); (root / 'original').rename(ca)
            swapped = []
            def swap(fd, count):
                result = original(fd, count)
                if count > 1 and not swapped:
                    ca.rename(root / 'old'); ca.symlink_to(self.foreign.ca_cert); swapped.append(True)
                return result
            with patch.object(credentials.os, 'read', side_effect=swap): self.refuses(read)
            self.assertTrue(swapped)

    def test_public_ca_hot_reader_detects_same_inode_drift_and_handles_short_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory).resolve() / 'ca.pem'; raw = self.pki.ca_cert.read_bytes(); ca.write_bytes(raw); ca.chmod(0o600)
            fingerprint, approved, der = credentials._verified_public_ca(ca)
            original = os.read; changed = []
            def drift(fd, count):
                result = original(fd, count)
                if count > 1 and not changed:
                    ca.write_bytes(raw); changed.append(True)
                return result
            with patch.object(credentials.os, 'read', side_effect=drift):
                self.refuses(lambda: credentials._read_pinned_ca(ca, fingerprint, (approved, der)))
            with patch.object(credentials.os, 'read', side_effect=lambda fd, count: original(fd, min(count, 7))):
                self.assertEqual(credentials._read_pinned_ca(ca, fingerprint, (approved, der)), der)

    def test_same_size_malformed_ca_key_bundle_never_returns_or_changes_approved_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory).resolve() / 'ca.pem'; original = self.pki.ca_cert.read_bytes()
            ca.write_bytes(original); ca.chmod(0o600)
            fingerprint, approved, der = credentials._verified_public_ca(ca)
            snapshot = (approved, der)
            shorter = b'-----BEGIN CERTIFICATE-----\nQUJD\n-----END CERTIFICATE-----\n'
            key = self.key.read_bytes(); canary = b'PRIVATE_BUNDLE_CANARY'
            malformed = shorter + key + canary
            self.assertLess(len(malformed), len(original))
            ca.write_bytes(malformed + b' ' * (len(original) - len(malformed)))
            self.assertEqual(ca.stat().st_size, len(approved))
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.refuses(lambda: credentials._read_pinned_ca(ca, fingerprint, snapshot))
            self.assertEqual(output.getvalue(), '')
            self.assertEqual(snapshot, (original, der))
            ca.write_bytes(original)
            self.assertEqual(credentials._read_pinned_ca(ca, fingerprint, snapshot), der)



class LocalApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name).resolve()
        self.pki = TestPKI(self.root / 'pki'); self.authority_id = uuid.uuid4().hex
        self.binding = {'runtime_config_sha256': 'a' * 64, 'ca_sha256': self.pki.fingerprint(self.pki.ca_cert), 'home_root_bindings_sha256': 'b' * 64,
            'workers': {'one': {'provider_id': 'provider-one', 'boot_id': 'c' * 32, 'evidence_sha256': 'd' * 64, 'courses': ['course']}}}
        self.directory = self.root / 'enrollment'
        self.config = {'authority_id': self.authority_id}; self.service = {'enrollment_directory': str(self.directory), 'ca_file': str(self.pki.ca_cert), 'acl_file': str(self.root / 'acl.json')}
        self.operator, _ = self.pki.issue('operator')
        publish(self.root / 'acl.json', {'version': 'educloud-authority-access-v1', 'authority_id': self.authority_id,
            'clients': {self.pki.fingerprint(self.operator): {'role': 'operator', 'workers': ['one'], 'courses': ['course']}}})
        publish(self.root / 'config.json', self.config); publish(self.root / 'service.json', self.service)
        self.common = {'runtime_config': str(self.root / 'config.json'), 'service_config': str(self.root / 'service.json')}
        with patch.object(credentials, 'verify_binding', return_value=self.binding): credentials.execute(argparse.Namespace(command='create', **self.common))
    def run_action(self, args):
        with patch.object(credentials, 'verify_binding', return_value=self.binding): return credentials.execute(argparse.Namespace(**self.common, **args))
    def preview(self, action, sources=None, slot=None, duration=None):
        source = self.root / (uuid.uuid4().hex + '.json'); output = self.root / (uuid.uuid4().hex + '.plan')
        if sources is not None: publish(source, sources)
        result = self.run_action({'command': 'preview', 'action': action, 'worker': 'one', 'slot': slot,
            'credentials': str(source) if sources is not None else None, 'target_fingerprint': None, 'overlap_seconds': duration, 'output': str(output)})
        return result, output
    def apply(self, result, output):
        return self.run_action({'command': 'apply', 'plan_file': str(output), 'review_sha256': result['review_sha256']})
    def enroll(self):
        hub, _ = self.pki.issue('hub'); guardian, _ = self.pki.issue('guardian')
        result, output = self.preview('enroll', {'guardian': str(guardian), 'hub:course': str(hub)})
        self.apply(result, output); return hub
    def test_preview_apply_and_activation_require_exact_fresh_certificate(self):
        hub = self.enroll(); new, _ = self.pki.issue('new'); other, _ = self.pki.issue('other')
        result, output = self.preview('stage', {'hub:course': str(new)}, 'hub:course'); self.apply(result, output)
        with self.assertRaises(credentials.CredentialError): self.preview('activate', {'hub:course': str(other)}, 'hub:course', 10)
        result, output = self.preview('activate', {'hub:course': str(new)}, 'hub:course', 10)
        shutil.copyfile(other, new)
        with self.assertRaises(credentials.CredentialError): self.apply(result, output)
        store = EnrollmentStore.open(self.directory, self.authority_id, self.binding)
        self.assertIn(self.pki.fingerprint(hub), store.entries()); self.assertNotIn(self.pki.fingerprint(other), store.entries())
    def test_actual_cli_parser_review_and_apply_keep_output_bounded(self):
        hub, _ = self.pki.issue('cli-hub'); guardian, _ = self.pki.issue('cli-guardian')
        sources = self.root / 'sources.json'; output = self.root / 'review.json'
        publish(sources, {'guardian': str(guardian), 'hub:course': str(hub)})
        runner = ('import worker_credentials as m\nm.verify_binding=lambda *args,**kwargs:' + repr(self.binding) + '\ntry:m.main()\nexcept (Exception,KeyboardInterrupt):raise SystemExit(\"worker_credentials_failed\") from None\n')
        env = dict(os.environ, PYTHONPATH=str(Path(credentials.__file__).parent))
        base = [sys.executable, '-c', runner, '--runtime-config', self.common['runtime_config'], '--service-config', self.common['service_config']]
        preview = subprocess.run(base + ['preview', '--action', 'enroll', '--worker', 'one', '--credentials', str(sources), '--output', str(output)],
            capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(preview.returncode, 0, preview.stderr); result = json.loads(preview.stdout)
        self.assertEqual(result['status'], 'PLAN_READY'); self.assertNotIn(str(self.root), preview.stdout)
        applied = subprocess.run(base + ['apply', '--plan-file', str(output), '--review-sha256', result['review_sha256']],
            capture_output=True, text=True, timeout=15, env=env)
        self.assertEqual(applied.returncode, 0, applied.stderr); self.assertEqual(json.loads(applied.stdout)['status'], 'COMPLETE')
        replay = subprocess.run(base + ['apply', '--plan-file', str(output), '--review-sha256', result['review_sha256']],
            capture_output=True, text=True, timeout=15, env=env)
        self.assertNotEqual(replay.returncode, 0); self.assertEqual(replay.stderr.strip(), 'worker_credentials_failed')
        self.assertEqual(replay.stdout, '')

    def test_changed_review_file_and_static_operator_leaf_cannot_be_imported(self):
        with self.assertRaises(credentials.CredentialError): self.preview('enroll', {'guardian': str(self.operator), 'hub:course': str(self.operator)})
        hub, _ = self.pki.issue('hub'); guardian, _ = self.pki.issue('guardian')
        result, output = self.preview('enroll', {'guardian': str(guardian), 'hub:course': str(hub)})
        record = json.loads(output.read_text()); record['sources']['guardian'] = str(hub); output.write_text(json.dumps(record))
        with self.assertRaises(credentials.CredentialError): self.apply(result, output)
        self.assertEqual(EnrollmentStore.open(self.directory, self.authority_id, self.binding).entries(), {})


if __name__ == '__main__': unittest.main()
