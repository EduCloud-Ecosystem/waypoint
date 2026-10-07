#!/usr/bin/env python3
"""Disposable full course rehearsal; requires a Linux runner with Docker and root XFS access."""
import argparse
import json
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright
from cairn_journey import journey, run
from notebook import Notebook


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cairn-binary', required=True, type=Path)
    parser.add_argument('--cairn-source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('use a disposable Linux runner as root; physical devices are never formatted')
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    root = args.output.resolve()
    notebook_root, course_root = root/'notebook', root/'course'
    notebook_root.mkdir(mode=0o700); course_root.mkdir(mode=0o700)
    notebook = Notebook(notebook_root)
    try:
        notebook.start()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=['--host-resolver-rules=MAP *.rehearsal.test 127.0.0.1'])
            try:
                report = journey(course_root, args.cairn_binary.resolve(), notebook.origin,
                                 notebook.image, None, browser, notebook.handoff, notebook.revocation)
            finally:
                browser.close()
        report['cairn_revision'] = run('git', '-c', 'safe.directory='+str(args.cairn_source.resolve()),
                                       '-C', str(args.cairn_source.resolve()), 'rev-parse', 'HEAD')
        report['notebook_checks'] = notebook.report
        report['waypoint_revision'] = run('git', '-c', 'safe.directory='+str(Path(__file__).resolve().parents[2]),
                                          '-C', str(Path(__file__).resolve().parents[2]), 'rev-parse', 'HEAD')
        report['course_image_id'] = run('docker', 'image', 'inspect', notebook.image, '--format', '{{.Id}}')
        (root/'PASS.json').write_text(json.dumps(report, indent=2)+'\n')
        print('PASS: Cairn → Keycloak → Python/R → saved file → Forgejo → pinned Cairn grading → revocation.',flush=True)
    finally:
        notebook.close()


if __name__ == '__main__':
    main()
