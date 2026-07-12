#!/usr/bin/env python3
"""Enforce issue #79's bus-specific coverage contract."""

import ast
import json
import sys
from pathlib import Path


BUS_MIN = 70.0
CRITICAL_MIN = 90.0
CRITICAL_FUNCTIONS = [
    "cmd_send",
    "cmd_claim",
    "cmd_pen_checkpoint",
    "cmd_pen_pass",
    "set_status_label",
]


def percent(covered, total):
    return 100.0 if total == 0 else covered / total * 100.0


def function_ranges(bus_path):
    module = ast.parse(bus_path.read_text())
    return {
        node.name: range(node.lineno, node.end_lineno + 1)
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef)
    }


def main(argv=None):
    argv = argv or sys.argv[1:]
    coverage_path = Path(argv[0]) if argv else Path("coverage.json")
    data = json.loads(coverage_path.read_text())
    bus = data["files"]["bus"]
    bus_percent = float(bus["summary"]["percent_covered"])

    failures = []
    if bus_percent < BUS_MIN:
        failures.append(f"bus coverage {bus_percent:.2f}% < {BUS_MIN:.0f}%")

    executed = set(bus["executed_lines"])
    missing = set(bus["missing_lines"])
    ranges = function_ranges(Path("bus"))
    for name in CRITICAL_FUNCTIONS:
        lines = set(ranges[name])
        statements = (executed | missing) & lines
        covered = executed & lines
        function_percent = percent(len(covered), len(statements))
        if function_percent < CRITICAL_MIN:
            failures.append(
                f"{name} coverage {function_percent:.2f}% < {CRITICAL_MIN:.0f}%"
            )

    if failures:
        for failure in failures:
            print(f"coverage gate: {failure}", file=sys.stderr)
        return 1

    print(
        f"coverage gate: bus {bus_percent:.2f}%, "
        f"critical functions >= {CRITICAL_MIN:.0f}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
