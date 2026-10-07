"""Fail closed when a hosted learner's operator-provisioned quota is unverified."""
import json
from pathlib import Path
import time


def quota_home(settings, key, status_root=Path('/quota-status')):
    root = settings['home_root']
    if not root:
        return None
    status = json.loads((status_root / (settings['instance'] + '.json')).read_text())
    age = time.time() - status['checked_at']
    if not status.get('healthy') or not 0 <= age <= 120 or status.get('instance') != settings['instance']:
        raise ValueError('home quota verification is unhealthy or stale; contact the operator')
    name = settings['instance'] + '-home-' + key
    record = status['volumes'].get(name)
    quota = settings['home_quota_mb'] * 1024**2
    if not record or record['quota_bytes'] != quota:
        raise ValueError('learner home quota has not been provisioned')
    return {'name': name, 'labels': {'educloud.workspace.instance': settings['instance'],
                                    'educloud.workspace.kind': 'home',
                                    'educloud.workspace.quota-bytes': str(quota),
                                    'educloud.workspace.project-id': str(record['project_id'])},
            'options': {'type': 'none', 'o': 'bind', 'device': root + '/' + name}}
