"""Private local verification of public credentials; never signs private keys."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import tempfile
import time

MAX_CERTIFICATE = 64 * 1024


class CredentialError(ValueError):
    def __init__(self, code): self.code = code; super().__init__(code)


def _refuse(): raise CredentialError('worker_credentials_refused')


def _read_certificate(path):
    from runtime_authority import open_directory
    path = Path(path)
    if not path.is_absolute(): _refuse()
    parent = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            initial = os.fstat(fd)
            if (not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_uid != os.geteuid()
                    or initial.st_mode & 0o077 or not 1 <= initial.st_size <= MAX_CERTIFICATE): _refuse()
            # Identify the public certificate header before reading its body;
            # accidentally selecting a key file never reads private key data.
            header = bytearray()
            while len(header) < 128:
                byte = os.read(fd, 1)
                if not byte: break
                header.extend(byte)
                if byte == b'\n': break
            if bytes(header).strip() != b'-----BEGIN CERTIFICATE-----': _refuse()
            certificate = bytearray(header)
            ending = b'-----END CERTIFICATE-----'
            while not certificate.endswith(ending):
                byte = os.read(fd, 1)
                if not byte or len(certificate) >= MAX_CERTIFICATE: _refuse()
                certificate.extend(byte)
            # Once the public PEM ends, never consume another material body.
            # A key/certificate bundle refuses at its first nonwhite byte.
            while True:
                byte = os.read(fd, 1)
                if not byte: break
                if byte not in b' \t\r\n' or len(certificate) >= MAX_CERTIFICATE: _refuse()
                certificate.extend(byte)
            raw = bytes(certificate)
            after = os.fstat(fd); named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            def identity(info):
                return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if len(raw) != initial.st_size or identity(initial) != identity(after) or identity(initial) != identity(named): _refuse()
        finally: os.close(fd)
    finally: os.close(parent)
    match = re.fullmatch(rb'\s*-----BEGIN CERTIFICATE-----\s*([A-Za-z0-9+/=\r\n]+)\s*-----END CERTIFICATE-----\s*', raw)
    if match is None: _refuse()
    der = base64.b64decode(re.sub(rb'\s+', b'', match[1]), validate=True)
    return raw, der


def _read_pinned_ca_bytes(path, fingerprint, approved):
    """Fresh exact public-byte proof with bounded reads after startup validation."""
    from runtime_authority import open_directory
    expected, der = approved
    path = Path(path)
    if not path.is_absolute(): _refuse()
    parent = open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            initial = os.fstat(fd)
            if (not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_uid != os.geteuid()
                    or initial.st_mode & 0o077 or initial.st_size != len(expected)): _refuse()
            # Reject a selected key header before reading any material body.
            header = bytearray()
            while len(header) < 128:
                byte = os.read(fd, 1)
                if not byte: break
                header.extend(byte)
                if byte == b'\n': break
            if bytes(header).strip() != b'-----BEGIN CERTIFICATE-----': _refuse()
            raw = bytearray(header)
            while len(raw) < len(expected):
                chunk = os.read(fd, len(expected) - len(raw))
                if not chunk: _refuse()
                raw.extend(chunk)
            if os.read(fd, 1) or bytes(raw) != expected: _refuse()
            after = os.fstat(fd); named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            def identity(info):
                return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                        info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity(initial) != identity(after) or identity(initial) != identity(named): _refuse()
        finally: os.close(fd)
    finally: os.close(parent)
    if hashlib.sha256(der).hexdigest() != fingerprint: _refuse()
    return der


def _read_pinned_ca(path, fingerprint, approved):
    try:
        return _read_pinned_ca_bytes(path, fingerprint, approved)
    except CredentialError: raise
    except Exception: raise CredentialError('worker_credentials_refused') from None


def _openssl(*arguments, raw):
    executable = shutil.which('openssl')
    if executable is None: _refuse()
    result = subprocess.run([executable, *map(str, arguments)], input=raw, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=5, umask=0o077,
        env={'PATH': os.defpath, 'LANG': 'C', 'LC_ALL': 'C', 'OPENSSL_CONF': '/dev/null'})
    if result.returncode or len(result.stdout) > MAX_CERTIFICATE or len(result.stderr) > MAX_CERTIFICATE: _refuse()
    return result.stdout


def _canonical(raw, der):
    canonical = _openssl('x509', '-outform', 'DER', raw=raw)
    if canonical != der: _refuse()
    return hashlib.sha256(der).hexdigest()


def _canonical_spki(public):
    standard = _openssl('pkey', '-pubin', '-outform', 'DER', raw=public)
    # Inspect only the bounded outer sequence produced by OpenSSL, rather than
    # implementing a general certificate/ASN.1 parser. rsaEncryption has one
    # canonical AlgorithmIdentifier; PSS/other algorithms cannot use this arm.
    if len(standard) < 2 or standard[0] != 0x30: _refuse()
    width = standard[1]
    if width < 128:
        offset, size = 2, width
    else:
        count = width & 127
        if not 1 <= count <= 3 or len(standard) < 2 + count: _refuse()
        offset, size = 2 + count, int.from_bytes(standard[2:2 + count], 'big')
    if size != len(standard) - offset: _refuse()
    rsa = bytes.fromhex('300d06092a864886f70d0101010500')
    if standard[offset:offset + len(rsa)] == rsa:
        return standard
    canonical = _openssl('pkey', '-pubin', '-outform', 'DER', '-ec_conv_form', 'uncompressed',
                         '-ec_param_enc', 'named_curve', raw=public)
    p256 = bytes.fromhex('3059301306072a8648ce3d020106082a8648ce3d03010703420004')
    if len(canonical) != 91 or not canonical.startswith(p256): _refuse()
    return canonical


def _verified_public_ca(ca_file):
    try:
        raw, der = _read_certificate(ca_file)
        fingerprint = _canonical(raw, der)
        constraints = _openssl('x509', '-noout', '-ext', 'basicConstraints,keyUsage', raw=raw).decode('ascii')
        if not re.search(r'\bCA:TRUE\b', constraints) or 'Certificate Sign' not in constraints: _refuse()
        return fingerprint, raw, der
    except CredentialError: raise
    except Exception: raise CredentialError('worker_credentials_refused') from None


def ca_fingerprint(ca_file):
    return _verified_public_ca(ca_file)[0]


def verify_certificate(cert_file, ca_file, *, clock=time.time):
    try:
        now = clock()
        if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 2**53: _refuse()
        now = int(now)
        leaf, der = _read_certificate(cert_file); ca, ca_der = _read_certificate(ca_file)
        fingerprint = _canonical(leaf, der); _canonical(ca, ca_der)
        ca_constraints = _openssl('x509', '-noout', '-ext', 'basicConstraints,keyUsage', raw=ca).decode('ascii')
        if not re.search(r'\bCA:TRUE\b', ca_constraints) or 'Certificate Sign' not in ca_constraints: _refuse()
        metadata = _openssl('x509', '-noout', '-ext', 'basicConstraints,extendedKeyUsage', '-dates', raw=leaf).decode('ascii')
        if (not re.search(r'\bCA:FALSE\b', metadata) or re.search(r'\bCA:TRUE\b', metadata)
                or 'TLS Web Client Authentication' not in metadata): _refuse()
        dates = {}
        for line in metadata.splitlines():
            if line.startswith(('notBefore=', 'notAfter=')):
                key, value = line.split('=', 1)
                if key in dates: _refuse()
                dates[key] = int(ssl.cert_time_to_seconds(value))
        if set(dates) != {'notBefore', 'notAfter'} or not dates['notBefore'] <= now < dates['notAfter']: _refuse()
        public = _openssl('x509', '-pubkey', '-noout', raw=leaf)
        spki = _canonical_spki(public)
        # Only frozen public certificate bytes reach OpenSSL; source path swaps
        # cannot redirect verification. No private keys or signing CA are read.
        with tempfile.TemporaryDirectory(prefix='worker-certificate-') as directory:
            pin = Path(directory) / 'pinned-ca.pem'
            fd = os.open(pin, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream: stream.write(ca)
            _openssl('verify', '-trusted', pin, '-no-CAfile', '-no-CApath', '-no-CAstore',
                '-purpose', 'sslclient', '-x509_strict', '-auth_level', '2', '-attime', now, raw=leaf)
        return {'fingerprint': fingerprint, 'spki_sha256': hashlib.sha256(spki).hexdigest(),
                'not_before': dates['notBefore'], 'not_after': dates['notAfter']}
    except CredentialError: raise
    except Exception: raise CredentialError('worker_credentials_refused') from None


def verify_binding(config, service, *, backend=None, authority=None):
    """Fresh configured-root and registered-worker proof; no ownership writes."""
    try:
        from ownership_contract import Authority, digest
        from runtime_authority import DockerBackend, open_directory
        backend = backend if backend is not None else DockerBackend(config)
        authority = authority if authority is not None else Authority.open(service['authority_directory'], config['authority_id'], backend)
        if backend.config != config or backend.authority_id != config['authority_id'] or authority.authority_id != config['authority_id']: _refuse()
        observations = {worker: backend.inspect_worker(worker) for worker in sorted(config['workers'])}
        roots = {}
        for worker, courses in config['courses'].items():
            for course, item in courses.items():
                fd = open_directory(item['home_root'])
                try:
                    info = os.fstat(fd); pair = [info.st_dev, info.st_ino]
                    if pair != backend.home_bindings[worker + '/' + course]: _refuse()
                    roots[worker + '/' + course] = pair
                finally: os.close(fd)
        ca = ca_fingerprint(service['ca_file'])
        workers = {}
        with authority._connection() as connection:
            connection.execute('BEGIN'); authority._validate(connection)
            for worker, current in observations.items():
                saved = authority._worker(connection, worker, current)
                courses = sorted(config['courses'][worker])
                for course in courses: authority._course(connection, course)
                workers[worker] = {'provider_id': saved['provider_id'], 'boot_id': saved['boot_id'],
                                   'evidence_sha256': digest(saved), 'courses': courses}
            connection.execute('COMMIT')
        return {'runtime_config_sha256': digest(config), 'ca_sha256': ca,
                'home_root_bindings_sha256': digest(roots), 'workers': workers}
    except CredentialError: raise
    except Exception: raise CredentialError('worker_credentials_refused') from None


def _verify_runtime_binding(config, service, backend, binding, worker_id, course_id, public_ca=None):
    """Fresh scoped metadata under runtime exclusion; never reads authority SQL."""
    try:
        from ownership_contract import digest
        from runtime_authority import open_directory
        if (config != backend.config or digest(config) != binding['runtime_config_sha256']
                or digest(backend.home_bindings) != binding['home_root_bindings_sha256']): _refuse()
        approved = binding['workers'][worker_id]
        if course_id not in approved['courses']: _refuse()
        current = backend.inspect_worker(worker_id)  # Fresh daemon identity/budget proof.
        if (current['provider_id'] != approved['provider_id'] or current['boot_id'] != approved['boot_id']
                or digest(current) != approved['evidence_sha256']): _refuse()
        # The pinned CA was cryptographically/canonically checked at startup.
        # Exact DER equality here needs no subprocess or alternate trust roots.
        if public_ca is None:
            _, der = _read_certificate(service['ca_file'])
        else:
            der = _read_pinned_ca(service['ca_file'], binding['ca_sha256'], public_ca)
        if hashlib.sha256(der).hexdigest() != binding['ca_sha256']: _refuse()
        root = open_directory(config['courses'][worker_id][course_id]['home_root'])
        try:
            info = os.fstat(root)
            if [info.st_dev, info.st_ino] != backend.home_bindings[worker_id + '/' + course_id]: _refuse()
        finally: os.close(root)
    except CredentialError: raise
    except Exception: raise CredentialError('worker_credentials_refused') from None


def verify_runtime_binding(config, service, backend, binding, worker_id, course_id):
    _verify_runtime_binding(config, service, backend, binding, worker_id, course_id)


def make_runtime_binding_verifier(config, service, backend, binding):
    """Pin immutable public CA bytes; every invocation re-proves current bytes.

    This snapshot holds no registry/authorization/liveness state. PEM formatting
    changes safely require a newly constructed policy, even with identical DER.
    """
    fingerprint, raw, der = _verified_public_ca(service['ca_file'])
    if fingerprint != binding['ca_sha256']: _refuse()
    public_ca = (raw, der)
    return lambda worker, course: _verify_runtime_binding(config, service, backend, binding, worker, course, public_ca)


def _certificate_sources(path, slots, config, service):
    from authority_access import AccessPolicy, read_private
    sources = read_private(Path(path))
    if (not isinstance(sources, dict) or set(sources) != set(slots)
            or any(not isinstance(value, str) or not Path(value).is_absolute() for value in sources.values())): _refuse()
    static = AccessPolicy(service['acl_file'], config['authority_id'])._read()
    operators = {fingerprint for fingerprint, entry in static.items() if entry['role'] == 'operator'}
    metadata = {slot: verify_certificate(cert, service['ca_file']) for slot, cert in sources.items()}
    if any(cert['fingerprint'] in operators for cert in metadata.values()): _refuse()
    return sources, metadata


def execute(args):
    from authority_access import AccessPolicy, publish, read_private, HEX64
    from ownership_contract import digest
    from runtime_authority import read_record
    from worker_enrollment import EnrollmentStore
    config = read_record(args.runtime_config); service = read_record(args.service_config)
    directory = service.get('enrollment_directory')
    if not isinstance(directory, str) or not Path(directory).is_absolute(): _refuse()
    binding = verify_binding(config, service)
    if args.command == 'create':
        EnrollmentStore.create(directory, config['authority_id'], binding)
        return {'status': 'CREATED', 'authority_id': config['authority_id']}
    store = EnrollmentStore.open(directory, config['authority_id'], binding)
    if args.command == 'status':
        return {'status': 'INSPECTED', 'configured_workers': len(binding['workers']), 'live_credentials': len(store.entries())}
    if args.command == 'preview':
        if args.worker not in binding['workers']: _refuse()
        slots = {'guardian'} | {'hub:' + course for course in binding['workers'][args.worker]['courses']}
        sources = {}; metadata = None
        if args.action in ('enroll', 'stage', 'activate'):
            expected = slots if args.action == 'enroll' else {args.slot}
            if args.credentials is None or None in expected: _refuse()
            sources, actual = _certificate_sources(args.credentials, expected, config, service)
            metadata = actual if args.action == 'enroll' else actual[args.slot] if args.action == 'stage' else None
        elif args.credentials is not None: _refuse()
        summary = store.preview(args.action, args.worker, credentials=metadata, slot=args.slot,
            target_fingerprint=args.target_fingerprint, overlap_seconds=args.overlap_seconds)
        approved = store.certificates_for_plan(summary['plan_id'], summary['plan_sha256'])
        if sources and actual != approved: _refuse()
        record = {'version': 'educloud-local-credential-review-v1', 'authority_id': config['authority_id'],
                  'plan_id': summary['plan_id'], 'plan_sha256': summary['plan_sha256'], 'sources': sources}
        publish(Path(args.output), record)
        return dict(summary, review_sha256=digest(record))
    if args.command == 'apply':
        record = read_private(Path(args.plan_file))
        if (not isinstance(args.review_sha256, str) or not HEX64.fullmatch(args.review_sha256)
                or not isinstance(record, dict) or set(record) != {'version', 'authority_id', 'plan_id', 'plan_sha256', 'sources'}
                or record['version'] != 'educloud-local-credential-review-v1' or record['authority_id'] != config['authority_id']
                or digest(record) != args.review_sha256): _refuse()
        approved = store.certificates_for_plan(record['plan_id'], record['plan_sha256'])
        if not isinstance(record['sources'], dict) or set(record['sources']) != set(approved): _refuse()
        if approved:
            # A reviewed source path and its exact leaf/key identity must still
            # verify; replacing a leaf cannot silently rewrite the approval.
            static = AccessPolicy(service['acl_file'], config['authority_id'])._read()
            operators = {fp for fp, entry in static.items() if entry['role'] == 'operator'}
            actual = {slot: verify_certificate(path, service['ca_file']) for slot, path in record['sources'].items()}
            if actual != approved or any(cert['fingerprint'] in operators for cert in actual.values()): _refuse()
        return store.apply(record['plan_id'], record['plan_sha256'], lambda: verify_binding(config, service))
    _refuse()


def main():
    parser = argparse.ArgumentParser(description='Review credentials for already-configured local workers')
    parser.add_argument('--runtime-config', required=True); parser.add_argument('--service-config', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('create'); commands.add_parser('status')
    preview = commands.add_parser('preview')
    preview.add_argument('--action', choices=('enroll', 'stage', 'activate', 'retire', 'revoke', 'revoke_worker'), required=True)
    preview.add_argument('--worker', required=True); preview.add_argument('--credentials')
    preview.add_argument('--slot'); preview.add_argument('--target-fingerprint'); preview.add_argument('--overlap-seconds', type=int)
    preview.add_argument('--output', required=True)
    apply = commands.add_parser('apply')
    apply.add_argument('--plan-file', required=True); apply.add_argument('--review-sha256', required=True)
    args = parser.parse_args()
    print(json.dumps(execute(args), sort_keys=True), flush=True)


if __name__ == '__main__':
    try: main()
    except (Exception, KeyboardInterrupt): raise SystemExit('worker_credentials_failed') from None
