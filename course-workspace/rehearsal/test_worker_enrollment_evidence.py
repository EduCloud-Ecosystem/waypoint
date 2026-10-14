"""Report privacy/provenance regressions; examples are not runtime evidence."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import authenticated_evidence as base
import worker_enrollment_evidence as evidence
from test_authenticated_runtime_evidence import example as baseline


def example():
    value = baseline(8)
    value.update(version=evidence.VERSION, phases=list(evidence.PHASES),
        code_sha256={path: 'a' * 64 for path in evidence.CODE_FILES})
    return value


class WorkerEnrollmentEvidenceTests(unittest.TestCase):
    def refused(self, report):
        with self.assertRaisesRegex(ValueError, '^worker_enrollment_evidence_refused$'):
            evidence.validate_report(report)

    def test_exact_complete_clean_report_includes_all_executed_dependencies(self):
        report = example(); before = deepcopy(report)
        evidence.validate_report(report); self.assertEqual(report, before)
        self.assertEqual(len(evidence.CODE_FILES), 20)
        self.assertTrue(base.CODE_FILES <= evidence.CODE_FILES)
        self.assertEqual(len(evidence.PHASES), 10)

    def test_missing_reordered_or_fabricated_controls_cannot_pass(self):
        for phase in evidence.PHASES:
            report = example(); report['phases'].remove(phase); self.refused(report)
        for phases in (list(reversed(evidence.PHASES)), list(base.PHASES), ['private-value'], list(evidence.PHASES) + [evidence.PHASES[0]]):
            report = example(); report['phases'] = phases; self.refused(report)

    def test_every_dependency_is_required_and_old_report_cannot_be_promoted(self):
        for path in evidence.CODE_FILES:
            report = example(); del report['code_sha256'][path]; self.refused(report)
        self.refused(baseline())
        report = baseline(); report['version'] = evidence.VERSION; self.refused(report)
        report['phases'] = list(evidence.PHASES); self.refused(report)

    def test_private_values_and_false_acceptance_flags_are_refused(self):
        for field in ('certificate', 'keyfile', 'fingerprint', 'spki_sha256', 'clients', 'log', 'hostname', 'plan_id'):
            report = example(); report[field] = 'PRIVATE'; self.refused(report)
        for field in ('cleanup_verified', 'acceptance_eligible'):
            for value in (False, 1, 'true'):
                report = example(); report[field] = value; self.refused(report)
        report = example(); report['code_sha256']['private.key'] = 'a' * 64; self.refused(report)
        report = example(); report['source_dirty'] = True; self.refused(report)

    def test_partial_failed_reports_never_claim_acceptance_and_keep_typed_allocation(self):
        report = example(); report.update(status='FAIL', failure='RuntimeError', acceptance_eligible=False)
        report['phases'] = list(evidence.PHASES[:3]); evidence.validate_report(report)
        report['acceptance_eligible'] = True; self.refused(report)
        report['acceptance_eligible'] = False; report['allocation']['learner_cpu'] = True; self.refused(report)

    def test_cli_redacts_private_exception_and_still_writes_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); report = root / 'report.json'
            (root / 'sitecustomize.py').write_text(
                "import subprocess\ndef private_failure(*args, **kwargs):\n"
                "    raise RuntimeError('private-enrollment-canary')\nsubprocess.run=private_failure\n")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('verify_worker_enrollment.py')),
                '--output', str(report)], capture_output=True, text=True, timeout=20,
                env=dict(os.environ, PYTHONPATH=str(root)))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('private-enrollment-canary', result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            value = json.loads(report.read_text()); evidence.validate_report(value)
            self.assertEqual(value['status'], 'FAIL'); self.assertFalse(value['acceptance_eligible'])
            self.assertTrue(value['cleanup_verified'])


if __name__ == '__main__': unittest.main()
