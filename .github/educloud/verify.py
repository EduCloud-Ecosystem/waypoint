"""Offline verification of the shared pilot development contract (stdlib only)."""
import hashlib
import json
from pathlib import Path
import sys

VERSION = 'educloud-pilot-v1'
REPOSITORIES = {'educloud', 'belay-classroom', 'cairn', 'waypoint', 'portage', 'outfitter'}


def verify(root):
    config = json.loads((root / '.github/educloud/component.json').read_text())
    if config.get('protocol_version') != VERSION or config.get('repository') not in REPOSITORIES:
        raise ValueError('Unknown protocol version or component')
    policy = root / 'docs/development/PILOT_PROTOCOL_V1.md'
    digest = hashlib.sha256(policy.read_bytes()).hexdigest()
    if config.get('protocol_sha256') != digest:
        raise ValueError('Protocol copy differs from the recorded shared version')
    if 'docs/development/PILOT_PROTOCOL_V1.md' not in (root / 'AGENTS.md').read_text():
        raise ValueError('AGENTS.md must direct maintainers to the common protocol')
    workflows = config.get('required_workflows')
    if not isinstance(workflows, list) or not workflows:
        raise ValueError('Component must retain its verification workflow inventory')
    for name in workflows:
        if not isinstance(name, str) or '/' in name or not name.endswith(('.yml', '.yaml')):
            raise ValueError('Invalid workflow filename')
        if not (root / '.github/workflows' / name).is_file():
            raise ValueError('Missing verification workflow: ' + name)
    for key in ('authority', 'local_verification', 'integration_verification'):
        if not config.get(key):
            raise ValueError('Missing component guidance: ' + key)
    print(config['repository'] + ': shared protocol and verification inventory valid; runtime acceptance is separate')


if __name__ == '__main__':
    try:
        verify(Path(__file__).resolve().parents[2])
    except (OSError, ValueError, KeyError) as error:
        print('Protocol check failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
