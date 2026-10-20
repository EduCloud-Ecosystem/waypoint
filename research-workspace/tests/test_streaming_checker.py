"""Independent literal-oracle and refusal controls; no workload imports."""

import ast
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from decimal import Inexact, Rounded, localcontext
from unittest import mock


CHECKER_PATH = Path(__file__).resolve().parents[1] / "recipes/assessment/check_summary_fixed6.py"
FIXTURES = Path(__file__).with_name("streaming_checker_fixtures")
SPEC = importlib.util.spec_from_file_location("independent_fixed6_checker", CHECKER_PATH)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


def encoded(value):
    return (json.dumps(value, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def one_group(*, label="x", condition="baseline", original_count=1,
              original_sum="1000000", low="1000000", high="1000000",
              count=1, total="1000000", mean="1.000000000000"):
    """Wrap explicit test-oracle literals; do not calculate reference values."""
    return {
        "schema": "educloud.summary-stream-result/v1",
        "input_profile": {"id": "summary-fixed6-csv", "version": 1},
        "condition": condition,
        "input_rows": original_count,
        "input_sum_scaled": original_sum,
        "count": count,
        "sum_scaled": total,
        "mean": mean,
        "groups": [{
            "group": label, "input_count": original_count,
            "input_sum_scaled": original_sum, "min_scaled": low,
            "max_scaled": high, "count": count, "sum_scaled": total,
            "mean": mean,
        }],
    }


class BoundedReader:
    def __init__(self, data, max_read, fragment=None):
        self.source = io.BytesIO(data)
        self.max_read = max_read
        self.fragment = fragment
        self.requests = []
        self.returned = 0

    def read(self, size):
        if not 0 < size <= self.max_read:
            raise AssertionError("unbounded read request")
        self.requests.append(size)
        block = self.source.read(min(size, self.fragment or size))
        self.returned += len(block)
        return block


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.csv = (FIXTURES / "literal.csv").read_bytes()
        self.baseline = json.loads((FIXTURES / "baseline.json").read_bytes())
        self.alternative = json.loads((FIXTURES / "alternative.json").read_bytes())

    def check(self, data, result, condition="baseline", rows=1):
        raw = result if isinstance(result, bytes) else encoded(result)
        return checker.check_streams(io.BytesIO(data), io.BytesIO(raw), condition, rows)

    def assert_status(self, result, status, reason=None):
        self.assertEqual(set(result), {"schema", "status", "reason"})
        self.assertEqual(result["schema"], "educloud.summary-stream-check/v1")
        self.assertEqual(result["status"], status)
        if reason is not None:
            self.assertEqual(result["reason"], reason)

    def test_literal_cancellation_weighted_totals_and_trim(self):
        for condition, result in (("baseline", self.baseline), ("alternative", self.alternative)):
            with self.subTest(condition=condition):
                self.assert_status(self.check(self.csv, result, condition, 13), "passed", "ok")

    def test_all_equal_group_sizes_one_through_six(self):
        literals = [(1, "7000000", 1, "7000000"), (2, "14000000", 2, "14000000"),
                    (3, "21000000", 3, "21000000"), (4, "28000000", 4, "28000000"),
                    (5, "35000000", 3, "21000000"), (6, "42000000", 4, "28000000")]
        for n, original, used, total in literals:
            data = b"group,value\n" + b"x,7\n" * n
            for condition in ("baseline", "alternative"):
                count, sum_used = (n, original) if condition == "baseline" else (used, total)
                expected = one_group(condition=condition, original_count=n, original_sum=original,
                                     low="7000000", high="7000000", count=count,
                                     total=sum_used, mean="7.000000000000")
                with self.subTest(n=n, condition=condition):
                    self.assert_status(self.check(data, expected, condition, n), "passed")

    def test_half_even_ties_both_signs_and_adjacent_wrong_tick(self):
        literals = [
            ("0.000001", "1", "0", "1", "0.000000007812", "0.000000007813"),
            ("0.000003", "3", "0", "3", "0.000000023438", "0.000000023437"),
            ("-0.000001", "-1", "-1", "0", "-0.000000007812", "-0.000000007813"),
            ("-0.000003", "-3", "-3", "0", "-0.000000023438", "-0.000000023437"),
        ]
        for value, total, low, high, mean, wrong in literals:
            data = b"group,value\nx," + value.encode("ascii") + b"\n" + b"x,0\n" * 127
            expected = one_group(original_count=128, original_sum=total, low=low,
                                 high=high, count=128, total=total, mean=mean)
            with self.subTest(value=value):
                self.assert_status(self.check(data, expected, rows=128), "passed")
                altered = copy.deepcopy(expected)
                altered["mean"] = altered["groups"][0]["mean"] = wrong
                self.assert_status(self.check(data, altered, rows=128), "failed", "result_mismatch")

    def test_normalizes_signed_zero_but_refuses_negative_zero_result(self):
        expected = one_group(original_count=3, original_sum="0", low="0", high="0",
                             count=3, total="0", mean="0.000000000000")
        data = b"group,value\nx,-0\nx,+0.000000\nx,-0.000000\n"
        self.assert_status(self.check(data, expected, rows=3), "passed")
        expected["mean"] = "-0.000000000000"
        self.assert_status(self.check(data, expected, rows=3), "failed", "result_invalid")

    def test_extreme_grammar_and_88_byte_crlf_record(self):
        label = "a" * 64
        row = label.encode() + b",+1000000000000.000000\r\n"
        self.assertEqual(len(row), 88)
        data = b"group,value\r\n" + row
        expected = one_group(label=label, original_sum="1000000000000000000",
                             low="1000000000000000000", high="1000000000000000000",
                             total="1000000000000000000", mean="1000000000000.000000000000")
        self.assert_status(self.check(data, expected), "passed")
        with localcontext() as context:
            context.prec = 64
            context.traps[Inexact] = context.traps[Rounded] = True
            scanner = checker.InputScanner(1)
            scanner.feed(data)
            scanner.finish()
        self.assertEqual(scanner.max_record_bytes, 88)
        self.assertEqual(scanner.max_group_bytes, 64)
        self.assertEqual(scanner.max_numeric_bytes, 21)

    def test_negative_cap_and_one_micro_above_endpoint(self):
        expected = one_group(original_sum="-1000000000000000000",
                             low="-1000000000000000000", high="-1000000000000000000",
                             total="-1000000000000000000", mean="-1000000000000.000000000000")
        self.assert_status(self.check(b"group,value\nx,-1000000000000.000000\n", expected), "passed")
        for value in (b"1000000000000.000001", b"-1000000000000.000001", b"1000000000001"):
            self.assert_status(self.check(b"group,value\nx," + value + b"\n", expected),
                               "failed", "input_invalid")

    def test_every_byte_split_and_one_byte_fragmentation(self):
        data = b"group,value\r\n x ,+0.000001\r\n"
        expected = one_group(label=" x ", original_sum="1", low="1", high="1",
                             total="1", mean="0.000001000000")
        for split in range(len(data) + 1):
            with self.subTest(split=split), localcontext() as context:
                context.prec = 64
                scanner = checker.InputScanner(1)
                scanner.feed(data[:split])
                scanner.feed(data[split:])
                scanner.finish()
                self.assertEqual(scanner.rows, 1)
                self.assertEqual(scanner.groups[" x "].total(), checker.Decimal("0.000001"))
        source = BoundedReader(data, 65536, fragment=1)
        result = BoundedReader(encoded(expected), 4096, fragment=1)
        self.assert_status(checker.check_streams(source, result, "baseline", 1), "passed")

    def test_more_than_two_real_input_blocks_remains_bounded(self):
        label = "a" * 64
        data = b"group,value\n" + (label.encode() + b",0\n") * 2000
        self.assertGreater(len(data), 2 * 65536)
        expected = one_group(label=label, original_count=2000, original_sum="0",
                             low="0", high="0", count=2000, total="0", mean="0.000000000000")
        source = BoundedReader(data, 65536)
        result = BoundedReader(encoded(expected), 4096)
        self.assert_status(checker.check_streams(source, result, "baseline", 2000), "passed")
        self.assertEqual(max(source.requests), 65536)
        self.assertEqual(source.returned, len(data))

    def test_input_lexical_refusals(self):
        bad_rows = [
            b",1\n", b"x,\n", b"x,+\n", b"x,-\n", b"x,.5\n", b"x,1.\n",
            b"x,1e0\n", b"x,NaN\n", b"x,Inf\n", b"x,0x1\n", b"x,1_000\n",
            b"x, 1\n", b"x,1 \n", b"x,01\n", b"x,+01\n", b"x,00.1\n",
            b"x,1.0000001\n", b"x,10000000000000\n", b"x,1,2\n",
            b'"x",1\n', b"x,1\x00\n", b"x,1\r", b"x,1", b"x,1\r\r\n",
            b"\n", b"x\t,1\n", b"\xff,1\n", b"x,1\njunk",
            b"a" * 65 + b",1\n",
        ]
        for row in bad_rows:
            with self.subTest(row=row):
                self.assert_status(self.check(b"group,value\n" + row, one_group()),
                                   "failed", "input_invalid")
        for header in (b"\xef\xbb\xbfgroup,value\n", b"group,Value\n",
                       b"group,value", b"group,value\rX", b" group,value\n"):
            self.assert_status(self.check(header + b"x,1\n", one_group()), "failed", "input_invalid")

    def test_exact_rows_and_tail_eof_required(self):
        for data, rows in ((b"group,value\nx,1\n", 2),
                           (b"group,value\nx,1\nx,1\n", 1),
                           (b"group,value\n", 1)):
            self.assert_status(self.check(data, one_group(), rows=rows), "failed", "input_invalid")
        for rows in (True, 0, -1, 1.0, "1", 1000001):
            self.assert_status(self.check(b"group,value\nx,1\n", one_group(), rows=rows),
                               "failed", "input_invalid")

    def test_128_groups_and_129th_refusal(self):
        groups = [one_group(label=f"{n:03d}", original_sum="0", low="0", high="0",
                            total="0", mean="0.000000000000")["groups"][0] for n in range(128)]
        expected = one_group(original_count=128, original_sum="0", low="0", high="0",
                             count=128, total="0", mean="0.000000000000")
        expected["groups"] = groups
        data = b"group,value\n" + b"".join(f"{n:03d},0\n".encode() for n in range(128))
        self.assert_status(self.check(data, expected, rows=128), "passed")
        self.assert_status(self.check(data + b"128,0\n", expected, rows=129), "failed", "input_invalid")

    def test_logical_byte_and_row_counter_boundaries_not_large_input(self):
        row = b"x,1\n"
        scanner = checker.InputScanner(1)
        scanner.feed(b"group,value\n")
        scanner.total_bytes = checker.MAX_INPUT_BYTES - len(row)
        scanner.feed(row)
        scanner.finish()
        self.assertEqual(scanner.total_bytes, checker.MAX_INPUT_BYTES)
        scanner = checker.InputScanner(1)
        scanner.feed(b"group,value\n")
        scanner.total_bytes = checker.MAX_INPUT_BYTES - len(row) + 1
        with self.assertRaises(checker.InputInvalid):
            scanner.feed(row)
        scanner = checker.InputScanner(checker.MAX_ROWS)
        scanner.feed(b"group,value\n")
        scanner.rows = checker.MAX_ROWS - 1
        scanner.feed(row)
        scanner.finish()
        with self.assertRaises(checker.InputInvalid):
            scanner.feed(row)

    def test_each_mathematical_field_is_checked_against_input(self):
        mutations = [
            lambda x: x.update(condition="alternative"),
            lambda x: x.update(input_rows=14),
            lambda x: x.update(input_sum_scaled="18000002"),
            lambda x: x.update(count=14),
            lambda x: x.update(sum_scaled="18000002"),
            lambda x: x.update(mean="1.384615461539"),
            lambda x: x["groups"][1].update(input_count=5),
            lambda x: x["groups"][1].update(input_sum_scaled="6000001"),
            lambda x: x["groups"][1].update(min_scaled="-1"),
            lambda x: x["groups"][1].update(max_scaled="3000001"),
            lambda x: x["groups"][1].update(count=3),
            lambda x: x["groups"][1].update(sum_scaled="6000001"),
            lambda x: x["groups"][1].update(mean="1.500000000001"),
            lambda x: x["groups"][1].update(group="002"),
        ]
        for index, mutate in enumerate(mutations):
            altered = copy.deepcopy(self.baseline)
            mutate(altered)
            with self.subTest(index=index):
                self.assert_status(self.check(self.csv, altered, rows=13), "failed", "result_mismatch")

    def test_self_consistent_wrong_dataset_cannot_pass(self):
        different = one_group(original_sum="2000000", low="2000000", high="2000000",
                              total="2000000", mean="2.000000000000")
        self.assert_status(self.check(b"group,value\nx,1\n", different), "failed", "result_mismatch")

    def test_unweighted_global_mean_and_remove_all_extrema_refuse(self):
        changed = copy.deepcopy(self.alternative)
        changed["mean"] = "1.375000083333"
        self.assert_status(self.check(self.csv, changed, "alternative", 13), "failed", "result_mismatch")
        changed = copy.deepcopy(self.alternative)
        # Removing every repeated -5/9 leaves only the value 2, not three values.
        changed["groups"][2].update(count=1, sum_scaled="2000000")
        changed.update(count=9, sum_scaled="10000001", mean="1.111111222222")
        self.assert_status(self.check(self.csv, changed, "alternative", 13), "failed", "result_mismatch")

    def test_closed_result_shape_and_scalar_types(self):
        mutations = [
            lambda x: x.update(extra=0), lambda x: x.pop("count"),
            lambda x: x.update(Count=x.pop("count")), lambda x: x.update(count=True),
            lambda x: x.update(count=1.0), lambda x: x.update(count=None),
            lambda x: x.update(count=0), lambda x: x.update(input_rows=-1),
            lambda x: x.update(groups=[]), lambda x: x.update(groups={}),
            lambda x: x["input_profile"].update(version=True),
            lambda x: x["input_profile"].update(extra=1),
            lambda x: x["groups"][0].update(extra=1),
            lambda x: x["groups"][0].update(input_count=False),
            lambda x: x["groups"].reverse(),
            lambda x: x["groups"].append(copy.deepcopy(x["groups"][-1])),
        ]
        for index, mutate in enumerate(mutations):
            altered = copy.deepcopy(self.baseline)
            mutate(altered)
            with self.subTest(index=index):
                self.assert_status(self.check(self.csv, altered, rows=13), "failed", "result_invalid")

    def test_noncanonical_numeric_strings_refused(self):
        for bad in ("+1", "01", "-0", "1e0", "1.0", " 1", "NaN", "9" * 27):
            changed = copy.deepcopy(self.baseline)
            changed["sum_scaled"] = bad
            with self.subTest(scaled=bad):
                self.assert_status(self.check(self.csv, changed, rows=13), "failed", "result_invalid")
        for bad in ("+1.000000000000", "01.000000000000", "1.0",
                    "1.0000000000000", "1e0", "nan", "-0.000000000000",
                    "1000000000000.000000000001"):
            changed = copy.deepcopy(self.baseline)
            changed["mean"] = bad
            with self.subTest(mean=bad):
                self.assert_status(self.check(self.csv, changed, rows=13), "failed", "result_invalid")

    def test_json_duplicate_alias_nonfinite_trailing_and_truncation(self):
        original = encoded(self.baseline)
        bad = [
            original.replace(b'"count":13', b'"count":13,"count":13', 1),
            original.replace(b'"count":13', b'"count":13,"\\u0063ount":13', 1),
            original.replace(b'"count":13', b'"count":1e1', 1),
            original.replace(b'"count":13', b'"count":NaN', 1),
            original.replace(b'"count":13', b'"count":-0', 1),
            original.replace(b'"count":13', b'"count":01', 1),
            original + b"{}", original[:-3], b"\xef\xbb\xbf" + original,
            original.replace(b'"001"', b'"\\ud800"', 1),
        ]
        for raw in bad:
            self.assert_status(self.check(self.csv, raw, rows=13), "failed", "result_invalid")

    def test_json_preflight_refuses_before_object_construction(self):
        bad = [b'[[[[[]]]]]', b'["' + b"x" * 81 + b'"]',
               b'["' + b"\\u0041" * 81 + b'"]', b"[10000000]",
               b"[" + b"0," * 4096 + b"0]", b"[" + b"{}," * 131 + b"{}]"]
        with mock.patch.object(checker.json, "loads", side_effect=AssertionError("must preflight first")):
            for raw in bad:
                with self.subTest(raw_prefix=raw[:20]):
                    self.assert_status(self.check(b"group,value\nx,1\n", raw), "failed", "result_invalid")

    def test_result_read_cap_consumes_at_most_one_overflow_byte(self):
        source = BoundedReader(b"group,value\nx,1\n", 65536)
        result = BoundedReader(b" " * (61440 + 1000), 4096)
        self.assert_status(checker.check_streams(source, result, "baseline", 1),
                           "failed", "result_invalid")
        self.assertEqual(result.returned, 61441)
        legal = encoded(one_group())
        legal += b" " * (61440 - len(legal))
        self.assert_status(self.check(b"group,value\nx,1\n", legal), "passed")

    def test_io_and_internal_failure_never_pass_or_echo(self):
        class Unavailable:
            def read(self, size):
                raise OSError("private-path-and-label")
        class Interrupted:
            def read(self, size):
                raise KeyboardInterrupt
        self.assert_status(checker.check_streams(Unavailable(), io.BytesIO(b""), "baseline", 1),
                           "unknown", "input_unavailable")
        self.assert_status(checker.check_streams(Interrupted(), io.BytesIO(b""), "baseline", 1),
                           "unknown", "internal_failure")
        with mock.patch.object(checker, "_compare", side_effect=RuntimeError("PRIVATE-POISON")):
            result = self.check(b"group,value\nx,1\n", one_group())
        self.assert_status(result, "unknown", "internal_failure")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_input_and_diagnostics_do_not_enter_quality_projection(self):
        poison = b"PRIVATE_LABEL_VALUE"
        result = self.check(b"group,value\n" + poison + b",NaN\n", one_group())
        self.assert_status(result, "failed", "input_invalid")
        self.assertNotIn(poison.decode(), json.dumps(result))
        self.assertLess(len(encoded(result)), 1024)

    def test_checker_has_no_workload_or_external_imports(self):
        tree = ast.parse(CHECKER_PATH.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, {"__future__", "json", "os", "re", "stat", "sys",
                                       "decimal", "fractions"})
        self.assertNotIn("summary_fixed6", imported)


def run_cli(args, result):
    """Actual tiny process with bounded stdout/stderr and ten-second deadline."""
    environment = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C",
                   "PYTHONHASHSEED": "0"}
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(CHECKER_PATH), *args],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=environment,
    )
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + 10
    try:
        # Inputs here are all under PIPE_BUF; no large/unbounded write producer.
        if len(result) > 4096:
            raise AssertionError("CLI fixture exceeds tiny input envelope")
        process.stdin.write(result)
        process.stdin.close()
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("checker timed out")
            for key, _ in selector.select(min(remaining, 0.25)):
                block = os.read(key.fd, 1025)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                outputs[name].extend(block)
                if len(outputs[name]) > (1024 if name == "stdout" else 256):
                    raise AssertionError("checker output overflow")
        code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return code, bytes(outputs["stdout"]), bytes(outputs["stderr"])
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()


class CheckerCLITests(unittest.TestCase):
    def test_actual_cli_literal_baseline_and_alternative(self):
        for condition in ("baseline", "alternative"):
            with self.subTest(condition=condition):
                code, out, err = run_cli(
                    [str(FIXTURES / "literal.csv"), condition, "13"],
                    (FIXTURES / f"{condition}.json").read_bytes(),
                )
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out), {
                    "schema": "educloud.summary-stream-check/v1", "status": "passed", "reason": "ok",
                })
                self.assertTrue(out.endswith(b"\n"))
                self.assertEqual(err, b"")

    def test_actual_cli_mismatch_and_bad_result_are_failed(self):
        for raw, reason in ((b"{}", "result_invalid"),
                            (encoded(one_group()), "result_mismatch")):
            with self.subTest(reason=reason):
                code, out, err = run_cli([str(FIXTURES / "literal.csv"), "baseline", "13"], raw)
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out)["status"], "failed")
                self.assertEqual(json.loads(out)["reason"], reason)
                self.assertEqual(err, b"")

    def test_actual_cli_missing_file_and_stdin_alias_refuse(self):
        with tempfile.TemporaryDirectory(prefix="checker-unavailable-") as directory:
            path = str(Path(directory) / "PRIVATE_MISSING.csv")
            code, out, err = run_cli([path, "baseline", "1"], encoded(one_group()))
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(out)["reason"], "input_unavailable")
            self.assertNotIn(path.encode(), out + err)
        code, out, err = run_cli(["-", "baseline", "1"], encoded(one_group()))
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["reason"], "input_invalid")
        self.assertEqual(err, b"")

    def test_actual_cli_noncanonical_row_count_and_poison_input(self):
        code, out, err = run_cli([str(FIXTURES / "literal.csv"), "baseline", "013"], b"{}")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["reason"], "input_invalid")
        self.assertEqual(err, b"")
        with tempfile.TemporaryDirectory(prefix="checker-poison-") as directory:
            path = Path(directory) / "PRIVATE_FILENAME.csv"
            path.write_bytes(b"group,value\nPRIVATE_LABEL,NaN\n")
            code, out, err = run_cli([str(path), "baseline", "1"], b"{}")
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)["reason"], "input_invalid")
            self.assertNotIn(b"PRIVATE", out + err)
            self.assertEqual(err, b"")


if __name__ == "__main__":
    unittest.main()
