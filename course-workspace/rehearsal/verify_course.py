#!/usr/bin/env python3
"""Disposable full course rehearsal; requires Linux Docker and root XFS access."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

from playwright.sync_api import sync_playwright
from cairn_journey import journey, FORGEJO_IMAGE, public_metrics, run
from evidence import collect_provenance, write_pass, REQUIRED_PHASES
from notebook import Notebook, KEYCLOAK_IMAGE, NGINX_IMAGE


NOTEBOOK_CHECKS = {
    'notebook_python_r': {'python-r-wss', 'saved-file-handoff'},
    'notebook_persistence': {'server-stop-resume', 'saved-file-after-server-restart'},
    'notebook_isolation': {'authenticated-peer-file-denied', 'unrostered-login-denied'},
    'notebook_revocation': {'active-server-stopped', 'old-session-denied',
                            'removed-roster-respawn-denied', 'revocation-retains-files'},
    'bounded_load': {'bounded-notebook-traffic', 'active-limit-429', 'freed-slot-reused'},
}


def notebook_phases(checks):
    observed = set(checks)
    return {phase: expected <= observed for phase, expected in NOTEBOOK_CHECKS.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cairn-binary', required=True, type=Path)
    parser.add_argument('--cairn-source', required=True, type=Path)
    parser.add_argument('--lti-binary', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('use a disposable Linux runner as root; physical devices are never formatted')
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    root = args.output.resolve()
    notebook_root, course_root = root/'notebook', root/'course'
    notebook_root.mkdir(mode=0o700)
    course_root.mkdir(mode=0o700)
    started, timer = datetime.now(timezone.utc).isoformat(), time.monotonic()
    notebook = Notebook(notebook_root, extra_users=('carol',))
    try:
        notebook.start()
        # A fresh CI worker has never run Course.start yet. Resolve the pinned
        # Forgejo image before collecting its immutable local identity.
        run('docker', 'pull', FORGEJO_IMAGE, timeout=180)
        images = {'course': notebook.image, 'checkout': 'cairn-checkout:local', 'forgejo': FORGEJO_IMAGE,
                  'keycloak': KEYCLOAK_IMAGE, 'nginx': NGINX_IMAGE, 'hub': notebook.instance + '-hub:pilot'}
        provenance = collect_provenance(args.cairn_source, args.cairn_binary, args.lti_binary, images=images)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                report = journey(course_root, args.cairn_binary.resolve(), notebook.origin,
                                 notebook.image, None, browser, args.cairn_source.resolve(),
                                 args.lti_binary.resolve(), notebook.handoff, notebook.revocation)
            finally:
                browser.close()
        assert provenance == collect_provenance(args.cairn_source, args.cairn_binary, args.lti_binary, images=images), 'inputs changed during pilot'
    finally:
        notebook.close()
    metrics = public_metrics(report)
    metrics.update({f'notebook_{key}': value for key, value in notebook.load_report.items()
                    if type(value) in (int, float) and value >= 0})
    phases = {p: True for p in REQUIRED_PHASES['local-subset']}
    phases.update(notebook_phases(notebook.report))
    write_pass(root, {'provenance': provenance, 'phases': phases,
        'started_at': started, 'finished_at': datetime.now(timezone.utc).isoformat(),
        'duration_seconds': time.monotonic() - timer,
        'checks': report['checks'] + report['assessment']['checks'] + report['load']['checks'] + notebook.report,
        'metrics': metrics}, scope='full-linux', cleanup_verified=notebook.cleanup_verified)
    print('PASS: full synthetic course pilot; all phases and cleanup verified.', flush=True)


if __name__ == '__main__':
    main()
