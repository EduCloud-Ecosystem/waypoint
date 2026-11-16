"""Operator-only roster profile. No identity minting or browser bearer credentials."""
import base64
import json
import os
from pathlib import Path
import re
import stat
import time

from waypoint_cairn import open_root, open_beneath

KEYS = {'version', 'issuer', 'audience', 'subject', 'institution_id', 'class_id',
        'learner_id', 'grant_generation', 'authority_id', 'authorization_file',
        'credential_file', 'expires_at', 'assistance', 'exercise_id', 'exercise_version', 'assignment_title'}


def private_bytes(path, limit):
    path = Path(path)
    root = open_root(path.parent)
    try:
        fd = open_beneath(root, [path.name])
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > limit:
                raise ValueError('private bounded regular file required')
            with os.fdopen(fd, 'rb', closefd=False) as f:
                body = f.read(limit + 1)
            if len(body) > limit:
                raise ValueError('file too large')
            return body
        finally:
            os.close(fd)
    finally:
        os.close(root)


def load(path):
    value = json.loads(private_bytes(path, 8192))
    if not isinstance(value, dict) or set(value) != KEYS or type(value['version']) is not int or value['version'] != 1:
        raise ValueError('unsupported roster profile')
    for key in ('learner_id', 'grant_generation', 'authority_id'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[0-9a-f]{32}', value[key]):
            raise ValueError('explicit roster identity required')
    for key in ('exercise_id', 'exercise_version'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', value[key]):
            raise ValueError('explicit assignment required')
    if not isinstance(value['assignment_title'], str) or not 1 <= len(value['assignment_title']) <= 120:
        raise ValueError('assignment title required')
    for key in ('institution_id', 'class_id'):
        if not isinstance(value[key], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value[key]):
            raise ValueError('invalid course binding')
    if (not isinstance(value['subject'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value['subject'])
            or not isinstance(value['issuer'], str) or not value['issuer'].startswith('https://')
            or not isinstance(value['audience'], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', value['audience'])
            or type(value['expires_at']) is not int or not time.time() < value['expires_at'] <= time.time() + 900
            or value['assistance'] not in ('hints', 'disabled')):
        raise ValueError('invalid finite profile')
    for key in ('authorization_file', 'credential_file'):
        if not isinstance(value[key], str) or not Path(value[key]).is_absolute():
            raise ValueError('absolute operator file required')
    value['_path'] = str(path)
    return value


def refresh(profile):
    fresh = load(profile['_path'])
    if any(fresh[k] != profile[k] for k in KEYS - {'assistance'}):
        raise ValueError('profile changed; operator rebind required')
    return fresh


def current(profile, exercise, version, *, now=None):
    now = time.time() if now is None else now
    value = json.loads(private_bytes(profile['authorization_file'], 1024 * 1024))
    a = value['authority']
    if (a['schema_version'] != 1 or a['authority_id'] != profile['authority_id']
            or a['issuer'] != profile['issuer'] or a['audience'] != profile['audience']
            or any(type(a[k]) is not int for k in ('revision', 'issued_at', 'expires_at'))
            or a['revision'] < 1 or not 0 < a['expires_at'] - a['issued_at'] <= 300
            or not a['issued_at'] <= now < min(a['expires_at'], profile['expires_at'])
            or value['exercise_versions'].get(exercise) != version):
        raise ValueError('current course authority unavailable')
    members = [m for m in value['subjects'].get(profile['subject'], [])
               if m['institution_id'] == profile['institution_id'] and m['class_id'] == profile['class_id']]
    if len(members) != 1:
        raise ValueError('current course membership unavailable')
    m = members[0]
    if (m['learner_id_kind'] != 'cairn_roster_v1' or m['learner_id'] != profile['learner_id']
            or m['grant_generation'] != profile['grant_generation'] or m['assignments'].get(exercise) != version):
        raise ValueError('current roster grant unavailable')
    # Belay still verifies the signature and durable authority watermark on EVERY
    # dispatch/status request. This local observation never replaces that check.
    return min(a['expires_at'], profile['expires_at'])


def credential(profile, exercise, version, *, now=None):
    now = time.time() if now is None else now
    profile = refresh(profile)
    current(profile, exercise, version, now=now)
    if profile['assistance'] != 'hints':
        raise PermissionError('assistance disabled by instructor')
    token = private_bytes(profile['credential_file'], 16384).decode().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', token):
        raise ValueError('credential unavailable')
    encoded = token.split('.')[1]
    claims = json.loads(base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True))
    if (claims.get('sub') != profile['subject'] or claims.get('iss') != profile['issuer']
            or claims.get('aud') != profile['audience']
            or type(claims.get('iat')) is not int or type(claims.get('exp')) is not int
            or not claims['iat'] <= now < claims['exp']
            or not 0 < claims['exp'] - claims['iat'] <= 300):
        raise ValueError('finite credential unavailable')
    return token
