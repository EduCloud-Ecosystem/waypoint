import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from research_workspace import assessment


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "assessment/fixtures/research-default.json"


def fixture():
    return json.loads(FIXTURE.read_text())


def encoded(value):
    return json.dumps(value).encode("ascii")


class ManifestTests(unittest.TestCase):
    def test_canonical_shared_vector(self):
        value = assessment.validate_manifest(fixture())
        self.assertEqual(assessment.manifest_digest(value),
                         "506a3587d73e70c1d545631ea2dd455056db92570e394230d5f56d81a7738420")
        reordered = dict(reversed(list(value.items())))
        self.assertEqual(assessment.canonical_bytes(value), assessment.canonical_bytes(reordered))
        self.assertNotIn(b"\n", assessment.canonical_bytes(value))

    def test_default_is_only_structural_not_execution_or_cleanup_authority(self):
        summary = assessment.manifest_summary(fixture(), now=datetime(2026, 10, 19, tzinfo=timezone.utc))
        self.assertTrue(summary["structure_valid"])
        self.assertFalse(summary["expired"])
        for key in ("execution_authorized", "upload_admission", "cleanup_trust_verified", "retention_finalized"):
            self.assertFalse(summary[key])
        expired = assessment.manifest_summary(fixture(), now=datetime(2026, 10, 20, tzinfo=timezone.utc))
        self.assertTrue(expired["expired"])

    def test_instructional_path_requires_distinct_bounded_fields(self):
        value = fixture()
        value["pathway"] = "instructional"
        with self.assertRaises(assessment.AssessmentError):
            assessment.validate_manifest(value)
        value["instruction"] = {"enrollment": 10, "simultaneous_sessions": 5,
                                "assignment_count": 2, "deadline_peak_sessions": 5,
                                "per_student_storage_bytes": 1048576}
        assessment.validate_manifest(value)
        for key in ("simultaneous_sessions", "deadline_peak_sessions"):
            changed = copy.deepcopy(value)
            changed["instruction"][key] = 11
            with self.assertRaises(assessment.AssessmentError):
                assessment.validate_manifest(changed)
        value["pathway"] = "research"
        with self.assertRaises(assessment.AssessmentError):
            assessment.validate_manifest(value)

    def test_smaller_design_requires_explicit_declaration_and_remains_provisional(self):
        value = fixture()
        value["design"].update(fitting_sizes=[10], repetitions=1, seeds=[1])
        value["ceilings"].update(max_attempts=2, cpu_seconds=60)
        with self.assertRaises(assessment.AssessmentError):
            assessment.validate_manifest(value)
        value["design"]["smaller_design"] = {"approved_by_user": True, "reason": "allowance_limited"}
        self.assertTrue(assessment.manifest_summary(value)["provisional"])
        self.assertFalse(assessment.manifest_summary(value)["execution_authorized"])
        value["design"]["smaller_design"]["approved_by_user"] = 1
        with self.assertRaises(assessment.AssessmentError):
            assessment.validate_manifest(value)

    def test_held_out_size_cannot_be_a_fitting_size(self):
        value = fixture()
        value["design"]["held_out_size"] = 20
        with self.assertRaises(assessment.AssessmentError):
            assessment.validate_manifest(value)

    def test_repetition_seed_and_attempt_denominators_cannot_disagree(self):
        variants = []
        for seeds in ([1, 1, 2], [1, 2], [1, 2, True]):
            value = fixture(); value["design"]["seeds"] = seeds; variants.append(value)
        for attempts in (11, 13, True):
            value = fixture(); value["ceilings"]["max_attempts"] = attempts; variants.append(value)
        for value in variants:
            with self.subTest(value=value["design"]), self.assertRaises(assessment.AssessmentError):
                assessment.validate_manifest(value)

    def test_partial_empty_and_complete_reservation_coverage_are_unverified(self):
        value = fixture()
        summary = assessment.manifest_summary(value)
        self.assertEqual(summary["reservation_coverage"],
                         {"declared": 1, "planned": 12, "status": "partial", "references_verified": False})
        value["reservations"] = []
        self.assertEqual(assessment.manifest_summary(value)["reservation_coverage"]["status"], "unreserved")
        for role, sizes in (("fitting", [10, 20, 30]), ("held_out", [40])):
            for size in sizes:
                for repetition in (1, 2, 3):
                    identity = f'{len(value["reservations"]):032x}'
                    value["reservations"].append({"attempt_id": identity, "stage_id": "analysis",
                        "sample_role": role, "sample_size": size, "repetition": repetition,
                        "reservation_id": identity})
        summary = assessment.manifest_summary(value)
        self.assertEqual(summary["reservation_coverage"]["status"], "complete")
        self.assertFalse(summary["reservation_coverage"]["references_verified"])
        self.assertFalse(summary["execution_authorized"])

    def test_reservations_bind_unique_declared_slots_and_ids(self):
        for key, bad in (("stage_id", "setup"), ("sample_role", "training"),
                         ("sample_size", 11), ("sample_size", True), ("repetition", 4),
                         ("sample_role", "held_out"), ("attempt_id", "raw-input.csv"),
                         ("reservation_id", "raw-input.csv"), ("filename", "raw-input.csv")):
            value = fixture(); value["reservations"][0][key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(assessment.AssessmentError):
                assessment.validate_manifest(value)
        for duplicate in ("slot", "attempt_id", "reservation_id"):
            value = fixture()
            other = copy.deepcopy(value["reservations"][0])
            other.update(attempt_id="4"*32, reservation_id="5"*32, repetition=2)
            if duplicate == "slot":
                other["repetition"] = 1
            else:
                other[duplicate] = value["reservations"][0][duplicate]
            value["reservations"].append(other)
            with self.subTest(duplicate=duplicate), self.assertRaises(assessment.AssessmentError):
                assessment.validate_manifest(value)
        value = fixture(); value["reservations"] = None
        with self.assertRaises(assessment.AssessmentError):
            assessment.validate_manifest(value)

    def test_reviewed_recipe_image_pair_and_candidate_gate(self):
        from research_workspace.catalog import list_environments
        reviewed_r = next(e for e in list_environments() if e["status"] == "active" and e["recipe"] == "summary-r")
        value = fixture()
        value["workload"].update(recipe_id=reviewed_r["recipe"], recipe_sha256=reviewed_r["recipe_sha256"],
                                 image=reviewed_r["image"], environment_id=reviewed_r["id"])
        assessment.validate_manifest(value)
        candidate = next(e for e in list_environments() if e["status"] == "candidate")
        for field, replacement in (("recipe_sha256", "0"*64), ("image", "python:latest"),
                                   ("environment_id", "unknown")):
            value = fixture(); value["workload"][field] = replacement
            with self.subTest(field=field), self.assertRaises(assessment.AssessmentError):
                assessment.validate_manifest(value)
        value = fixture()
        value["workload"].update(recipe_id=candidate["recipe"], recipe_sha256=candidate["recipe_sha256"],
                                 image=candidate["image"], environment_id=candidate["id"])
        with self.assertRaises(assessment.AssessmentError):
            assessment.validate_manifest(value)

    def test_only_supported_units_methods_quality_and_finite_ceilings(self):
        changes = [("design", "size_unit", "GB"), ("measurement", "method", "custom command"),
                   ("quality", "absolute_tolerance_nanounits", 0),
                   ("ceilings", "scratch_bytes", 0), ("ceilings", "output_bytes", 65537),
                   ("ceilings", "timeout_seconds_per_attempt", -1),
                   ("hardware", "memory_bytes", True), ("hardware", "gpu_count", 1)]
        for group, key, new in changes:
            value = fixture(); value[group][key] = new
            with self.subTest(key=key), self.assertRaises(assessment.AssessmentError):
                assessment.validate_manifest(value)

    def test_unknown_content_is_refused_at_every_object_level(self):
        for group in (None, "workload", "hardware", "measurement", "design", "quality", "ceilings"):
            for key in ("source_sha256", "filename", "stdout", "stderr", "raw_log", "user_text", "cleanup_verified"):
                value = fixture()
                target = value if group is None else value[group]
                target[key] = "PRIVATE_SAMPLE_SENTINEL"
                with self.subTest(group=group, key=key), self.assertRaises(assessment.AssessmentError):
                    assessment.validate_manifest(value)

    def test_private_data_and_malformed_identifiers_expiry_refused(self):
        for field, new in (("data_classification", "student_records"), ("assessment_id", "../raw.csv"),
                           ("project_id", "owner@example.org"), ("expires_at", "never"),
                           ("expires_at", "2026-02-31T00:00:00Z"),
                           ("expires_at", "2026-10-20T00:00:00+00:00")):
            value = fixture(); value[field] = new
            with self.subTest(field=field), self.assertRaises(assessment.AssessmentError):
                assessment.validate_manifest(value)


class ParserTests(unittest.TestCase):
    def test_duplicates_floats_nonfinite_deep_and_oversize_refused(self):
        values = [b'{"a":1,"a":2}', b'{"a":{"b":1,"b":2}}', b'{"n":1.0}', b'{"n":1e0}',
                  b'{"n":NaN}', b'{"n":Infinity}', b'{"n":9007199254740992}',
                  b'{' + b' ' * assessment.MAX_DOCUMENT_BYTES + b'}',
                  b'{"a":' + b'['*20 + b'0' + b']'*20 + b'}', b'{"a":"\\u2603"}']
        for payload in values:
            with self.subTest(payload=payload[:50]), self.assertRaises(assessment.AssessmentError):
                assessment.strict_document(payload)

    def test_fifo_symlink_hardlink_and_directory_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ordinary = root / "manifest.json"; ordinary.write_bytes(encoded(fixture()))
            fifo = root / "fifo"; os.mkfifo(fifo)
            alias = root / "alias"; alias.symlink_to(ordinary)
            for path in (fifo, alias, root):
                with self.subTest(path=path.name), self.assertRaises(assessment.AssessmentError):
                    assessment.read_document(path)
            hardlink = root / "hardlink"; os.link(ordinary, hardlink)
            with self.assertRaises(assessment.AssessmentError):
                assessment.read_document(ordinary)


class OperatorAssessmentTests(unittest.TestCase):
    def cli(self, args, cwd):
        return subprocess.run([sys.executable, "-m", "research_workspace", *args], cwd=cwd,
                              env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
                              capture_output=True, timeout=15)

    def test_actual_prepare_and_validate_cli_create_no_state_and_match_canonical_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "manifest.json"
            value = fixture(); value["expires_at"] = "2099-01-01T00:00:00Z"
            path.write_bytes(encoded(value))
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            result = self.cli(["assessment-prepare", str(path)], root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, assessment.canonical_bytes(value))
            result = self.cli(["assessment-validate", str(path)], root)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout)
            self.assertEqual(summary["manifest_sha256"], assessment.manifest_digest(value))
            self.assertFalse(summary["execution_authorized"])
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})

    def test_actual_cli_rejects_expired_preparation_but_can_inspect(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            value = fixture(); value["expires_at"] = "2020-01-01T00:00:00Z"
            path.write_bytes(encoded(value))
            prepared = self.cli(["assessment-prepare", str(path)], directory)
            self.assertEqual(prepared.returncode, 1)
            self.assertEqual(prepared.stdout, b"")
            validated = self.cli(["assessment-validate", str(path)], directory)
            self.assertEqual(validated.returncode, 0)
            self.assertTrue(json.loads(validated.stdout)["expired"])

    def test_cli_never_echoes_rejected_data_or_missing_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PRIVATE_FILENAME_SENTINEL.json"
            value = fixture(); value["filename"] = "PRIVATE_SAMPLE_SENTINEL"
            path.write_bytes(encoded(value))
            for selected in (path, path.with_suffix('.missing')):
                result = self.cli(["assessment-validate", str(selected)], directory)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(b"PRIVATE_", result.stderr)

    def test_actual_cli_rejects_malformed_wire_types_and_binding_without_echo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            bad = []
            for field, new in (("schema", "educloud.benchmark-manifest/v2"),
                               ("reservation_id", "PRIVATE_SOURCE_HASH_SENTINEL"),
                               ("cleanup_verified", True)):
                value = fixture(); value[field] = new; bad.append(encoded(value))
            value = fixture(); value["hardware"]["cpu_count"] = True; bad.append(encoded(value))
            value = fixture(); value["design"]["size_unit"] = "PRIVATE_UNIT_SENTINEL"; bad.append(encoded(value))
            bad += [b'{"schema":"PRIVATE_SENTINEL","schema":"again"}', b'{"n":NaN}',
                    b'{"n":1.0}', b'{"n":1e0}', b' ' * (assessment.MAX_DOCUMENT_BYTES + 1)]
            for payload in bad:
                path.write_bytes(payload)
                result = self.cli(["assessment-validate", str(path)], directory)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b"")
                self.assertNotIn(b"PRIVATE_", result.stderr)

    def test_cli_paths_cannot_be_substituted_for_old_bundle_format(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"; path.write_bytes(encoded(fixture()))
            old = self.cli(["verify-bundle", str(path)], directory)
            self.assertEqual(old.returncode, 1)
            bundle = ROOT / "evidence/2026-10-17/browser-evidence.zip"
            new = self.cli(["assessment-validate", str(bundle)], directory)
            self.assertEqual(new.returncode, 1)
            self.assertEqual(new.stdout, b"")

    def test_validation_cannot_initialize_state_call_broker_or_export_study(self):
        from research_workspace import evidence, runtime, store
        from research_workspace.__main__ import main
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"; path.write_bytes(encoded(fixture()))
            with patch.object(store.Store, "__init__", side_effect=AssertionError("state initialized")), \
                 patch.object(runtime.Broker, "__init__", side_effect=AssertionError("broker initialized")), \
                 patch.object(evidence, "build_bundle", side_effect=AssertionError("raw exporter invoked")), \
                 patch.object(sys, "argv", ["research_workspace", "assessment-validate", str(path)]), \
                 patch.object(sys, "stdout", io.StringIO()) as output, \
                 patch.object(assessment, "datetime", wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 10, 19, tzinfo=timezone.utc)
                main()
                self.assertFalse(json.loads(output.getvalue())["expired"])


if __name__ == "__main__":
    unittest.main()
