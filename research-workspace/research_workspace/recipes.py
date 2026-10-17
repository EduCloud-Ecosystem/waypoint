"""Reviewed recipes and an independent, deterministic result validator."""

import csv
import hashlib
import io
import math
import re
import statistics
from pathlib import Path

RECIPES = Path(__file__).resolve().parent.parent / "recipes"
IDS = {"summary-python": ("python", "recipe.py"), "summary-r": ("r", "recipe.R")}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_csv(content):
    if not isinstance(content, str) or len(content.encode("utf-8")) > 65536:
        raise ValueError("Use a CSV file no larger than 64 KiB.")
    reader = csv.DictReader(io.StringIO(content, newline=""))
    if reader.fieldnames != ["group", "value"]:
        raise ValueError("CSV columns must be exactly group,value.")
    groups = {}
    count = 0
    for row in reader:
        count += 1
        if count > 1000 or set(row) != {"group", "value"}:
            raise ValueError("Use at most 1,000 rows with exactly two columns.")
        name = row["group"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 _.-]{1,64}", name):
            raise ValueError("Group names use 1–64 letters, numbers, spaces, dots, dashes or underscores.")
        try:
            number = float(row["value"])
        except (ValueError, TypeError) as exc:
            raise ValueError("Every value must be a finite number.") from exc
        if not math.isfinite(number) or abs(number) > 1e12:
            raise ValueError("Every value must be finite and between -1e12 and 1e12.")
        groups.setdefault(name, []).append(number)
    if not groups:
        raise ValueError("Add at least one data row.")
    return groups


def expected_result(content, condition):
    if condition not in ("baseline", "alternative"):
        raise ValueError("Choose baseline or alternative.")
    groups = parse_csv(content)
    rows = []
    all_values = []
    for name in sorted(groups):
        values = sorted(groups[name])
        if condition == "alternative" and len(values) >= 5:
            values = values[1:-1]
        rows.append({"group": name, "count": len(values), "mean": statistics.fmean(values)})
        all_values.extend(values)
    return {"condition": condition, "count": len(all_values),
            "mean": statistics.fmean(all_values), "groups": rows}


def validate_result(result, content, condition):
    expected = expected_result(content, condition)
    def match(a, b):
        if isinstance(b, dict):
            return isinstance(a, dict) and set(a) == set(b) and all(match(a[k], v) for k, v in b.items())
        if isinstance(b, list):
            return isinstance(a, list) and len(a) == len(b) and all(match(x, y) for x, y in zip(a, b))
        if isinstance(b, float):
            return type(a) in (int, float) and math.isfinite(a) and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-9)
        return type(a) is type(b) and a == b
    if not match(result, expected):
        raise ValueError("The recipe output did not match the independent result check.")
    return result


def recipe_bytes(identifier):
    if identifier not in IDS:
        raise ValueError("Choose an available reviewed recipe.")
    return (RECIPES / IDS[identifier][1]).read_bytes()
