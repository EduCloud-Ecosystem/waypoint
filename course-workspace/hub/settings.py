"""Validated operator settings; no cloud account or learner-provided configuration."""
import math
import re
from urllib.parse import urlsplit


def load(env):
    def required(key):
        value = env.get(key, '').strip()
        if not value:
            raise ValueError(f'{key} is required')
        return value

    instance = required('WORKSPACE_INSTANCE')
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', instance):
        raise ValueError('WORKSPACE_INSTANCE must be a short lowercase identifier')
    mode = env.get('WORKSPACE_AUTH_MODE', 'oidc')
    if mode not in ('oidc', 'local-test'):
        raise ValueError('WORKSPACE_AUTH_MODE must be oidc or local-test')
    public = urlsplit(required('WORKSPACE_PUBLIC_URL'))
    if public.username or public.password or public.query or public.fragment or public.path not in ('', '/'):
        raise ValueError('WORKSPACE_PUBLIC_URL must be an origin without credentials')
    if mode == 'local-test':
        if public.scheme != 'http' or public.hostname != '127.0.0.1':
            raise ValueError('local-test requires a 127.0.0.1 HTTP origin')
        if len(required('WORKSPACE_TEST_PASSWORD')) < 24:
            raise ValueError('local-test requires a generated password of at least 24 characters')
    elif public.scheme != 'https' or not public.hostname or public.hostname in ('localhost', '127.0.0.1'):
        raise ValueError('oidc requires an HTTPS public origin and wildcard user subdomains')

    cpu = float(env.get('WORKSPACE_CPU_LIMIT', '1'))
    if not math.isfinite(cpu) or cpu <= 0:
        raise ValueError('WORKSPACE_CPU_LIMIT must be positive and finite')
    result = dict(instance=instance, mode=mode, public_url=public.geturl().rstrip('/'), cpu=cpu)
    for key, default in [('MEMORY_MB', 2048), ('ACTIVE_LIMIT', 4), ('IDLE_SECONDS', 1800), ('MAX_AGE_SECONDS', 28800)]:
        value = int(env.get('WORKSPACE_' + key, default))
        if value <= 0:
            raise ValueError('WORKSPACE_' + key + ' must be positive')
        result[key.lower()] = value
    result['image'] = required('WORKSPACE_IMAGE')
    result['home_root'] = env.get('WORKSPACE_HOME_ROOT', '').rstrip('/')
    result['home_quota_mb'] = int(env.get('WORKSPACE_HOME_QUOTA_MB', '0'))
    if result['home_root']:
        if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', result['home_root']) or result['home_quota_mb'] < 16:
            raise ValueError('quota homes require an absolute storage root and at least 16 MiB')
    elif result['home_quota_mb']:
        raise ValueError('WORKSPACE_HOME_ROOT is required with a home quota')
    if mode == 'oidc':
        if not result['home_root']:
            raise ValueError('institutional workspaces require provisioned hard home quotas')
        issuer = urlsplit(required('WORKSPACE_OIDC_ISSUER'))
        if issuer.scheme != 'https' or not issuer.hostname or issuer.username or issuer.password or issuer.query or issuer.fragment:
            raise ValueError('WORKSPACE_OIDC_ISSUER must be a trusted HTTPS Keycloak realm URL')
        result['issuer'] = issuer.geturl().rstrip('/')
        result['client_id'] = required('WORKSPACE_OIDC_CLIENT_ID')
        result['client_secret'] = required('WORKSPACE_OIDC_CLIENT_SECRET')
        result['subjects'] = {s.strip() for s in required('WORKSPACE_ALLOWED_SUBJECTS').split(',') if s.strip()}
        if not result['subjects']:
            raise ValueError('WORKSPACE_ALLOWED_SUBJECTS must contain approved subject IDs')
    else:
        result['test_password'] = required('WORKSPACE_TEST_PASSWORD')
    return result
