# SPDX-License-Identifier: Apache-2.0
"""Disabled assessment candidate: exact, bounded, fixed-decimal summaries.

CLI: INPUT CONDITION EXPECTED_ROWS. INPUT "-" reads binary standard input.
This program provides no admission, sampling, storage or cleanup authority.
"""

from contextlib import nullcontext
import json
import os
import re
import sys


CHUNK_BYTES = 65536
MAX_BYTES = 88000013
MAX_ROWS = 1000000
MAX_GROUPS = 128
MAX_RECORD_BYTES = 88
MAX_OUTPUT_BYTES = 61440
SCALE = 1000000
MAX_VALUE = 10**18
MAX_SUM = 10**24
GROUP = re.compile(rb"[A-Za-z0-9 _.-]{1,64}\Z")
VALUE = re.compile(rb"[+-]?(?:0|[1-9][0-9]{0,12})(?:\.[0-9]{1,6})?\Z")
ROWS = re.compile(r"[1-9][0-9]{0,6}\Z")


class InvalidInput(ValueError):
    """A fixed public error category; never includes the rejected content."""


def scaled_value(raw):
    if len(raw) > 21 or not VALUE.fullmatch(raw):
        raise InvalidInput()
    negative = raw.startswith(b"-")
    unsigned = raw[1:] if raw[:1] in (b"-", b"+") else raw
    pieces = unsigned.split(b".")
    whole = int(pieces[0])
    fraction = int(pieces[1].ljust(6, b"0")) if len(pieces) == 2 else 0
    value = whole * SCALE + fraction
    if value > MAX_VALUE:
        raise InvalidInput()
    return -value if negative else value


def mean_text(total, count):
    """Exact rational rounding to twelve decimal places, ties to even."""
    if type(total) is not int or abs(total) > MAX_SUM:
        raise ArithmeticError()
    if type(count) is not int or not 1 <= count <= MAX_ROWS:
        raise ArithmeticError()
    rounded, remainder = divmod(abs(total) * SCALE, count)
    if 2 * remainder > count or (2 * remainder == count and rounded % 2):
        rounded += 1
    digits = str(rounded).rjust(13, "0")
    return ("-" if total < 0 and rounded else "") + digits[:-12] + "." + digits[-12:]


def records(source):
    """Read fixed-size blocks, retaining at most one bounded partial record."""
    tail = bytearray()
    total = 0
    while True:
        block = source.read(min(CHUNK_BYTES, MAX_BYTES - total + 1))
        if not isinstance(block, bytes) or len(block) > CHUNK_BYTES:
            raise InvalidInput()
        if not block:
            if tail:
                raise InvalidInput()
            return
        total += len(block)
        if total > MAX_BYTES:
            raise InvalidInput()
        start = 0
        while start < len(block):
            stop = block.find(b"\n", start)
            end = len(block) if stop < 0 else stop + 1
            if len(tail) + end - start > MAX_RECORD_BYTES:
                raise InvalidInput()
            tail.extend(block[start:end])
            start = end
            if stop >= 0:
                line = bytes(tail)
                tail.clear()
                yield line


def summarize(source, condition, expected_rows):
    if condition not in ("baseline", "alternative"):
        raise InvalidInput()
    if type(expected_rows) is not int or not 1 <= expected_rows <= MAX_ROWS:
        raise InvalidInput()
    lines = records(source)
    if next(lines, None) not in (b"group,value\n", b"group,value\r\n"):
        raise InvalidInput()
    groups = {}
    rows = 0
    original_total = 0
    for line in lines:
        if rows >= MAX_ROWS or rows >= expected_rows:
            raise InvalidInput()
        body = line[:-2] if line.endswith(b"\r\n") else line[:-1]
        if body.count(b",") != 1:
            raise InvalidInput()
        label, raw = body.split(b",")
        if not GROUP.fullmatch(label):
            raise InvalidInput()
        value = scaled_value(raw)
        key = label.decode("ascii")
        if key not in groups:
            if len(groups) >= MAX_GROUPS:
                raise InvalidInput()
            groups[key] = [0, 0, value, value]
        group = groups[key]
        group[0] += 1
        group[1] += value
        group[2] = min(group[2], value)
        group[3] = max(group[3], value)
        rows += 1
        original_total += value
        if abs(group[1]) > MAX_SUM or abs(original_total) > MAX_SUM:
            raise ArithmeticError()
    if rows != expected_rows:
        raise InvalidInput()
    output_groups = []
    used_count = 0
    used_total = 0
    for label in sorted(groups):
        count, total, low, high = groups[label]
        trim = condition == "alternative" and count >= 5
        kept_count = count - 2 if trim else count
        kept_total = total - low - high if trim else total
        output_groups.append({
            "group": label,
            "input_count": count,
            "input_sum_scaled": str(total),
            "min_scaled": str(low),
            "max_scaled": str(high),
            "count": kept_count,
            "sum_scaled": str(kept_total),
            "mean": mean_text(kept_total, kept_count),
        })
        used_count += kept_count
        used_total += kept_total
    return {
        "schema": "educloud.summary-stream-result/v1",
        "input_profile": {"id": "summary-fixed6-csv", "version": 1},
        "condition": condition,
        "input_rows": rows,
        "input_sum_scaled": str(original_total),
        "count": used_count,
        "sum_scaled": str(used_total),
        "mean": mean_text(used_total, used_count),
        "groups": output_groups,
    }


def encode_result(result):
    data = (json.dumps(result, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
    if len(data) > MAX_OUTPUT_BYTES:
        raise ArithmeticError()
    return data


def write_all(descriptor, data):
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError()
        offset += written


def main(arguments):
    try:
        if len(arguments) != 3 or not ROWS.fullmatch(arguments[2]):
            raise InvalidInput()
        expected = int(arguments[2])
        if not 1 <= expected <= MAX_ROWS:
            raise InvalidInput()
        source = nullcontext(sys.stdin.buffer) if arguments[0] == "-" else open(arguments[0], "rb")
        with source as stream:
            result = summarize(stream, arguments[1], expected)
        write_all(1, encode_result(result))
        return 0
    except InvalidInput:
        message, code = b"SUMMARY_INPUT_INVALID\n", 1
    except OSError:
        message, code = b"SUMMARY_INPUT_UNAVAILABLE\n", 2
    except (Exception, KeyboardInterrupt):
        message, code = b"SUMMARY_INTERNAL_FAILURE\n", 2
    try:
        write_all(2, message)
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
