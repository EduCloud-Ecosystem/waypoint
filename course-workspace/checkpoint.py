"""Offline workspace checkpoint, called under the pilot operation lock."""
from datetime import datetime, timezone
import json

from backup import backup, docker, LABEL
from deployment import compose, wait_ready


def checkpoint(directory, env, destination):
    # Fail before downtime if an archive already exists or its parent is absent.
    if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
        raise ValueError('checkpoint requires a new directory under an existing parent')
    instance = env['WORKSPACE_INSTANCE']
    hub = json.loads(docker('inspect', instance + '-hub'))[0]
    if hub['Config'].get('Labels', {}).get(LABEL) != instance:
        raise ValueError('refusing to stop a Hub not owned by this trial')
    image = json.loads(docker('image', 'inspect', env['WORKSPACE_IMAGE']))[0]
    was_running = hub['State']['Running']
    try:
        if was_running:
            compose(directory, 'stop', 'hub')
        # Existing archive code refuses orphaned or unlabelled live writers.
        backup(instance, destination, env['WORKSPACE_IMAGE'])
        metadata = {'created_at': datetime.now(timezone.utc).isoformat(),
                    'hub_image_id': hub['Image'], 'course_image_id': image['Id'],
                    'course_architecture': image['Architecture'],
                    'instance': instance, 'synthetic_only': env['WORKSPACE_AUTH_MODE'] == 'local-test'}
        record = destination / 'runtime.json'
        record.write_text(json.dumps(metadata, indent=2) + '\n')
        record.chmod(0o600)
    finally:
        # Resume even on archive failure, but never start a previously stopped Hub.
        # start uses the same container/image; it cannot silently rebuild a release.
        if was_running:
            compose(directory, 'start', 'hub')
            wait_ready(env)
    print(f'Checkpoint saved at {destination}. ' +
          ('Hub resumed; reopen your notebook to restart its kernel.' if was_running else 'Hub remains stopped.'))
    print('This is a local, unencrypted archive containing credentials and saved work. Copy securely off host and preserve both images for recovery.')
