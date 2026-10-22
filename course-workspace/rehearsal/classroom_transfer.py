# SPDX-License-Identifier: Apache-2.0
"""CW1 adapter to the existing explicit browser submission rehearsal.

This module starts no services and provisions no assignments. A later admitted
rehearsal supplies the existing authorized Course and two browser sessions.
"""
import base64
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'classroom'), str(ROOT / 'course')]
from info511_package import FILES, check_submission, compare_receipt
from assignment_profiles import LEGACY, get_profile
from waypoint_cairn import read_saved
from submission_handoff import first_submission


def submit_info511_attempt(course, learner, peer, item, saved_directory, *,
                           learner_username, expected_actions, notebook_page=None, profile=LEGACY):
    """Exercise actual explicit upload/confirm for one prepared synthetic attempt.

    Does not execute the notebook or grade work. Run only after fresh serial
    compute admission, with an operator-created pinned assignment and no models.
    expected_actions must reflect the current repository snapshot (create/replace).
    notebook_page, when provided, must explicitly select these same four saved
    files in Jupyter; otherwise the existing explicit device upload is used.
    """
    profile = get_profile(profile)
    selected = read_saved(saved_directory, list(profile.files))
    files = {entry['path']: base64.b64decode(entry['content_base64'], validate=True)
             for entry in selected}
    report = check_submission(files, profile=profile)
    if report['state'] != 'ready_for_preview_not_submitted':
        raise ValueError('CW1 attempt is incomplete; use local completeness guidance')
    result = first_submission(course, learner, peer, item, '', notebook_page,
                              selected_files=files, learner_username=learner_username,
                              expected_actions=expected_actions)
    # Read back through the existing session and compare exact saved bytes.
    sid = item['submission_id']
    receipt = course.request(learner, 'GET', f'/me/work/{sid}/submission-receipts/{result["id"]}')
    if compare_receipt(report, receipt, sid, profile=profile)['state'] != 'receipt_bytes_match_only':
        raise AssertionError('CW1 submitted receipt differs from the selected saved snapshot')
    return result
