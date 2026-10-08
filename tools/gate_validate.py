"""G0 gate — validate the §2.2 JSON summary block of a per-tool review report.

Every review report ends with a JSON block (plan §2.2).  This validator extracts
the *last* fenced ```json block that looks like a review summary and checks it
against the schema:

* required top-level keys and types;
* ``verdict`` in {ok, minor, major, blocked};
* the per-gate sub-schemas for G1_simplicity / G2_risk / G3_coverage /
  G4_behavior / G5_regression when the gate is reported as an object;
* every ``findings[]`` entry carries the eight documented keys.

Exit code ``0`` when the block is schema-valid, ``1`` otherwise (with a list of
errors on stderr).

Usage::

    uv run tools/gate_validate.py reviews/tools/C08-grep.md
    uv run tools/gate_validate.py reviews/tools/*.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import orjson

VERDICTS = {"ok", "minor", "major", "blocked"}
GATE_KEYS = (
    "G0_scope",
    "G1_simplicity",
    "G2_risk",
    "G3_coverage",
    "G4_behavior",
    "G5_regression",
    "G6_independent_rerun",
)
FINDING_KEYS = (
    "severity",
    "axis",
    "title",
    "file",
    "evidence",
    "impact",
    "fix",
    "test_to_add",
)
SUBTYPES: dict[str, dict[str, type]] = {
    "G1_simplicity": {
        "ruff": str,
        "complexity_max": int,
        "longest_fn_loc": int,
        "loc_regression": str,
        "vulture_hits": int,
        "vulture_waived": int,
    },
    "G2_risk": {
        "items_answered": int,
        "open_high": int,
        "open_medium": int,
        "waived_medium": int,
    },
    "G3_coverage": {
        "module": str,
        "floor": (int, float),
        "actual": (int, float),
        "waived_lines": int,
        "waiver_budget": int,
    },
    "G4_behavior": {
        "probe_file": str,
        "probes_required": int,
        "probes_present": int,
        "result": str,
    },
    "G5_regression": {
        "root_suite": str,
        "cli_suite": str,
        "syntax_check": str,
        "git_diff": str,
    },
}


def extract_json_blocks(text: str) -> list[str]:
    blocks: list[str] = []
    fence = "```"
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith(fence + "json") or stripped == fence:
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(fence):
                body.append(lines[i])
                i += 1
            blocks.append("\n".join(body))
        i += 1
    return blocks


def find_summary(text: str) -> tuple[dict | None, str | None]:
    candidates: list[dict] = []
    for block in extract_json_blocks(text):
        try:
            data = orjson.loads(block)
        except orjson.JSONDecodeError:
            continue
        if isinstance(data, dict) and "id" in data and "gates" in data:
            candidates.append(data)
    if not candidates:
        return None, "no fenced ```json review-summary block found"
    return candidates[-1], None


def _check_type(value: Any, expected: type | tuple, label: str, errors: list[str]) -> None:
    if not isinstance(value, expected) or isinstance(value, bool) and expected is int:
        errors.append(f"{label}: expected {expected}, got {type(value).__name__}")


REQUIRED_TOP_LEVEL = (
    ("id", str),
    ("tool", str),
    ("files", list),
    ("verdict", str),
    ("gates", dict),
    ("findings", list),
    ("coverage", dict),
    ("gate_evidence", str),
    ("git_sha", str),
    ("worktree_clean_at_start", bool),
    ("needs_fix_phase", bool),
    ("duplication_notes", list),
)


def _check_top_level(data: dict, errors: list[str]) -> None:
    for key, expected in REQUIRED_TOP_LEVEL:
        if key not in data:
            errors.append(f"missing required key: {key}")
        else:
            _check_type(data[key], expected, key, errors)
    verdict = data.get("verdict")
    if isinstance(verdict, str) and verdict not in VERDICTS:
        errors.append(f"verdict: must be one of {sorted(VERDICTS)}, got {verdict!r}")


def _check_gates(data: dict, errors: list[str]) -> None:
    gates = data.get("gates")
    if not isinstance(gates, dict):
        errors.append("gates: must be an object")
        return
    for gate_key in GATE_KEYS:
        if gate_key not in gates:
            errors.append(f"gates: missing {gate_key}")
    for gate_key, subschema in SUBTYPES.items():
        value = gates.get(gate_key)
        if value is None or isinstance(value, str):
            continue  # a flat "pass"/"fail"/"n/a" is accepted
        if not isinstance(value, dict):
            errors.append(f"gates.{gate_key}: expected object or string")
            continue
        for sub, expected in subschema.items():
            if sub not in value:
                errors.append(f"gates.{gate_key}: missing {sub}")
            else:
                _check_type(value[sub], expected, f"gates.{gate_key}.{sub}", errors)


def _check_findings(data: dict, errors: list[str]) -> None:
    findings = data.get("findings")
    if not isinstance(findings, list):
        return
    for idx, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"findings[{idx}]: must be an object")
            continue
        for key in FINDING_KEYS:
            if key not in finding:
                errors.append(f"findings[{idx}]: missing {key}")


def _check_coverage(data: dict, errors: list[str]) -> None:
    cov = data.get("coverage")
    if isinstance(cov, dict):
        for key in ("behaviors_mapped", "covered", "gaps", "gap_list"):
            if key not in cov:
                errors.append(f"coverage: missing {key}")


def validate(data: dict) -> list[str]:
    errors: list[str] = []
    _check_top_level(data, errors)
    _check_gates(data, errors)
    _check_findings(data, errors)
    _check_coverage(data, errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a review report's §2.2 JSON block.")
    parser.add_argument("reports", nargs="+", help="review markdown files")
    parser.add_argument("--json", action="store_true", help="emit a JSON verdict")
    args = parser.parse_args(argv)

    overall = 0
    results: list[dict] = []
    for raw in args.reports:
        path = Path(raw)
        if not path.exists():
            results.append({"report": raw, "valid": False, "errors": ["file not found"]})
            overall = 1
            continue
        data, err = find_summary(path.read_text(encoding="utf-8", errors="replace"))
        if data is None:
            results.append({"report": raw, "valid": False, "errors": [err or "unknown"]})
            overall = 1
            continue
        errors = validate(data)
        results.append(
            {"report": raw, "valid": not errors, "id": data.get("id"), "errors": errors}
        )
        if errors:
            overall = 1

    if args.json:
        print(orjson.dumps(results, option=orjson.OPT_INDENT_2).decode())
    else:
        for res in results:
            status = "VALID  " if res["valid"] else "INVALID"
            print(f"{status} {res['report']}  id={res.get('id')}")
            for err in res["errors"]:
                print(f"    - {err}")
    return overall


if __name__ == "__main__":
    sys.exit(main())
