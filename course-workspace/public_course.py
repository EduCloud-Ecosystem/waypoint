#!/usr/bin/env python3
"""Render reviewable HTTPS/Keycloak configuration; never enables a public service."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from backup import identifier


def render(domain, issuer, client_id, instance, subjects, home_root, port=18000):
    identifier(instance)
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', domain):
        raise ValueError('use a dedicated lowercase DNS name, without scheme, wildcard or port')
    url = urlsplit(issuer)
    if any(c.isspace() for c in issuer) or url.scheme != 'https' or not url.hostname or url.username or url.password or url.query or url.fragment or not re.fullmatch(r'/realms/[a-zA-Z0-9_-]+', url.path):
        raise ValueError('issuer must be an HTTPS Keycloak realm URL')
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', client_id) or not 1024 <= port <= 65535:
        raise ValueError('invalid client ID or loopback port')
    if not subjects or any(not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,127}', s) for s in subjects):
        raise ValueError('supply stable lowercase subject IDs, not email addresses')
    if not re.fullmatch(r'/[a-zA-Z0-9_/-]+', home_root):
        raise ValueError('supply a dedicated absolute XFS home root')
    origin = 'https://' + domain
    nginx = f'''# Install on a dedicated nginx host; remove its default site first.
map $http_upgrade $educloud_connection {{
    default upgrade;
    '' close;
}}
server {{
    listen 80 default_server;
    server_name _;
    return 444;
}}
server {{
    listen 443 ssl default_server;
    server_name _;
    ssl_reject_handshake on;
    return 444;
}}
server {{
    listen 80;
    server_name {domain} *.{domain};
    return 301 https://$host$request_uri;
}}
server {{
    listen 443 ssl;
    server_name {domain} *.{domain};
    ssl_certificate /etc/educloud/tls/fullchain.pem;
    ssl_certificate_key /etc/educloud/tls/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 100m;
    access_log off;
    location / {{
        proxy_pass http://127.0.0.1:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $educloud_connection;
        proxy_buffering off;
        proxy_read_timeout 3600s;
    }}
}}
'''
    client = {'clientId': client_id, 'name': 'EduCloud course workspace', 'enabled': True,
              'protocol': 'openid-connect', 'publicClient': False, 'clientAuthenticatorType': 'client-secret',
              'standardFlowEnabled': True, 'implicitFlowEnabled': False,
              'directAccessGrantsEnabled': False, 'serviceAccountsEnabled': False,
              'redirectUris': [origin + '/hub/oauth_callback'], 'webOrigins': [origin],
              'attributes': {'pkce.code.challenge.method': 'S256'}}
    env = dict(WORKSPACE_INSTANCE=instance, WORKSPACE_IMAGE='educloud-python-r:2026-10-06',
               WORKSPACE_PORT=str(port), WORKSPACE_AUTH_MODE='oidc', WORKSPACE_PUBLIC_URL=origin,
               WORKSPACE_OIDC_ISSUER=issuer, WORKSPACE_OIDC_CLIENT_ID=client_id,
               WORKSPACE_OIDC_CLIENT_SECRET='', WORKSPACE_ALLOWED_SUBJECTS=','.join(sorted(set(subjects))),
               WORKSPACE_MEMORY_MB='2048', WORKSPACE_CPU_LIMIT='1', WORKSPACE_ACTIVE_LIMIT='4',
               WORKSPACE_IDLE_SECONDS='1800', WORKSPACE_MAX_AGE_SECONDS='28800',
               WORKSPACE_HOME_ROOT=home_root, WORKSPACE_HOME_QUOTA_MB='5120', WORKSPACE_HOME_INODE_LIMIT='100000')
    return nginx, client, env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--issuer', required=True)
    parser.add_argument('--client-id', default='course-workspace')
    parser.add_argument('--instance', required=True)
    parser.add_argument('--subjects-file', type=Path, required=True)
    parser.add_argument('--home-root', required=True)
    parser.add_argument('--port', type=int, default=18000)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        subjects = [s.strip() for s in args.subjects_file.read_text().splitlines() if s.strip()]
        nginx, client, env = render(args.domain, args.issuer, args.client_id, args.instance,
                                    subjects, args.home_root, args.port)
        args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
        env['WORKSPACE_ENV_FILE'] = str((args.output / '.env').resolve())
        files = {'nginx.conf': nginx, 'keycloak-client.json': json.dumps(client, indent=2) + '\n',
                 '.env': ''.join(f'{k}={v}\n' for k, v in env.items())}
        for name, content in files.items():
            with open(args.output / name, 'x', opener=lambda p, f: os.open(p, f, 0o600)) as out:
                out.write(content)
        print('Rendered private configuration. Client secret is intentionally empty: startup remains blocked until configured.')
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Configuration refused: {exc}\n')


if __name__ == '__main__':
    main()
