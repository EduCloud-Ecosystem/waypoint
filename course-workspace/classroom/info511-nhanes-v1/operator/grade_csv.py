# SPDX-License-Identifier: Apache-2.0
"""Instructor-pinned adapter to the reviewed NHANES formative checker.

Runs only in Cairn's existing network-disabled grading container. It reads the
submitted CSV, never executes the submitted notebook, and checks correctness
only. The other six rubric points require explicit instructor review.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat

POLICY = Path('/cairn-policy')
LIMIT = 256 * 1024


def bounded_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > LIMIT:
            raise ValueError('regular bounded file required')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(LIMIT + 1)
        after = os.fstat(fd)
        if len(data) > LIMIT or len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('file changed or exceeds limit')
        return data
    finally:
        os.close(fd)


def main():
    import pandas as pd
    binding = json.loads(bounded_regular(POLICY / 'checker-binding.json'))
    checker = POLICY / 'nhanes_check.py'
    if hashlib.sha256(bounded_regular(checker)).hexdigest() != binding['sha256']:
        raise ValueError('instructor checker binding changed')
    spec = importlib.util.spec_from_file_location('cw1_pinned_checker', checker)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources = {}
    for name in ('DEMO_L_selected.csv', 'BPXO_L_selected.csv'):
        raw = bounded_regular(POLICY / 'data' / name)
        if hashlib.sha256(raw).hexdigest() != binding['source_files'][name]:
            raise ValueError('instructor source binding changed')
        sources[name] = pd.read_csv(io.BytesIO(raw))
    demographics = sources['DEMO_L_selected.csv']
    bp_readings = sources['BPXO_L_selected.csv']
    result = pd.read_csv(io.BytesIO(bounded_regular(Path('/work/tidy-nhanes.csv'))))
    # CSV-only: copied preserved frames cannot establish submitted notebook method,
    # actual kernel execution or source-frame immutability. Review those separately.
    ok, detail = module.check_nhanes(demographics, bp_readings,
        demographics.copy(deep=True), bp_readings.copy(deep=True), result)
    # Checker-owned fixed messages only. No learner code, rows or narration.
    print(detail)
    return 0 if ok else 1


if __name__ == '__main__':
    try:
        status = main()
    except (OSError, ValueError, KeyError, TypeError):
        print('CSV or instructor checker unavailable; correctness not established.')
        status = 2
    raise SystemExit(status)
