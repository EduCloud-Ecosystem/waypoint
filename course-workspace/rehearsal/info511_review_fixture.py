# SPDX-License-Identifier: Apache-2.0
"""Explicit synthetic reviewer inputs, never a human or academic judgment."""
import base64
import hashlib
import math
import re

TASK = 'info511-lab3-q4-synthetic-v1'
FILES = {'lab3-q4.ipynb', 'tidy-inflation.csv', 'explanation.md', 'assistance.md'}
LABEL = 'automated-synthetic-fixture'
NOTE = 'Automated synthetic reviewer: API transition test only; no human review, learning or assessment-quality evidence.'


def review_inputs(record, receipt, fixture, user, rubric):
    """Refuse drift; bind authored test points/citations to all four originals."""
    if fixture.get('schema') != 'CW1-reviewer-fixture-v1' or fixture.get('task_version') != TASK or fixture.get('review_actor_kind') != LABEL:
        raise ValueError('explicit CW1 automated reviewer fixture required')
    if set(fixture.get('learners', {})) != {'alice', 'bob'} or user not in ('alice', 'bob'):
        raise ValueError('only the two synthetic learners are permitted')
    case = fixture['learners'][user]
    doc = record['document']
    if record['status'] != 'collected' or record['revision'] != receipt['commit'] or doc['revision'] != receipt['commit']:
        raise ValueError('capture is not the submitted collected revision')
    if doc['rubric'] != rubric or not re.fullmatch('[0-9a-f]{64}', doc['input_digest']):
        raise ValueError('unexpected rubric or input digest')
    artifacts = doc['artifacts']
    changes = receipt['changes']
    if len(artifacts) != 4 or len(changes) != 4 or {a['path'] for a in artifacts} != FILES or {a['path'] for a in changes} != FILES or set(case['artifacts']) != FILES:
        raise ValueError('exactly four fixture artifacts and receipt changes required')
    by_path = {a['path']: a for a in artifacts}
    receipt_by_path = {a['path']: a for a in changes}
    for path, artifact in by_path.items():
        raw = base64.b64decode(artifact['original'], validate=True)
        expected = case['artifacts'][path]
        change = receipt_by_path[path]
        if artifact.get('issue') or len(raw) > 256 * 1024 or len(raw) != artifact['size'] or len(raw) != expected['bytes'] or len(raw) != change['bytes']:
            raise ValueError('artifact issue, size or fixture drift')
        digest = hashlib.sha256(raw).hexdigest()
        if not (digest == artifact['sha256'] == expected['sha256'] == change['sha256']):
            raise ValueError('captured original, receipt or fixture digest differs')
    criteria = rubric['criteria']
    ids = {c['id'] for c in criteria}
    if any(set(case[key]) != ids for key in ('proposal_points', 'review_points', 'citations')):
        raise ValueError('every rubric criterion needs explicit fixture inputs')
    def judgments(key):
        result = []
        for criterion in criteria:
            cid = criterion['id']
            points = case[key][cid]
            if type(points) not in (int, float) or not math.isfinite(points) or not 0 <= points <= criterion['max_points']:
                raise ValueError('fixture point input is outside the rubric')
            selected = case['citations'][cid]
            artifact = by_path[selected['path']]
            matches = [seg for seg in artifact['segments'] if seg['location'] == selected['location']]
            if len(matches) != 1 or not selected['quote'] or selected['quote'] != matches[0]['text']:
                raise ValueError('fixture citation is not the exact captured source segment')
            citation = dict(selected, sha256=artifact['sha256'])
            result.append({'criterion_id': cid, 'points': points, 'feedback': NOTE,
                           'uncertainty': 'Fixture points are test inputs, not academic conclusions.',
                           'citations': [citation]})
        return result
    proposal = {'submission_status': 'relevant_work', 'source': 'fixture',
                'model': 'none-automated-synthetic-reviewer', 'prompt_version': 'CW1-fixture-review-v1',
                'input_digest': doc['input_digest'], 'criteria': judgments('proposal_points')}
    review = {'action': 'approve', 'note': NOTE, 'criteria': judgments('review_points')}
    return proposal, review
