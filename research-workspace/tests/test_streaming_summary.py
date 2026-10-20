"""Candidate-only controls; ordinary catalog and execution paths stay separate."""

from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from research_workspace import assessment_catalog


ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "recipes/assessment/summary_fixed6.py"
R_RECIPE = ROOT / "recipes/assessment/summary_fixed6.R"
CHECKER = ROOT / "recipes/assessment/check_summary_fixed6.py"
FIXTURES = Path(__file__).parent / "streaming_summary_fixtures"
SPEC = importlib.util.spec_from_file_location("candidate_fixed6", RECIPE)
recipe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recipe)


def bounded_process(command, body=b"", timeout=20):
    """Actual CLI with bounded concurrent stdin/stdout/stderr and final drain."""
    environment = {
        key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    environment.update(LC_ALL="C", LANG="C", PYTHONDONTWRITEBYTECODE="1")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=environment)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    streams = {}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    offset = 0
    try:
        for name in output:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
            streams[name] = stream
        if body:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            process.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("candidate process exceeded test deadline")
            for key, _ in selector.select(min(0.2, remaining)):
                if key.data == "stdin":
                    try:
                        # Short writes exercise the real binary stream path.
                        written = os.write(key.fd, body[offset:offset + 37])
                    except BrokenPipeError:
                        written = 0
                        offset = len(body)
                    except BlockingIOError:
                        continue
                    offset += written
                    if offset >= len(body):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                else:
                    try:
                        chunk = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    else:
                        output[key.data].extend(chunk)
                        ceiling = 61440 if key.data == "stdout" else 256
                        if len(output[key.data]) > ceiling:
                            raise AssertionError("candidate exceeded output bound")
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return code, bytes(output["stdout"]), bytes(output["stderr"])
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


class Chunks:
    def __init__(self, data, width):
        self.data, self.width, self.offset = data, width, 0
        self.requests = []

    def read(self, size):
        if not 1 <= size <= 65536:
            raise AssertionError("unbounded read")
        self.requests.append(size)
        end = min(len(self.data), self.offset + min(self.width, size))
        block = self.data[self.offset:end]
        self.offset = end
        return block


class StreamingMathTests(unittest.TestCase):
    def summary(self, data, condition="baseline", rows=None):
        return recipe.summarize(io.BytesIO(data), condition,
                                rows if rows is not None else data.count(b"\n") - 1)

    def test_literal_results_and_every_fragment_width(self):
        data = (FIXTURES / "basic.csv").read_bytes()
        for condition in ("baseline", "alternative"):
            expected = json.loads((FIXTURES / f"basic-{condition}.json").read_bytes())
            for width in range(1, len(data) + 1):
                with self.subTest(condition=condition, width=width):
                    source = Chunks(data, width)
                    self.assertEqual(recipe.summarize(source, condition, 8), expected)
                    self.assertTrue(source.requests)

    def test_trim_threshold_ties_and_weighting(self):
        for count in range(1, 7):
            result = self.summary(b"group,value\n" + b"g,1\n" * count, "alternative")
            self.assertEqual(result["count"], count - 2 if count >= 5 else count)
            self.assertEqual(result["mean"], "1.000000000000")
        data = b"group,value\na,0\na,0\na,0\nb,100\n"
        self.assertEqual(self.summary(data)["mean"], "25.000000000000")

    def test_extreme_cancellation_and_alternative(self):
        data = b"group,value\ng,1000000000000\ng,0.000001\ng,-1000000000000\n"
        result = self.summary(data)
        self.assertEqual(result["sum_scaled"], "1")
        self.assertEqual(result["mean"], "0.000000333333")
        trimmed = self.summary(data + b"g,0\ng,0\n", "alternative")
        self.assertEqual(trimmed["sum_scaled"], "1")
        self.assertEqual(trimmed["count"], 3)
        self.assertEqual(trimmed["mean"], result["mean"])

    def test_half_even_rounding_and_negative_zero(self):
        for total, expected in ((1, "0.000000007812"), (3, "0.000000023438"),
                                (-1, "-0.000000007812"), (-3, "-0.000000023438")):
            self.assertEqual(recipe.mean_text(total, 128), expected)
        self.assertEqual(recipe.mean_text(0, 1), "0.000000000000")
        self.assertEqual(recipe.mean_text(10**24, 1000000), "1000000000000.000000000000")
        self.assertEqual(recipe.mean_text(-(10**24), 1000000), "-1000000000000.000000000000")

    def test_ascii_label_identity_and_order(self):
        data = b"group,value\nNA,1\n001,1\nNULL,1\n space ,1\n.-_,1\n"
        labels = [row["group"] for row in self.summary(data)["groups"]]
        self.assertEqual(labels, [" space ", ".-_", "001", "NA", "NULL"])

    def test_stream_exceeds_two_blocks_without_row_retention(self):
        data = b"group,value\n" + (b"g" * 64 + b",1\n") * 2200
        source = Chunks(data, 65536)
        result = recipe.summarize(source, "baseline", 2200)
        self.assertGreater(len(data), 2 * recipe.CHUNK_BYTES)
        self.assertEqual(result["count"], 2200)
        self.assertEqual(len(result["groups"]), 1)
        self.assertLessEqual(max(source.requests), 65536)

    def test_maximum_field_record_and_group_boundaries(self):
        row = b"g" * 64 + b",-1000000000000.000000\r\n"
        self.assertEqual(len(row), 88)
        self.assertEqual(self.summary(b"group,value\r\n" + row)["count"], 1)
        for bad in (b"g" * 65 + b",0\n", b"g,-1000000000000.0000000\n", row[:-2] + b" \r\n"):
            with self.subTest(kind=len(bad)):
                with self.assertRaises(recipe.InvalidInput):
                    self.summary(b"group,value\n" + bad)
        data = b"group,value\n" + b"".join(f"g{i:03d},1\n".encode() for i in range(128))
        self.assertEqual(len(self.summary(data)["groups"]), 128)
        with self.assertRaises(recipe.InvalidInput):
            self.summary(data + b"g128,1\n")

    def test_grammar_rejects_before_conversion(self):
        invalid = [
            b"NaN", b"Inf", b"-Infinity", b"1e3", b".5", b"1.", b" 1", b"1 ",
            b"01", b"0x10", b"1_0", b"0.0000001", b"1000000000000.000001",
            b"1\x00", b"\xff", b'"1"', b"1\r2", b"",
        ]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(recipe.InvalidInput):
                    self.summary(b"group,value\ng," + value + b"\n")
        for data in (b"\xef\xbb\xbfgroup,value\ng,1\n", b"group,value\n",
                     b"group,value\ng,1", b"group,value\n\n", b"group,value\ng,1,2\n",
                     b"group,value\n\xff,1\n", b"group,value\n\"g\",1\n"):
            with self.subTest(data=data):
                with self.assertRaises(recipe.InvalidInput):
                    self.summary(data, rows=1)

    def test_no_ignored_tail_missing_rows_or_coercive_count(self):
        data = b"group,value\ng,1\n"
        for rows in (True, 0, 2, 1.0, 1000001):
            with self.subTest(rows=rows):
                with self.assertRaises(recipe.InvalidInput):
                    self.summary(data, rows=rows)
        with self.assertRaises(recipe.InvalidInput):
            self.summary(data + b"g,2\n", rows=1)

    def test_counter_boundaries_are_logical_not_large_runtime_claims(self):
        data = b"group,value\ng,1\ng,2\n"
        with patch.object(recipe, "MAX_ROWS", 1):
            with self.assertRaises(recipe.InvalidInput):
                self.summary(data, rows=1)
        exact = b"group,value\ng,1\n"
        with patch.object(recipe, "MAX_BYTES", len(exact)):
            self.assertEqual(self.summary(exact)["count"], 1)
            with self.assertRaises(recipe.InvalidInput):
                self.summary(exact + b"x", rows=1)
        with self.assertRaises(ArithmeticError):
            recipe.mean_text(10**24 + 1, 1000000)

    def test_valid_large_result_and_independent_width_bound(self):
        data = b"group,value\n" + b"".join(
            (f"g{i:03d}" + "x" * 60 + ",-1000000000000.000000\r\n").encode()
            for i in range(128))
        encoded = recipe.encode_result(self.summary(data))
        self.assertLessEqual(len(encoded), 40496)
        self.assertGreater(len(encoded), 30000)
        # A separate key/literal-width count; not a second scientific dataset.
        group = {"group": "g" * 64, "input_count": 1000000,
                 "input_sum_scaled": "-" + "9" * 25,
                 "min_scaled": "-" + "9" * 19, "max_scaled": "-" + "9" * 19,
                 "count": 1000000, "sum_scaled": "-" + "9" * 25,
                 "mean": "-" + "9" * 13 + "." + "9" * 12}
        maximum = {"schema": "educloud.summary-stream-result/v1",
                   "input_profile": {"id": "summary-fixed6-csv", "version": 1},
                   "condition": "alternative", "input_rows": 1000000,
                   "input_sum_scaled": "-" + "9" * 25, "count": 1000000,
                   "sum_scaled": "-" + "9" * 25,
                   "mean": "-" + "9" * 13 + "." + "9" * 12, "groups": [group] * 128}
        self.assertEqual(len(json.dumps(maximum, separators=(",", ":")).encode()) + 1, 40496)
        with patch.object(recipe, "MAX_OUTPUT_BYTES", len(encoded) - 1):
            with self.assertRaises(ArithmeticError):
                recipe.encode_result(self.summary(data))


class CandidateCatalogTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "recipes/assessment").mkdir(parents=True)
        for source in (RECIPE, R_RECIPE):
            shutil.copyfile(source, self.root / "recipes/assessment" / source.name)
        if CHECKER.exists():
            shutil.copyfile(CHECKER, self.root / "recipes/assessment" / CHECKER.name)
        self.document = json.loads((ROOT / "assessment-environments.json").read_bytes())
        self.write()
        self.patcher = patch.object(assessment_catalog, "ROOT", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def write(self):
        (self.root / "assessment-environments.json").write_text(json.dumps(self.document))

    def test_all_readiness_false_sources_match_and_no_fake_runtime(self):
        catalog = assessment_catalog.load_catalog()
        self.assertTrue(all(value is False for value in catalog["readiness"].values()))
        for entry in assessment_catalog.list_candidates():
            self.assertIsNone(entry["runtime"]["image"])
            self.assertIsNone(entry["runtime"]["interpreter_identity_sha256"])
            code = assessment_catalog.candidate_source(entry["environment_id"])
            self.assertEqual(hashlib.sha256(code).hexdigest(), entry["code_sha256"])
        with self.assertRaises(ValueError):
            assessment_catalog.candidate_source("../summary-python")

    def test_unknown_checker_pin_is_unavailable_not_fake_identity(self):
        self.document["checker"].update(code_sha256=None, status="unavailable")
        self.write()
        self.assertEqual(assessment_catalog.load_catalog()["checker"]["status"], "unavailable")
        with self.assertRaises(ValueError):
            assessment_catalog.checker_source()

    def test_closed_profile_and_no_truthy_gate(self):
        original = deepcopy(self.document)
        mutations = [
            lambda x: x["readiness"].update(execution_authorized=True),
            lambda x: x["readiness"].update(cleanup_trusted=0),
            lambda x: x["input_profile"].update(version=True),
            lambda x: x["input_profile"].update(max_input_bytes=5000000000),
            lambda x: x["candidates"][0]["runtime"].update(image="python:latest"),
            lambda x: x["candidates"][0].update(source="../recipe.py"),
            lambda x: x["candidates"].append(x["candidates"][0]),
            lambda x: x.update(authorization=True),
        ]
        for mutate in mutations:
            self.document = deepcopy(original)
            mutate(self.document)
            self.write()
            with self.assertRaises(ValueError):
                assessment_catalog.load_catalog()

    def test_source_mutation_symlink_and_special_file_refused(self):
        path = self.root / "recipes/assessment" / RECIPE.name
        old = path.read_bytes()
        path.write_bytes(old + b"\n# changed\n")
        with self.assertRaises(ValueError):
            assessment_catalog.load_catalog()
        path.unlink()
        path.symlink_to(RECIPE)
        with self.assertRaises(ValueError):
            assessment_catalog.load_catalog()
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            assessment_catalog.load_catalog()

    def test_duplicate_catalog_key_refused(self):
        path = self.root / "assessment-environments.json"
        path.write_text('{"schema":"waypoint.assessment-candidates/v1",' + path.read_text()[1:])
        with self.assertRaises(ValueError):
            assessment_catalog.load_catalog()


class PythonCLITests(unittest.TestCase):
    command = [sys.executable, "-I", "-B", str(RECIPE)]

    def invoke(self, data, condition, rows):
        return bounded_process(self.command + ["-", condition, str(rows)], data)

    def test_real_streamed_literal_conditions(self):
        data = (FIXTURES / "basic.csv").read_bytes()
        for condition in ("baseline", "alternative"):
            code, output, error = self.invoke(data, condition, 8)
            self.assertEqual((code, error), (0, b""))
            self.assertEqual(json.loads(output), json.loads(
                (FIXTURES / f"basic-{condition}.json").read_bytes()))

    def test_actual_rejection_has_no_payload_or_filename_diagnostic(self):
        code, output, error = self.invoke(b"group,value\nPRIVATE_LABEL_SENTINEL,nan\n", "baseline", 1)
        self.assertEqual((code, output, error), (1, b"", b"SUMMARY_INPUT_INVALID\n"))
        code, output, error = bounded_process(self.command + [
            "/no/such/PRIVATE_FILENAME_SENTINEL", "baseline", "1"])
        self.assertEqual((code, output, error), (2, b"", b"SUMMARY_INPUT_UNAVAILABLE\n"))
        for rows in ("01", "1\n", "true", "1.0"):
            code, output, error = self.invoke(b"group,value\ng,1\n", "baseline", rows)
            self.assertEqual((code, output, error), (1, b"", b"SUMMARY_INPUT_INVALID\n"))

    def test_real_streamed_cancellation_precision_and_rounding(self):
        for value, expected in (("0.000001", "0.000000007812"),
                                ("0.000003", "0.000000023438"),
                                ("-0.000001", "-0.000000007812")):
            data = b"group,value\n" + f"g,{value}\n".encode() + b"g,0\n" * 127
            code, output, error = self.invoke(data, "baseline", 128)
            self.assertEqual((code, error), (0, b""))
            self.assertEqual(json.loads(output)["mean"], expected)
        data = b"group,value\ng,1000000000000\ng,0.000001\ng,-1000000000000\ng,0\ng,0\n"
        code, output, error = self.invoke(data, "alternative", 5)
        self.assertEqual((code, error), (0, b""))
        self.assertEqual(json.loads(output)["sum_scaled"], "1")
        self.assertEqual(json.loads(output)["mean"], "0.000000333333")

    def test_actual_two_block_stream_and_valid_large_form(self):
        data = b"group,value\n" + (b"g" * 64 + b",1\n") * 2200
        code, output, error = self.invoke(data, "baseline", 2200)
        self.assertEqual((code, error), (0, b""))
        self.assertEqual(json.loads(output)["count"], 2200)
        data = b"group,value\n" + b"".join(
            (f"g{i:03d}" + "x" * 60 + ",-1000000000000.000000\r\n").encode()
            for i in range(128))
        code, output, error = self.invoke(data, "alternative", 128)
        self.assertEqual((code, error), (0, b""))
        self.assertEqual(len(json.loads(output)["groups"]), 128)
        self.assertLessEqual(len(output), 40496)

    def test_actual_crlf_is_valid_internal_carriage_return_is_invalid(self):
        code, output, error = self.invoke(b"group,value\r\ng,1\r\n", "baseline", 1)
        self.assertEqual((code, error), (0, b""))
        self.assertEqual(json.loads(output)["mean"], "1.000000000000")
        code, output, error = self.invoke(b"group,value\ng,1\r2\n", "baseline", 1)
        self.assertEqual((code, output, error), (1, b"", b"SUMMARY_INPUT_INVALID\n"))


@unittest.skipUnless(shutil.which("Rscript"), "Rscript unavailable: actual R candidate NOT RUN")
class RCLITests(PythonCLITests):
    command = [shutil.which("Rscript") or "Rscript", "--vanilla", str(R_RECIPE)]


@unittest.skipUnless(CHECKER.exists(), "Independent checker not integrated: cross-CLI NOT RUN")
class CrossCheckerTests(unittest.TestCase):
    def check(self, path, output, condition, rows):
        return bounded_process([sys.executable, "-I", "-B", str(CHECKER),
                                str(path), condition, str(rows)], output)

    def test_python_and_available_r_against_actual_independent_checker(self):
        commands = [[sys.executable, "-I", "-B", str(RECIPE)]]
        if shutil.which("Rscript"):
            commands.append([shutil.which("Rscript"), "--vanilla", str(R_RECIPE)])
        for command in commands:
            for condition in ("baseline", "alternative"):
                path = FIXTURES / "basic.csv"
                code, output, error = bounded_process(command + [str(path), condition, "8"])
                self.assertEqual((code, error), (0, b""))
                checked, report, diagnostic = self.check(path, output, condition, 8)
                self.assertEqual((checked, diagnostic), (0, b""))
                self.assertEqual(json.loads(report)["status"], "passed")
                changed = json.loads(output)
                changed["sum_scaled"] = "900000000"
                changed["mean"] = "112.500000000000"
                checked, report, _ = self.check(path, json.dumps(changed).encode(), condition, 8)
                self.assertEqual(checked, 1)
                self.assertEqual(json.loads(report)["status"], "failed")

    def test_actual_checker_covers_cancellation_rounding_and_large_forms(self):
        commands = [[sys.executable, "-I", "-B", str(RECIPE)]]
        if shutil.which("Rscript"):
            commands.append([shutil.which("Rscript"), "--vanilla", str(R_RECIPE)])
        cases = [
            (b"group,value\ng,1000000000000\ng,0.000001\ng,-1000000000000\ng,0\ng,0\n",
             "alternative", 5),
            (b"group,value\ng,-0.000003\n" + b"g,0\n" * 127, "baseline", 128),
            (b"group,value\n" + b"".join(
                (f"g{i:03d}" + "x" * 60 + ",-1000000000000.000000\r\n").encode()
                for i in range(128)), "alternative", 128),
            (b"group,value\n" + (b"g" * 64 + b",1\n") * 2200, "baseline", 2200),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.csv"
            for case, (data, condition, rows) in enumerate(cases):
                path.write_bytes(data)
                for language, command in enumerate(commands):
                    with self.subTest(case=case, language=language):
                        code, output, error = bounded_process(command + [str(path), condition, str(rows)])
                        self.assertEqual((code, error), (0, b""))
                        checked, report, diagnostic = self.check(path, output, condition, rows)
                        self.assertEqual((checked, diagnostic), (0, b""))
                        self.assertEqual(json.loads(report)["status"], "passed")


if __name__ == "__main__":
    unittest.main()
