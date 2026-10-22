# SPDX-License-Identifier: Apache-2.0
"""Local completeness guidance for CW1; never submit, authenticate or grade.

Inputs are explicitly selected saved bytes. Results establish presence/format
only, never authorship, scientific correctness, policy permission or submission.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

TASK = 'info511-lab3-q4-synthetic-v1'
FILES = ('lab3-q4.ipynb', 'tidy-inflation.csv', 'explanation.md', 'assistance.md')
MAX_FILE_BYTES = 256 * 1024  # Fits the narrower Cairn assessment extraction bound.
MAX_TOTAL_BYTES = 1024 * 1024
PLACEHOLDER = 'REPLACE_ME'


def _json(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError('duplicate JSON key')
            out[key] = value
        return out
    def reject_constant(_):
        raise ValueError("nonfinite JSON")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)


def _source(cell):
    value = cell.get('source', '')
    if isinstance(value, list) and all(isinstance(line, str) for line in value):
        return ''.join(value)
    return value if isinstance(value, str) else ''


def check_submission(files):
    """Return content-free reminders and digests. A ready draft is not submitted."""
    reminders = []
    manifest = []
    if not isinstance(files, dict) or set(files) != set(FILES):
        return {'task_version': TASK, 'state': 'incomplete',
                'reminders': ['Select exactly the notebook, tidy CSV, explanation and assistance declaration.'],
                'files': []}
    if any(not isinstance(value, bytes) or len(value) > MAX_FILE_BYTES for value in files.values()) or sum(map(len, files.values())) > MAX_TOTAL_BYTES:
        return {'task_version': TASK, 'state': 'incomplete',
                'reminders': ['Use saved regular files at most 256 KiB each and 1 MiB total.'], 'files': []}
    for name in FILES:
        raw = files[name]
        manifest.append({'path': name, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    try:
        notebook = _json(files['lab3-q4.ipynb'])
        if not isinstance(notebook, dict) or notebook.get('nbformat') != 4 or not isinstance(notebook.get('cells'), list) or len(notebook['cells']) > 512:
            raise ValueError('notebook shape')
        cells = notebook['cells']
        if any(not isinstance(cell, dict) for cell in cells):
            raise ValueError('cell shape')
        if notebook.get('metadata', {}).get('cw1_task_version') != TASK:
            reminders.append('Use the versioned CW1 notebook; do not change its task-version label.')
        for cell_id, label in [('schema-prediction', 'schema prediction'), ('unit-prediction', 'observational-unit prediction')]:
            matches = [cell for cell in cells if cell.get('id') == cell_id]
            if len(matches) != 1 or matches[0].get('cell_type') != 'markdown' or not _source(matches[0]).strip() or PLACEHOLDER in _source(matches[0]):
                reminders.append('Complete the ' + label + ' in its labeled notebook cell before optional help.')
        # Presence only; these editable fields cannot prove chronology or authorship.
    except (ValueError, TypeError, AttributeError, RecursionError, UnicodeError):
        reminders.append('Save a readable version-4 notebook with its two prediction cells.')
    try:
        text = files['tidy-inflation.csv'].decode('utf-8')
        reader = csv.reader(io.StringIO(text, newline=''), strict=True)
        if next(reader, None) != ['country', 'year', 'annual_inflation']:
            raise ValueError('header')
        count = 0
        for row in reader:
            if len(row) != 3:
                raise ValueError('row width')
            count += 1
        if count == 0:
            raise ValueError('empty CSV')
    except (ValueError, csv.Error, UnicodeError):
        reminders.append('Save a nonempty CSV with country, year, annual_inflation columns. Use the formative checker separately for values and missingness.')
    try:
        explanation = files['explanation.md'].decode('utf-8').strip()
        if not explanation or PLACEHOLDER in explanation or '\x00' in explanation or len(explanation.split()) > 150:
            raise ValueError('explanation')
    except (ValueError, UnicodeError):
        reminders.append('Write your own explanation of at most 150 whitespace-separated words.')
    try:
        declaration = files['assistance.md'].decode('utf-8')
        lines = declaration.splitlines()
        if len(lines) < 3 or lines[0] != 'Task version: ' + TASK or lines[1] not in ('Declaration: no_ai', 'Declaration: ai_assisted'):
            raise ValueError('declaration')
        if not lines[2].startswith('Summary: ') or not lines[2][9:].strip() or PLACEHOLDER in declaration or len(declaration) > 4096 or '\x00' in declaration:
            raise ValueError('summary')
    except (ValueError, UnicodeError):
        reminders.append('Complete the assistance declaration and brief summary. no_ai is valid; omit private tutor conversations and monitoring records.')
    return {'task_version': TASK,
            'state': 'incomplete' if reminders else 'ready_for_preview_not_submitted',
            'reminders': reminders, 'files': manifest}


def compare_receipt(report, receipt, submission_id):
    """Compare a separately obtained Cairn receipt; this does not authenticate it.

The operator must obtain it through the existing authorized Cairn session and
bind the submission ID to the assignment/learner there. No receipt is issued.
"""
    mismatch = {'state': 'not_verified', 'authenticated_here': False}
    if report.get('state') != 'ready_for_preview_not_submitted' or not isinstance(receipt, dict):
        return mismatch
    if not isinstance(submission_id, str) or not submission_id or receipt.get('submission_id') != submission_id or receipt.get('status') != 'committed':
        return mismatch
    revision = receipt.get('commit_revision')
    if not isinstance(revision, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', revision):
        return mismatch
    changes = receipt.get('changes')
    if not isinstance(changes, list) or len(changes) != len(FILES):
        return mismatch
    actual = {}
    for change in changes:
        if not isinstance(change, dict) or set(change) != {'path', 'bytes', 'sha256', 'action'}:
            return mismatch
        name = change['path']
        if not isinstance(name, str) or name not in FILES or name in actual or change['action'] not in ('create', 'replace') or type(change['bytes']) is not int:
            return mismatch
        actual[name] = {key: change[key] for key in ('path', 'bytes', 'sha256')}
    if actual != {entry['path']: entry for entry in report['files']}:
        return mismatch
    return {'state': 'receipt_bytes_match_only', 'authenticated_here': False,
            'commit_revision': revision}


def main():
    parser = argparse.ArgumentParser(description='Check local CW1 completeness. Does not submit or grade.')
    parser.add_argument('--saved-directory', type=Path, required=True)
    args = parser.parse_args()
    # Reuse the existing bounded descriptor-relative saved-file reader. No new
    # filesystem authority, transport, project runner, notebook execution or logs.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'course'))
    from waypoint_cairn import read_saved
    try:
        selected = read_saved(args.saved_directory, list(FILES))
        report = check_submission({item['path']: base64.b64decode(item['content_base64'], validate=True) for item in selected})
    except (OSError, ValueError):
        report = {'task_version': TASK, 'state': 'incomplete', 'files': [],
                  'reminders': ['The four saved files could not be read safely. Save them, check their names and select them again.']}
    print(json.dumps(report, indent=2))
    return 0 if report['state'] == 'ready_for_preview_not_submitted' else 1


if __name__ == '__main__':
    raise SystemExit(main())
