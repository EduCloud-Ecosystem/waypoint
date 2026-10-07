#!/usr/bin/env python3
"""Exercise the small trial with real kernels and bounded admission."""
import argparse
from pathlib import Path
import uuid
import requests

from backup import docker, restore
from checkpoint import checkpoint
from pilot import ROOT, initialize, start, compose, locked
from smoke import login, kernel, change, xsrf, runtime_policy
from verify import cleanup, port


def check(env, restored=False):
    base = env['WORKSPACE_PUBLIC_URL']
    session = login(base, 'alice', env['WORKSPACE_TEST_PASSWORD'])
    runtime_policy(env, users=('alice',))
    file_url = base + '/user/alice/api/contents/minimal-trial.txt'
    marker = 'synthetic Python/R trial survives restart'
    if not restored:
        change(session, 'PUT', file_url,
               json={'type': 'file', 'format': 'text', 'content': marker}).raise_for_status()
    response = session.get(file_url, timeout=30)
    response.raise_for_status()
    assert response.json()['content'] == marker
    kernel(session, base, 'alice', 'python3',
           "import pandas as pd; assert pd.Series([2, 3]).sum() == 5; print('CONTINUITY_OK')")
    kernel(session, base, 'alice', 'ir',
           "library(ggplot2); stopifnot(sum(c(2,3)) == 5); cat('CONTINUITY_OK\\n')")
    bob = requests.Session()
    bob.get(base + '/hub/login', timeout=30).raise_for_status()
    response = bob.post(base + '/hub/login',
                        data={'username': 'bob', 'password': env['WORKSPACE_TEST_PASSWORD'],
                              '_xsrf': xsrf(bob, base + '/hub/login')}, timeout=60)
    # Login may land on the capacity error, or Hub home depending on Hub state.
    if response.status_code not in (429, 503):
        response.raise_for_status()
        response = bob.get(base + '/hub/spawn/bob', timeout=60)
    assert response.status_code in (429, 503), f'expected capacity refusal, got {response.status_code}'
    runtime_policy(env, users=('alice',))
    print('PASS: one-learner capacity enforced; Python/R and saved file verified', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-hub-image')
    args = parser.parse_args()
    directory = ROOT / 'output' / ('verify-small-' + uuid.uuid4().hex[:10])
    env = initialize(directory, port())
    recovered_dir = directory / 'recovered'
    recovered = initialize(recovered_dir, port())
    try:
        if args.reuse_hub_image:
            docker('tag', args.reuse_hub_image, env['WORKSPACE_INSTANCE'] + '-hub:pilot')
        start(directory, env, build=not args.reuse_hub_image)
        check(env)
        compose(directory, 'stop', 'hub')
        start(directory, env, build=False)
        check(env, restored=True)
        archive = directory / 'checkpoint'
        with locked(directory):
            checkpoint(directory, env, archive)
        for item in archive.iterdir():
            assert item.stat().st_mode & 0o777 == 0o600
        check(env, restored=True)
        compose(directory, 'stop', 'hub')
        restore(recovered['WORKSPACE_INSTANCE'], archive, recovered['WORKSPACE_IMAGE'])
        docker('tag', env['WORKSPACE_INSTANCE'] + '-hub:pilot', recovered['WORKSPACE_INSTANCE'] + '-hub:pilot')
        start(recovered_dir, recovered, build=False)
        check(recovered, restored=True)
        (directory / 'PASS.txt').write_text('Small trial kernels, capacity, limits, restart, checkpoint resume and fresh-volume recovery passed.\n')
    finally:
        cleanup(env['WORKSPACE_INSTANCE'], directory / 'pilot.env')
        cleanup(recovered['WORKSPACE_INSTANCE'], recovered_dir / 'pilot.env')


if __name__ == '__main__':
    main()
