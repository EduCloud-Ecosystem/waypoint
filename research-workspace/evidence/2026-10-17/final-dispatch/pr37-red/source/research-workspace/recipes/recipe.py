# SPDX-License-Identifier: Apache-2.0
"""Reviewed public-data demonstration; emits descriptive statistics only."""
import csv
import json
import sys

groups = {}
with open(sys.argv[1], newline="", encoding="utf-8") as source:
    for row in csv.DictReader(source):
        groups.setdefault(row["group"], []).append(float(row["value"]))
condition = sys.argv[2]
rows = []
all_values = []
for group, values in sorted(groups.items()):
    values.sort()
    if condition == "alternative" and len(values) >= 5:
        values = values[1:-1]
    rows.append({"group": group, "count": len(values), "mean": sum(values) / len(values)})
    all_values.extend(values)
print(json.dumps({"condition": condition, "count": len(all_values),
                  "mean": sum(all_values) / len(all_values), "groups": rows}, allow_nan=False))
