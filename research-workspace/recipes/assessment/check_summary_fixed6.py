"""Independent, ephemeral reference checker for summary-fixed6-csv/v1.

The input lexer is a byte state machine; arithmetic uses anchored Decimal
deviations, and the rounding oracle uses exact Fraction intervals/tie parity.
No workload implementation, parser, accumulator or formatter is imported.
This verifies the supplied sample, not its ownership, scientific sampling,
resource measurements, admission or cleanup. No files are written.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from decimal import Decimal, Inexact, Rounded, localcontext
from fractions import Fraction


MAX_ROWS = 1_000_000
MAX_GROUPS = 128
MAX_INPUT_BYTES = 88_000_013
CHUNK_BYTES = 65_536
MAX_RESULT_BYTES = 61_440
RESULT_SCHEMA = "educloud.summary-stream-result/v1"
CHECK_SCHEMA = "educloud.summary-stream-check/v1"
INPUT_PROFILE = {"id": "summary-fixed6-csv", "version": 1}
SCALE = Decimal("1000000")
MAX_VALUE = Decimal("1000000000000")
TOP_FIELDS = frozenset(
    ("schema", "input_profile", "condition", "input_rows", "input_sum_scaled",
     "count", "sum_scaled", "mean", "groups")
)
GROUP_FIELDS = frozenset(
    ("group", "input_count", "input_sum_scaled", "min_scaled", "max_scaled",
     "count", "sum_scaled", "mean")
)
SIGNED_INTEGER = re.compile(r"(?:0|-?[1-9][0-9]*)\Z", re.ASCII)
FIXED_MEAN = re.compile(r"-?(?:0|[1-9][0-9]{0,12})\.[0-9]{12}\Z", re.ASCII)
COUNT_ARGUMENT = re.compile(r"[1-9][0-9]{0,6}\Z", re.ASCII)
GROUP_TEXT = re.compile(r"[A-Za-z0-9 _.-]{1,64}\Z", re.ASCII)


class InputInvalid(Exception):
    pass


class ResultInvalid(Exception):
    pass


class ResultMismatch(Exception):
    pass


class InputUnavailable(Exception):
    pass


class _Group:
    __slots__ = ("count", "anchor", "deviations", "low", "high")

    def __init__(self, value: Decimal):
        self.count = 1
        self.anchor = value
        self.deviations = Decimal(0)
        self.low = value
        self.high = value

    def add(self, value: Decimal) -> None:
        self.count += 1
        self.deviations += value - self.anchor
        self.low = min(self.low, value)
        self.high = max(self.high, value)

    def total(self) -> Decimal:
        return self.anchor * self.count + self.deviations


class InputScanner:
    """Bounded lexer; feed never retains rows or a partial input block.

    The caller supplies a Decimal context with precision 64 and Inexact/Rounded
    traps. Group sums have at most 25 integral and six fractional digits;
    anchored deviations and reconstructing sums therefore fit exactly.
    """

    def __init__(self, expected_rows: int):
        if type(expected_rows) is not int or not 1 <= expected_rows <= MAX_ROWS:
            raise InputInvalid
        self.expected_rows = expected_rows
        self.state = "header"
        self.header_offset = 0
        self.total_bytes = 0
        self.rows = 0
        self.record_bytes = 0
        self.group = bytearray()
        self.number = bytearray()
        self.number_state = "start"
        self.integral_digits = 0
        self.fractional_digits = 0
        self.groups: dict[str, _Group] = {}
        # Payload-free instrumentation for logical-boundary controls.
        self.max_chunk_bytes = 0
        self.max_record_bytes = 0
        self.max_group_bytes = 0
        self.max_numeric_bytes = 0

    def _number_byte(self, byte: int) -> None:
        if len(self.number) >= 21:
            raise InputInvalid
        digit = 48 <= byte <= 57
        state = self.number_state
        if state == "start" and byte in (43, 45):
            self.number_state = "sign"
        elif state in ("start", "sign") and digit:
            self.integral_digits = 1
            self.number_state = "zero" if byte == 48 else "integer"
        elif state == "integer" and digit:
            self.integral_digits += 1
            if self.integral_digits > 13:
                raise InputInvalid
        elif state in ("zero", "integer") and byte == 46:
            self.number_state = "point"
        elif state in ("point", "fraction") and digit:
            self.fractional_digits += 1
            if self.fractional_digits > 6:
                raise InputInvalid
            self.number_state = "fraction"
        else:
            raise InputInvalid
        self.number.append(byte)
        self.max_numeric_bytes = max(self.max_numeric_bytes, len(self.number))

    def _finish_row(self) -> None:
        if self.number_state not in ("zero", "integer", "fraction"):
            raise InputInvalid
        if self.rows >= self.expected_rows or self.rows >= MAX_ROWS:
            raise InputInvalid
        value = Decimal(self.number.decode("ascii"))
        if not value.is_finite() or abs(value) > MAX_VALUE:
            raise InputInvalid
        label = self.group.decode("ascii")
        if label not in self.groups:
            if len(self.groups) >= MAX_GROUPS:
                raise InputInvalid
            self.groups[label] = _Group(value)
        else:
            self.groups[label].add(value)
        self.rows += 1
        self.state = "group"
        self.record_bytes = 0
        self.group.clear()
        self.number.clear()
        self.number_state = "start"
        self.integral_digits = self.fractional_digits = 0

    def feed(self, block: bytes) -> None:
        if type(block) is not bytes or len(block) > CHUNK_BYTES:
            raise InputInvalid
        if self.total_bytes + len(block) > MAX_INPUT_BYTES:
            raise InputInvalid
        self.total_bytes += len(block)
        self.max_chunk_bytes = max(self.max_chunk_bytes, len(block))
        for byte in block:
            if self.state == "header":
                header = b"group,value"
                if self.header_offset < len(header):
                    if byte != header[self.header_offset]:
                        raise InputInvalid
                    self.header_offset += 1
                elif byte == 10:
                    self.state = "group"
                elif byte == 13:
                    self.state = "header_lf"
                else:
                    raise InputInvalid
                continue
            if self.state == "header_lf":
                if byte != 10:
                    raise InputInvalid
                self.state = "group"
                continue
            self.record_bytes += 1
            if self.record_bytes > 88:
                raise InputInvalid
            self.max_record_bytes = max(self.max_record_bytes, self.record_bytes)
            if self.state == "group":
                if byte == 44:
                    if not self.group:
                        raise InputInvalid
                    self.state = "number"
                elif (65 <= byte <= 90 or 97 <= byte <= 122
                      or 48 <= byte <= 57 or byte in (32, 95, 46, 45)):
                    if len(self.group) >= 64:
                        raise InputInvalid
                    self.group.append(byte)
                    self.max_group_bytes = max(self.max_group_bytes, len(self.group))
                else:
                    raise InputInvalid
            elif self.state == "number":
                if byte == 10:
                    self._finish_row()
                elif byte == 13:
                    self.state = "record_lf"
                else:
                    self._number_byte(byte)
            elif self.state == "record_lf":
                if byte != 10:
                    raise InputInvalid
                self._finish_row()
            else:
                raise InputInvalid

    def finish(self) -> None:
        if (self.state != "group" or self.group or self.record_bytes
                or self.rows != self.expected_rows):
            raise InputInvalid


def _read(stream, size: int) -> bytes:
    try:
        block = stream.read(size)
    except OSError as exc:
        raise InputUnavailable from exc
    if type(block) is not bytes or len(block) > size:
        raise InputUnavailable
    return block


def _scan_input(stream, expected_rows: int) -> InputScanner:
    scanner = InputScanner(expected_rows)
    while True:
        block = _read(stream, CHUNK_BYTES)
        if not block:
            break
        scanner.feed(block)
    scanner.finish()
    return scanner


def _json_preflight(raw: bytes) -> None:
    """Bound token costs before json.loads allocates result objects."""
    depth: list[int] = []
    tokens = containers = 0
    cursor = 0
    while cursor < len(raw):
        byte = raw[cursor]
        if byte in (9, 10, 13, 32):
            cursor += 1
            continue
        if byte in (123, 91):
            containers += 1
            depth.append(byte)
            if len(depth) > 4 or containers > 131:
                raise ResultInvalid
            cursor += 1
            continue
        if byte in (125, 93):
            if not depth or depth.pop() != (123 if byte == 125 else 91):
                raise ResultInvalid
            cursor += 1
            continue
        if byte in (44, 58):
            cursor += 1
            continue
        tokens += 1
        if tokens > 4096:
            raise ResultInvalid
        if byte == 34:
            cursor += 1
            size = 0
            while True:
                if cursor >= len(raw):
                    raise ResultInvalid
                byte = raw[cursor]
                cursor += 1
                if byte == 34:
                    break
                if byte == 92:
                    if cursor >= len(raw):
                        raise ResultInvalid
                    escape = raw[cursor]
                    cursor += 1
                    if escape == 117:
                        digits = raw[cursor:cursor + 4]
                        if len(digits) != 4 or any(c not in b"0123456789abcdefABCDEF" for c in digits):
                            raise ResultInvalid
                        if int(digits, 16) > 127:
                            raise ResultInvalid
                        cursor += 4
                    elif escape not in b'"\\/bfnrt':
                        raise ResultInvalid
                elif not 32 <= byte <= 126:
                    raise ResultInvalid
                size += 1
                if size > 80:
                    raise ResultInvalid
        elif 48 <= byte <= 57:
            start = cursor
            while cursor < len(raw) and 48 <= raw[cursor] <= 57:
                cursor += 1
                if cursor - start > 7:
                    raise ResultInvalid
            if cursor - start > 1 and raw[start] == 48:
                raise ResultInvalid
        else:
            for literal in (b"true", b"false", b"null"):
                if raw.startswith(literal, cursor):
                    cursor += len(literal)
                    break
            else:
                raise ResultInvalid
    if depth:
        raise ResultInvalid


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ResultInvalid
        out[key] = value
    return out


def _count(value) -> int:
    if type(value) is not int or not 1 <= value <= MAX_ROWS:
        raise ResultInvalid
    return value


def _scaled(value, bound: int, width: int) -> int:
    if (type(value) is not str or len(value) > width
            or SIGNED_INTEGER.fullmatch(value) is None):
        raise ResultInvalid
    number = int(value)
    if abs(number) > bound:
        raise ResultInvalid
    return number


def _mean_ticks(value) -> int:
    if (type(value) is not str or len(value) > 27
            or FIXED_MEAN.fullmatch(value) is None
            or value == "-0.000000000000"):
        raise ResultInvalid
    ticks = int(value.replace(".", ""))
    if abs(ticks) > 10**24:
        raise ResultInvalid
    return ticks


def _read_result(stream) -> dict:
    raw = bytearray()
    while len(raw) <= MAX_RESULT_BYTES:
        block = _read(stream, min(4096, MAX_RESULT_BYTES + 1 - len(raw)))
        if not block:
            break
        raw.extend(block)
    if not raw or len(raw) > MAX_RESULT_BYTES:
        raise ResultInvalid
    data = bytes(raw)
    _json_preflight(data)
    try:
        result = json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ResultInvalid from exc
    if type(result) is not dict or result.keys() != TOP_FIELDS:
        raise ResultInvalid
    if result["schema"] != RESULT_SCHEMA or result["condition"] not in ("baseline", "alternative"):
        raise ResultInvalid
    profile = result["input_profile"]
    if (type(profile) is not dict or profile.keys() != INPUT_PROFILE.keys()
            or profile["id"] != INPUT_PROFILE["id"]
            or type(profile["version"]) is not int or profile["version"] != 1):
        raise ResultInvalid
    _count(result["input_rows"])
    _count(result["count"])
    _scaled(result["input_sum_scaled"], 10**24, 26)
    _scaled(result["sum_scaled"], 10**24, 26)
    _mean_ticks(result["mean"])
    groups = result["groups"]
    if type(groups) is not list or not 1 <= len(groups) <= MAX_GROUPS:
        raise ResultInvalid
    previous = None
    for group in groups:
        if type(group) is not dict or group.keys() != GROUP_FIELDS:
            raise ResultInvalid
        label = group["group"]
        if (type(label) is not str or GROUP_TEXT.fullmatch(label) is None
                or previous is not None and label <= previous):
            raise ResultInvalid
        previous = label
        _count(group["input_count"])
        _count(group["count"])
        _scaled(group["input_sum_scaled"], 10**24, 26)
        _scaled(group["sum_scaled"], 10**24, 26)
        _scaled(group["min_scaled"], 10**18, 20)
        _scaled(group["max_scaled"], 10**18, 20)
        _mean_ticks(group["mean"])
    return result


def _same(actual, expected) -> None:
    if actual != expected:
        raise ResultMismatch


def _check_mean(text: str, total: Decimal, count: int) -> None:
    # A candidate lies in the closed nearest-tick interval. At its endpoints,
    # only the even candidate is allowed. No recipe rounding/formatting is used.
    ticks = _mean_ticks(text)
    error_in_half_ticks = abs(Fraction(ticks, 10**12) - Fraction(total) / count) * (2 * 10**12)
    if error_in_half_ticks > 1 or error_in_half_ticks == 1 and ticks % 2:
        raise ResultMismatch


def _compare(scanner: InputScanner, result: dict, condition: str) -> None:
    _same(result["condition"], condition)
    _same(result["input_rows"], scanner.rows)
    labels = sorted(scanner.groups)  # At most 128 ASCII labels, never values.
    _same([g["group"] for g in result["groups"]], labels)
    original_total = used_total = Decimal(0)
    used_count = 0
    for label, actual in zip(labels, result["groups"]):
        reference = scanner.groups[label]
        original = reference.total()
        count, total = reference.count, original
        if condition == "alternative" and count >= 5:
            count -= 2
            total -= reference.low + reference.high
        _same(actual["input_count"], reference.count)
        _same(int(actual["input_sum_scaled"]), int(original * SCALE))
        _same(int(actual["min_scaled"]), int(reference.low * SCALE))
        _same(int(actual["max_scaled"]), int(reference.high * SCALE))
        _same(actual["count"], count)
        _same(int(actual["sum_scaled"]), int(total * SCALE))
        _check_mean(actual["mean"], total, count)
        original_total += original
        used_total += total
        used_count += count
    _same(int(result["input_sum_scaled"]), int(original_total * SCALE))
    _same(result["count"], used_count)
    _same(int(result["sum_scaled"]), int(used_total * SCALE))
    _check_mean(result["mean"], used_total, used_count)


def _report(status: str, reason: str) -> dict:
    return {"schema": CHECK_SCHEMA, "status": status, "reason": reason}


def check_streams(input_stream, result_stream, condition: str, expected_rows: int) -> dict:
    """Return only closed quality metadata; input/result details are discarded."""
    try:
        if condition not in ("baseline", "alternative"):
            raise InputInvalid
        with localcontext() as context:
            context.prec = 64
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            scanner = _scan_input(input_stream, expected_rows)
            result = _read_result(result_stream)
            _compare(scanner, result, condition)
        return _report("passed", "ok")
    except InputInvalid:
        return _report("failed", "input_invalid")
    except ResultInvalid:
        return _report("failed", "result_invalid")
    except ResultMismatch:
        return _report("failed", "result_mismatch")
    except InputUnavailable:
        return _report("unknown", "input_unavailable")
    except (Exception, KeyboardInterrupt):
        return _report("unknown", "internal_failure")


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    report = _report("failed", "input_invalid")
    try:
        if (len(args) == 3 and args[0] != "-"
                and args[1] in ("baseline", "alternative")
                and COUNT_ARGUMENT.fullmatch(args[2]) is not None
                and 1 <= int(args[2]) <= MAX_ROWS):
            try:
                fd = os.open(args[0], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except OSError:
                report = _report("unknown", "input_unavailable")
            else:
                # The standalone path variant accepts regular files only.
                # Streaming pipes are supplied directly to check_streams.
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        report = _report("unknown", "input_unavailable")
                    elif info.st_size > MAX_INPUT_BYTES:
                        report = _report("failed", "input_invalid")
                    else:
                        report = check_streams(source, sys.stdin.buffer, args[1], int(args[2]))
    except (Exception, KeyboardInterrupt):
        report = _report("unknown", "internal_failure")
    payload = (json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(1, payload[offset:])
            if written <= 0:
                return 2
            offset += written
    except (OSError, KeyboardInterrupt):
        return 2
    return 0 if report["status"] == "passed" else 1 if report["status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
