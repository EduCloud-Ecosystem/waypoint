# SPDX-License-Identifier: Apache-2.0
"""Two explicit classroom contracts; no arbitrary package/path configuration."""
from pathlib import Path
from typing import NamedTuple


class Profile(NamedTuple):
    key: str
    package: Path
    task: str
    exercise: str
    files: tuple[str, ...]
    csv_header: tuple[str, ...]
    checker_module: str
    correctness_test: str
    title: str


ROOT = Path(__file__).resolve().parent
LEGACY = Profile('legacy', ROOT / 'info511-lab03-q4-v1',
    'info511-lab3-q4-synthetic-v1', 'info511-lab3-q4-synthetic',
    ('lab3-q4.ipynb', 'tidy-inflation.csv', 'explanation.md', 'assistance.md'),
    ('country', 'year', 'annual_inflation'), 'tidy_check.py',
    'synthetic-csv-cell-preservation-only', 'INFO511 Q4 synthetic-v1')
NHANES = Profile('nhanes-v1', ROOT / 'info511-nhanes-v1',
    'info511-nhanes-wrangling-v1', 'info511-nhanes-wrangling',
    ('nhanes-wrangling.ipynb', 'tidy-nhanes.csv', 'explanation.md', 'assistance.md'),
    ('SEQN', 'RIDAGEYR', 'reading', 'systolic_mm_hg', 'bpxo_match'),
    'nhanes_check.py', 'nhanes-join-cell-preservation-only',
    'INFO511 NHANES wrangling-v1: synthetic learners, public data')
PROFILES = {profile.key: profile for profile in (LEGACY, NHANES)}


def get_profile(value='legacy'):
    if isinstance(value, Profile):
        if PROFILES.get(value.key) is value:
            return value
    elif isinstance(value, str) and value in PROFILES:
        return PROFILES[value]
    raise ValueError('unknown classroom assignment profile')
