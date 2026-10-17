"""Evidence boundary tests, including adversarial ZIPs and rewritten checksums."""

import copy
import hashlib
import io
import json
import pathlib
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
import zlib
from unittest.mock import patch

from research_workspace import evidence


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fixture(status="succeeded", recipe="summary-python", run_id="run1"):
    project = {
        "id": "project1", "name": "A public-data study", "question": "Does trimming change a summary?",
        "success_criterion": "A second researcher reproduces the descriptive means.",
        "owner": "opaque-owner-not-for-export", "created_at": "2026-10-17T10:00:00Z",
        "ends_at": "2026-11-17T10:00:00+00:00", "budget_cpu_seconds": 200, "status": "active",
    }
    data, code = b"group,value\nA,1\nA,3\n", b"# independently review this recipe before execution\n"
    metrics = {"condition": "baseline", "count": 2, "mean": 2.0, "groups": [{"group": "A", "count": 2, "mean": 2.0}]}
    run = {
        "id": run_id, "project_id": "project1", "recipe": recipe, "condition": "baseline", "status": status,
        "created_at": "2026-10-17T10:00:00Z", "started_at": "2026-10-17T10:00:01Z",
        "finished_at": "2026-10-17T10:00:02Z", "input_sha256": digest(data), "recipe_sha256": digest(code),
        "environment": {"image": "sha256:" + "a" * 64, "runtime": "python" if recipe == "summary-python" else "r", "runner": "outfitter-docker-v1"},
        "parameters": {}, "metrics": metrics if status == "succeeded" else {},
        "error": None if status == "succeeded" else "Bounded execution did not complete.",
        "cleanup_verified": status == "succeeded",
        "compute": {"reserved_cpu_seconds": 10.0, "charged_cpu_seconds": 10.0, "provenance": "estimated_upper_bound", "unresolved_liability": status != "succeeded"},
    }
    files = {f"runs/{run_id}/input.csv": data, f"runs/{run_id}/" + ("recipe.py" if recipe == "summary-python" else "recipe.R"): code}
    if status == "succeeded":
        # Non-canonical result serialization is preserved exactly and bound by hash.
        files[f"runs/{run_id}/result.json"] = json.dumps(metrics, indent=2).encode()
    return project, [run], files


def members(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def repack(files, *, mode=zipfile.ZIP_STORED, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=mode) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        if extra:
            archive.writestr(*extra)
    return output.getvalue()


def rehash(files):
    manifest = json.loads(files["checksums.json"])
    manifest["files"] = {name: {"sha256": digest(data), "size": len(data)} for name, data in files.items() if name != "checksums.json"}
    files["checksums.json"] = canonical(manifest)
    return repack(files)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.project, self.runs, self.files = fixture()
        self.payload = evidence.build_bundle(self.project, self.runs, self.files)

    def test_deterministic_round_trip_preserves_exact_selected_bytes(self):
        self.assertEqual(self.payload, evidence.build_bundle(self.project, self.runs, self.files))
        result = evidence.verify_bundle(self.payload)
        self.assertTrue(result["valid"])
        self.assertEqual(result["bundle_sha256"], digest(self.payload))
        self.assertEqual(result["run_count"], 1)
        self.assertEqual(result["file_count"], 10)
        decoded = members(self.payload)
        for name, value in self.files.items():
            self.assertEqual(decoded[name], value)
        self.assertIn("not establish scientific validity", result["warning"])

    def test_r_recipe_is_exported_with_correct_extension(self):
        args = fixture(recipe="summary-r")
        result = evidence.verify_bundle(evidence.build_bundle(*args))
        self.assertTrue(result["valid"])

    def test_failure_and_queued_runs_remain_visible_with_gaps(self):
        for status in ("queued", "running", "failed", "cancelled", "interrupted"):
            with self.subTest(status=status):
                project, runs, files = fixture(status=status)
                runs[0]["environment"] = {}
                payload = evidence.build_bundle(project, runs, files)
                result = evidence.verify_bundle(payload)
                codes = {gap["code"] for gap in result["gaps"]}
                self.assertTrue({"run_not_succeeded", "result_not_available", "environment_not_recorded", "cleanup_not_verified", "unresolved_compute_liability"} <= codes)
                exported_run = json.loads(members(payload)["runs.json"])[0]
                self.assertEqual(exported_run["error"], runs[0]["error"])
                self.assertEqual(exported_run["status"], status)

    def test_empty_project_keeps_both_missing_conditions_explicit(self):
        result = evidence.verify_bundle(evidence.build_bundle(self.project, [], {}))
        self.assertEqual(result["run_count"], 0)
        self.assertEqual({g["condition"] for g in result["gaps"]}, {"baseline", "alternative"})

    def test_unadmitted_and_unknown_accounting_remain_distinct(self):
        for status, provenance, amount, unresolved in (
            ("queued", "not_admitted", 0, False),
            ("failed", "not_admitted", 0, False),
            ("running", "unknown", None, True),
            ("interrupted", "unknown", None, True),
        ):
            with self.subTest(status=status):
                project, runs, files = fixture(status=status)
                runs[0]["compute"] = {"provenance": provenance, "reserved_cpu_seconds": amount,
                                      "charged_cpu_seconds": amount, "unresolved_liability": unresolved}
                payload = evidence.build_bundle(project, runs, files)
                result = evidence.verify_bundle(payload)
                self.assertEqual(json.loads(members(payload)["runs.json"])[0]["compute"], runs[0]["compute"])
                self.assertEqual(any(g["code"] == "unresolved_compute_liability" for g in result["gaps"]), unresolved)

    def test_unknown_cannot_be_zeroed_and_unadmitted_cannot_hide_charge(self):
        for compute in (
            {"provenance": "unknown", "reserved_cpu_seconds": 0, "charged_cpu_seconds": 0, "unresolved_liability": True},
            {"provenance": "unknown", "reserved_cpu_seconds": None, "charged_cpu_seconds": None, "unresolved_liability": False},
            {"provenance": "not_admitted", "reserved_cpu_seconds": 30, "charged_cpu_seconds": 0, "unresolved_liability": False},
            {"provenance": "not_admitted", "reserved_cpu_seconds": 0, "charged_cpu_seconds": 0, "unresolved_liability": True},
        ):
            with self.subTest(compute=compute):
                project, runs, files = fixture(status="failed")
                runs[0]["compute"] = compute
                with self.assertRaises(ValueError):
                    evidence.build_bundle(project, runs, files)

    def test_known_reservation_remains_unresolved_until_settled(self):
        project, runs, files = fixture(status="interrupted")
        runs[0]["compute"] = {"provenance": "reserved", "reserved_cpu_seconds": 30,
                              "charged_cpu_seconds": 0, "unresolved_liability": True}
        payload = evidence.build_bundle(project, runs, files)
        self.assertTrue(evidence.verify_bundle(payload)["valid"])
        for change in ({"reserved_cpu_seconds": 0}, {"charged_cpu_seconds": 30}, {"unresolved_liability": False}):
            invalid = copy.deepcopy(runs)
            invalid[0]["compute"].update(change)
            with self.assertRaises(ValueError):
                evidence.build_bundle(project, invalid, files)

    def test_private_top_level_fields_are_never_serialized(self):
        secret = "DO-NOT-EXPORT-THIS-CREDENTIAL"
        self.project.update({"members": [{"subject": secret}], "sessions": [secret], "access_key": secret})
        self.runs[0].update({"raw_logs": secret, "working_dir": "/Users/operator/private", "submitted_by": secret})
        payload = evidence.build_bundle(self.project, self.runs, self.files)
        self.assertNotIn(secret.encode(), payload)
        self.assertNotIn(self.project["owner"].encode(), payload)
        self.assertNotIn(b"/Users/operator/private", payload)
        self.assertTrue(evidence.verify_bundle(payload)["valid"])

    def test_unknown_nested_fields_are_rejected(self):
        for field in ("environment", "parameters", "compute", "metrics"):
            with self.subTest(field=field):
                runs = copy.deepcopy(self.runs)
                runs[0][field]["access_key"] = "private"
                with self.assertRaisesRegex(ValueError, "fields"):
                    evidence.build_bundle(self.project, runs, self.files)

    def test_host_paths_and_credentials_in_free_text_are_refused(self):
        for value in ("failed in /Users/operator/private/state", "failed in /data/private", "Bearer pretend-token", "api_key=private", "failed in C:\\Users\\operator"):
            with self.subTest(value=value):
                runs = copy.deepcopy(self.runs)
                runs[0]["error"] = value
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    evidence.build_bundle(self.project, runs, self.files)

    def test_nonfinite_values_refused_in_metadata_and_results(self):
        for value in (float("nan"), float("inf"), -float("inf"), 10 ** 500):
            with self.subTest(value=str(value)[:20]):
                runs = copy.deepcopy(self.runs)
                runs[0]["metrics"]["mean"] = value
                with self.assertRaises(ValueError):
                    evidence.build_bundle(self.project, runs, self.files)
        for token in (b"NaN", b"Infinity", b"1e999"):
            files = dict(self.files)
            files["runs/run1/result.json"] = canonical(self.runs[0]["metrics"]).replace(b'"mean":2.0', b'"mean":' + token)
            with self.assertRaises(ValueError):
                evidence.build_bundle(self.project, self.runs, files)

    def test_symlink_path_objects_are_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "pointer"
            path.symlink_to("/etc/passwd")
            files = dict(self.files)
            files["runs/run1/input.csv"] = path
            with self.assertRaisesRegex(ValueError, "bytes"):
                evidence.build_bundle(self.project, self.runs, files)

    def test_result_cannot_be_missing_or_disagree_with_metrics(self):
        files = dict(self.files)
        del files["runs/run1/result.json"]
        with self.assertRaisesRegex(ValueError, "missing"):
            evidence.build_bundle(self.project, self.runs, files)
        files = dict(self.files)
        changed = copy.deepcopy(self.runs[0]["metrics"])
        changed["mean"] = 9
        files["runs/run1/result.json"] = canonical(changed)
        with self.assertRaisesRegex(ValueError, "do not match"):
            evidence.build_bundle(self.project, self.runs, files)

    def test_duplicate_runs_and_cross_project_lineage_are_refused(self):
        with self.assertRaisesRegex(ValueError, "Duplicate run"):
            evidence.build_bundle(self.project, self.runs * 2, self.files)
        self.runs[0]["project_id"] = "other-project"
        with self.assertRaisesRegex(ValueError, "different project"):
            evidence.build_bundle(self.project, self.runs, self.files)

    def test_record_order_is_deterministic(self):
        _, other_runs, other_files = fixture(run_id="run0")
        runs = self.runs + other_runs
        files = self.files | other_files
        self.assertEqual(evidence.build_bundle(self.project, runs, files), evidence.build_bundle(self.project, list(reversed(runs)), files))

    def test_changed_input_still_refused_when_checksums_are_rewritten(self):
        files = members(self.payload)
        files["runs/run1/input.csv"] = b"group,value\nA,999\n"
        with self.assertRaisesRegex(ValueError, "Input SHA-256"):
            evidence.verify_bundle(rehash(files))

    def test_changed_recipe_still_refused_when_checksums_are_rewritten(self):
        files = members(self.payload)
        files["runs/run1/recipe.py"] += b"# changed\n"
        with self.assertRaisesRegex(ValueError, "Recipe SHA-256"):
            evidence.verify_bundle(rehash(files))

    def test_unknown_metadata_is_refused_even_when_checksums_are_rewritten(self):
        files = members(self.payload)
        project = json.loads(files["project.json"])
        project["members"] = ["private-member"]
        files["project.json"] = canonical(project)
        with self.assertRaisesRegex(ValueError, "project fields"):
            evidence.verify_bundle(rehash(files))

    def test_tampered_or_unlisted_member_refused(self):
        files = members(self.payload)
        files["runs/run1/input.csv"] += b"A,4\n"
        with self.assertRaisesRegex(ValueError, "checksum"):
            evidence.verify_bundle(repack(files))
        files = members(self.payload)
        manifest = json.loads(files["checksums.json"])
        del manifest["files"]["README.md"]
        files["checksums.json"] = canonical(manifest)
        with self.assertRaisesRegex(ValueError, "membership"):
            evidence.verify_bundle(repack(files))

    def test_derived_comparison_and_methods_cannot_be_rewritten(self):
        for name in ("comparison.json", "README.md", "METHODS.md", "ro-crate-metadata.json"):
            with self.subTest(name=name):
                files = members(self.payload)
                files[name] = b"all runs scientifically proven\n"
                with self.assertRaisesRegex(ValueError, "Derived manifests"):
                    evidence.verify_bundle(rehash(files))

    def test_duplicate_json_keys_and_noncanonical_metadata_are_refused(self):
        files = members(self.payload)
        for content in (files["project.json"].replace(b'{', b'{"id":"injected",', 1), json.dumps(json.loads(files["project.json"]), indent=2).encode()):
            with self.subTest(content=content[:30]):
                altered = dict(files)
                altered["project.json"] = content
                with self.assertRaisesRegex(ValueError, "Duplicate JSON|not canonical"):
                    evidence.verify_bundle(rehash(altered))

    def test_traversal_absolute_backslash_alias_and_unexpected_names_refused(self):
        for name in ("../outside", "/absolute", "runs/../input.csv", "runs/run1/./input.csv", "runs\\run1\\input.csv", "runs/run1/raw.log", "runs/run1/input.csv\x00shadow"):
            with self.subTest(name=name):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    with self.assertRaises(ValueError):
                        evidence.verify_bundle(repack(members(self.payload), extra=(name, b"hidden")))

    def test_duplicate_archive_names_refused(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            payload = repack(members(self.payload), extra=("runs/run1/input.csv", b"second version"))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            evidence.verify_bundle(payload)

    def test_symlink_archive_entry_refused(self):
        info = zipfile.ZipInfo("runs/run1/input.csv")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        files = members(self.payload)
        del files["runs/run1/input.csv"]
        with self.assertRaisesRegex(ValueError, "symlinks"):
            evidence.verify_bundle(repack(files, extra=(info, b"/etc/passwd")))

    def test_encrypted_flags_refused_before_read(self):
        payload = bytearray(self.payload)
        struct.pack_into("<H", payload, 6, struct.unpack_from("<H", payload, 6)[0] | 1)
        central = payload.index(b"PK\x01\x02")
        struct.pack_into("<H", payload, central + 8, struct.unpack_from("<H", payload, central + 8)[0] | 1)
        with self.assertRaisesRegex(ValueError, "Encrypted"):
            evidence.verify_bundle(bytes(payload))

    def test_decompression_bomb_refused_from_declared_expansion(self):
        files = members(self.payload)
        files["runs/run1/input.csv"] = b"A" * 64000
        payload = repack(files, mode=zipfile.ZIP_DEFLATED)
        with self.assertRaisesRegex(ValueError, "expansion"):
            evidence.verify_bundle(payload)

    def test_compressed_stream_cannot_hide_expansion_with_false_size(self):
        files = members(self.payload)
        files["runs/run1/input.csv"] = b"A" * 64000
        payload = bytearray(repack(files, mode=zipfile.ZIP_DEFLATED))
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            target = archive.getinfo("runs/run1/input.csv")
        # Lie consistently in both directory and local header, and use the CRC
        # of the declared single byte. The complete deflate stream is still huge.
        struct.pack_into("<I", payload, target.header_offset + 14, zlib.crc32(b"A"))
        struct.pack_into("<I", payload, target.header_offset + 22, 1)
        central_name = payload.index(b"runs/run1/input.csv", target.header_offset + 30 + len(target.filename))
        central = central_name - 46
        self.assertEqual(payload[central:central + 4], b"PK\x01\x02")
        struct.pack_into("<I", payload, central + 16, zlib.crc32(b"A"))
        struct.pack_into("<I", payload, central + 24, 1)
        with self.assertRaisesRegex(ValueError, "Actual member size"):
            evidence.verify_bundle(bytes(payload))

    def test_member_archive_count_total_and_per_file_limits(self):
        files = dict(self.files)
        files["runs/run1/input.csv"] = b"x" * (evidence.MAX_INPUT_BYTES + 1)
        with self.assertRaisesRegex(ValueError, "too large"):
            evidence.build_bundle(self.project, self.runs, files)
        with patch.object(evidence, "MAX_ARCHIVE_BYTES", len(self.payload) - 1):
            with self.assertRaisesRegex(ValueError, "oversized"):
                evidence.verify_bundle(self.payload)
        with patch.object(evidence, "MAX_FILES", 9):
            with self.assertRaisesRegex(ValueError, "member count"):
                evidence.verify_bundle(self.payload)
        with patch.object(evidence, "MAX_TOTAL_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "total uncompressed"):
                evidence.verify_bundle(self.payload)

    def test_local_header_name_and_size_disagreement_refused(self):
        for offset, value in ((30, ord("X")), (22, 255)):
            with self.subTest(offset=offset):
                payload = bytearray(self.payload)
                payload[offset] = value
                with self.assertRaisesRegex(ValueError, "Local"):
                    evidence.verify_bundle(bytes(payload))

    def test_prefix_trailing_data_and_truncated_archives_refused(self):
        for payload in (b"hidden" + self.payload, self.payload + b"hidden", self.payload[:-20], b"not a zip"):
            with self.subTest(size=len(payload)):
                with self.assertRaises(ValueError):
                    evidence.verify_bundle(payload)

    def test_hash_manifest_covers_all_but_itself_including_ro_crate(self):
        files = members(self.payload)
        manifest = json.loads(files["checksums.json"])
        self.assertEqual(set(manifest["files"]), set(files) - {"checksums.json"})
        crate = json.loads(files["ro-crate-metadata.json"])
        graph = {item["@id"]: item for item in crate["@graph"]}
        self.assertEqual(graph["project.json"]["sha256"], digest(files["project.json"]))
        self.assertNotIn("sha256", graph["checksums.json"])
        self.assertNotIn("conformsTo", graph["ro-crate-metadata.json"])


if __name__ == "__main__":
    unittest.main()
