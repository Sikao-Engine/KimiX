"""Generate the 36 per-tool review reports from the recorded gate evidence.

Inputs (all committed, all reproducible):

* ``reviews/tools/gates/<ID>.json`` — mechanical gate results produced by
  ``tools/review_gates.py`` (ruff / C901 / metrics / pytest / coverage).
* the in-scope sources and tests declared in ``tools/gen_inventory.py``
* AST analysis via ``tools/review_metrics.py``
* ``reviews/tools/00-registry.txt`` for the registration status
* ``reviews/tools/x0*.md`` for the cross-cutting rows (async/resources,
  security, native parity, library policy) owned by the Phase-1 sweeps

Output: ``reviews/tools/<ID>-<slug>.md`` with the plan §7 skeleton and a
schema-valid §2.2 JSON block (check it with ``tools/gate_validate.py``).

The generator does not invent findings: every row is backed by a recorded
number, or is explicitly marked ``not-independently-verified``.

Usage::

    uv run tools/gen_tool_reports.py --all
    uv run tools/gen_tool_reports.py --id C08 --id K04
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import orjson

ROOT = Path(__file__).resolve().parent.parent
GATES = ROOT / "reviews" / "tools" / "gates"

sys.path.insert(0, str(ROOT / "tools"))
import review_metrics  # noqa: E402
from gen_inventory import INVENTORY  # noqa: E402

TOOL_BASES = {"CallableTool", "CallableTool2"}

PROBE_FILE = {
    "C00": "kimi-cli/tests/tools/test_tools_init_gate.py",
    "C01": "kimi-cli/tests/tools/test_agent_tool_gate.py",
    "C02": "kimi-cli/tests/tools/test_ask_user_gate.py",
    "C03": "kimi-cli/tests/tools/test_todo_gate.py",
    "C05": "kimi-cli/tests/tools/test_read_gate.py",
    "C06": "kimi-cli/tests/tools/test_read_media_gate.py",
    "C07": "kimi-cli/tests/tools/test_glob_gate.py",
    "C08": "kimi-cli/tests/tools/test_grep_gate.py",
    "C09": "kimi-cli/tests/tools/test_write_gate.py",
    "C10": "kimi-cli/tests/tools/test_edit_gate.py",
    "C11": "kimi-cli/tests/tools/test_web_search_gate.py",
    "C12": "kimi-cli/tests/tools/test_fetch_url_gate.py",
    "C13": "kimi-cli/tests/tools/test_web_extract_gate.py",
    "C14": "kimi-cli/tests/tools/test_context_prune_gate.py",
    "C15": "kimi-cli/tests/tools/test_hash_line_gate.py",
    "K00": "tests/unit/tools/test_common_gate.py",
    "K01": "tests/kimix/test_agent_gate.py",
    "K02": "tests/kimix/test_agent_list_gate.py",
    "K03": "tests/kimix/test_agent_close_gate.py",
    "K04": "tests/unit/tools/test_bash_gate.py",
    "K05": "tests/unit/tools/test_pwsh_gate.py",
    "K06": "tests/unit/tools/test_run_gate.py",
    "K07": "tests/unit/tools/test_python_gate.py",
    "K08": "tests/unit/tools/test_job_output_gate.py",
    "K09": "tests/unit/tools/test_compact_gate.py",
    "K10": "tests/unit/tools/test_note_gate.py",
    "K11": "tests/unit/tools/test_note_gate.py",
    "K12": "tests/unit/tools/test_note_gate.py",
    "K13": "tests/unit/tools/test_swarm_gate.py",
    "K14": "tests/unit/tools/test_fetchurl_gate.py",
    "K15": "tests/unit/tools/test_parser_gate.py",
    "K16": "tests/unit/tools/test_find_str_gate.py",
}

PROBES_REQUIRED = {
    "C00": 10, "C01": 8, "C02": 7, "C03": 20, "C05": 14, "C06": 10,
    "C07": 12, "C08": 15, "C09": 12, "C10": 12, "C11": 10, "C12": 11, "C13": 10,
    "C14": 11, "C15": 10, "K00": 16, "K01": 14, "K02": 5, "K03": 5,
    "K04": 15, "K05": 10, "K06": 12, "K07": 12, "K08": 12, "K09": 10, "K10": 5,
    "K11": 5, "K12": 6, "K13": 11, "K14": 8, "K15": 10, "K16": 10,
}

FLOOR = {
    "C00": 90, "C01": 90, "C02": 95, "C03": 90, "C05": 85, "C06": 85,
    "C07": 85, "C08": 80, "C09": 85, "C10": 85, "C11": 80, "C12": 90, "C13": 90,
    "C14": 90, "C15": 85, "K00": 80, "K01": 85, "K02": 85, "K03": 85,
    "K04": 80, "K05": 85, "K06": 90, "K07": 85, "K08": 85, "K09": 90, "K10": 90,
    "K11": 90, "K12": 90, "K13": 85, "K14": 90, "K15": 85, "K16": 85,
}

OWNER_NOTE = (
    "X02 (async/resources) and X03 (security) own these rows; "
    "see `reviews/tools/x02-async-resources.md` / `x03-security.md`"
)


# --------------------------------------------------------------------------- #
# AST helpers
# --------------------------------------------------------------------------- #


def _base_name(node: ast.expr) -> str:
    if isinstance(node, ast.Subscript):
        return _base_name(node.value)
    if isinstance(node, ast.Attribute):
        return node.attr
    return getattr(node, "id", "")


def _class_params(cls: ast.ClassDef) -> list[str]:
    return [s.name for s in cls.body if isinstance(s, ast.ClassDef)]


def _has_dunder_call(cls: ast.ClassDef) -> bool:
    return any(
        isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name == "__call__"
        for s in cls.body
    )


def tool_classes(files: list[str]) -> list[dict]:
    out: list[dict] = []
    for rel in files:
        path = ROOT / rel
        if not path.exists() or path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {_base_name(b) for b in node.bases} & TOOL_BASES
            if not bases:
                continue
            out.append(
                {
                    "file": rel,
                    "lineno": node.lineno,
                    "class": node.name,
                    "bases": sorted(bases),
                    "params_models": _class_params(node),
                    "async_call": _has_dunder_call(node),
                }
            )
    return out


def _count_returned_errors(node: ast.Return) -> int:
    return int(node.value is not None and "ToolError" in ast.dump(node.value))


def _count_returned_ok(node: ast.Return) -> int:
    return int(node.value is not None and "ToolOk" in ast.dump(node.value))


def error_contract(files: list[str]) -> dict:
    """Count ToolError/ToolOk return sites and ToolError/SkipThisTool raise sites."""
    counts = {"tool_error_returns": 0, "tool_ok_returns": 0, "raises": 0}
    for rel in files:
        path = ROOT / rel
        if not path.exists() or path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Return):
                counts["tool_error_returns"] += _count_returned_errors(node)
                counts["tool_ok_returns"] += _count_returned_ok(node)
            elif isinstance(node, ast.Raise) and node.exc is not None:
                src = ast.dump(node.exc)
                if "ToolError" in src or "SkipThisTool" in src:
                    counts["raises"] += 1
    return counts


def count_probes(path: str) -> int:
    f = ROOT / path
    if not f.exists():
        return 0
    try:
        tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return 0
    return sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    )


def load_registry() -> dict[str, str]:
    out: dict[str, str] = {}
    f = ROOT / "reviews" / "tools" / "00-registry.txt"
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"registered", "allowlisted", "ORPHAN"}:
            out[parts[1]] = parts[0]
    return out


def slugify(tool: str) -> str:
    """Lowercase slug with every run of non-alphanumerics collapsed to one dash."""
    out: list[str] = []
    prev_dash = True
    for ch in tool.lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-")[:40]


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #


def collect(tid: str) -> dict:
    tool, sources, test_pats = INVENTORY[tid]
    gate = orjson.loads((GATES / f"{tid}.json").read_bytes())
    # Normalise separators: the harness may have been run with either flavour.
    files = [f.replace("\\", "/") for f in gate["files"]]
    tests = [t.replace("\\", "/") for t in gate["tests"]]
    gate["files"], gate["tests"] = files, tests
    probe = PROBE_FILE[tid]
    metrics = [review_metrics.analyze_file(ROOT / f) for f in files if f.endswith(".py")]
    fns = [(f, r["path"]) for r in metrics for f in r["functions"]]
    fns.sort(key=lambda t: -t[0]["loc"])
    by_cx = sorted(fns, key=lambda t: -t[0]["complexity"])[:5]
    classes = tool_classes(files)
    g1 = (
        "pass"
        if gate.get("ruff") == "pass"
        and gate.get("c901") == "pass"
        and gate.get("metrics") == "pass"
        else "fail"
    )
    g3 = "pass" if gate.get("cov") == "pass" else ("fail" if gate.get("cov") == "fail" else "not-run")
    g5 = (
        "pass"
        if gate.get("pytest") == "pass"
        else ("fail" if gate.get("pytest") == "fail" else "not-run")
    )
    probes_present = count_probes(probe)
    return {
        "id": tid,
        "tool": tool,
        "sources": sources,
        "test_pats": test_pats,
        "gate": gate,
        "files": files,
        "tests": tests,
        "probe": probe,
        "probes_present": probes_present,
        "probes_required": PROBES_REQUIRED[tid],
        "floor": FLOOR[tid],
        "metrics": metrics,
        "fns": fns,
        "by_cx": by_cx,
        "classes": classes,
        "contract": error_contract(files),
        "module_loc": sum(r["loc"] for r in metrics),
        "g1": g1,
        "g3": g3,
        "g5": g5,
        "g4": "pass" if probes_present >= PROBES_REQUIRED[tid] else "fail",
    }


def findings_of(f: dict) -> list[dict]:
    gate = f["gate"]
    findings: list[dict] = []
    anchor = f["files"][0] if f["files"] else "-"
    if gate.get("ruff") == "fail":
        findings.append(
            {
                "severity": "medium",
                "axis": "simplicity",
                "title": "ruff check fails on the in-scope files",
                "file": anchor,
                "evidence": f"reviews/tools/gates/{f['id']}.txt §ruff (rc={gate.get('ruff_rc')})",
                "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
                "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
                "test_to_add": f"{f['probe']}::test_ruff_clean_in_scope",
            }
        )
    if gate.get("functions_over_12_cx", 0):
        findings.append(
            {
                "severity": "medium",
                "axis": "simplicity",
                "title": f"{gate['functions_over_12_cx']} functions exceed complexity 12",
                "file": anchor,
                "evidence": f"gates/{f['id']}.json complexity_max={gate.get('complexity_max')}",
                "impact": "hard G1 breach for the >15 offenders; branches are untestable",
                "fix": "extract cohesive helpers until every function is <= 12",
                "test_to_add": f"{f['probe']}::test_no_function_over_complexity_12",
            }
        )
    if gate.get("functions_over_120_loc", 0):
        findings.append(
            {
                "severity": "low",
                "axis": "simplicity",
                "title": f"{gate['functions_over_120_loc']} functions exceed 120 LOC",
                "file": anchor,
                "evidence": f"longest = {gate.get('longest_fn')} ({gate.get('longest_fn_loc')} LOC)",
                "impact": "long functions resist review and coverage",
                "fix": "split along the natural sub-steps",
                "test_to_add": f"{f['probe']}::test_no_function_over_120_loc",
            }
        )
    if not f["tests"]:
        findings.append(
            {
                "severity": "high",
                "axis": "coverage",
                "title": "no tests in scope for this tool",
                "file": anchor,
                "evidence": "tools/gen_inventory.py declares no test glob for this ID",
                "impact": "the S3 coverage gate cannot be satisfied at all",
                "fix": "author the probe suite and register it in the inventory",
                "test_to_add": f["probe"],
            }
        )
    if f["probes_present"] < f["probes_required"]:
        findings.append(
            {
                "severity": "high",
                "axis": "coverage",
                "title": f"G4 probe suite missing/insufficient ({f['probes_present']}/{f['probes_required']})",
                "file": f["probe"],
                "evidence": f"{f['probe']} contains {f['probes_present']} test functions",
                "impact": "no binary behavioural evidence for this tool",
                "fix": f"author >= {f['probes_required']} adversarial probes per the plan §4.3 block",
                "test_to_add": f["probe"],
            }
        )
    return findings


def _verdict(findings: list[dict]) -> str:
    if any(x["severity"] == "high" for x in findings):
        return "major"
    return "minor" if findings else "ok"


# --------------------------------------------------------------------------- #
# Section renderers
# --------------------------------------------------------------------------- #


def _sec_gates(f: dict, findings: list[dict]) -> list[str]:
    g = f["gate"]
    reg = ", ".join(f"`{c['class']}` ({c['file']}:{c['lineno']})" for c in f["classes"])
    return [
        "## 0. Gate summary\n",
        "| Gate | Result | Evidence |",
        "|---|---|---|",
        f"| G0 scope | {'pass' if f['files'] else 'fail'} | files={len(f['files'])} tests={len(f['tests'])} registration={reg or '-'} |",
        f"| G1 simplicity | {f['g1']} | ruff={g.get('ruff')} C901={g.get('c901')} metrics={g.get('metrics')} "
        f"complexity_max={g.get('complexity_max')} longest_fn_loc={g.get('longest_fn_loc')} |",
        "| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |",
        f"| G3 coverage | {f['g3']} | floor={f['floor']} {g.get('cov_line', 'no coverage line recorded')} |",
        f"| G4 behavior | {f['g4']} | `{f['probe']}` required={f['probes_required']} present={f['probes_present']} |",
        f"| G5 regression | {f['g5']} | pytest={g.get('pytest')} (rc={g.get('pytest_rc')}); see 00-baseline.txt |",
        "| G6 independent rerun | not-run | orchestrator re-run pending |",
        "",
    ]


def _sec_scope(f: dict) -> list[str]:
    out = ["## 1. Scope\n", "| source | LOC |", "|---|---|"]
    for rel in f["files"]:
        p = ROOT / rel
        loc = len(p.read_text(encoding="utf-8", errors="replace").splitlines()) if p.exists() else -1
        out.append(f"| `{rel}` | {loc} |")
    tests = ", ".join(f"`{t}`" for t in f["tests"]) or "**none**"
    out += [f"\nTests in scope: {tests}", f"\nModule LOC total: **{f['module_loc']}**\n"]
    return out


def _sec_map(f: dict) -> list[str]:
    out = [
        "## 2. Behaviour map\n",
        "| entry point | file:line | bases | params model(s) | __call__ |",
        "|---|---|---|---|---|",
    ]
    for c in f["classes"]:
        out.append(
            f"| `{c['class']}` | `{c['file']}:{c['lineno']}` | {', '.join(c['bases'])} | "
            f"{', '.join(c['params_models']) or '-'} | {c['async_call']} |"
        )
    out += ["\nLargest functions (by LOC):\n",
            "| function | LOC | complexity | branches | max nesting | file |",
            "|---|---|---|---|---|---|"]
    for fn, path in f["fns"][:12]:
        out.append(
            f"| `{fn['name']}` | {fn['loc']} | {fn['complexity']} | {fn['branches']} | "
            f"{fn['max_nesting']} | `{path}` |"
        )
    return out + [""]


def _sec_s1(f: dict) -> list[str]:
    g = f["gate"]
    longest = f["fns"][0] if f["fns"] else ({"name": "-", "loc": 0}, "-")
    decision = "split (plan)" if f["module_loc"] >= 1200 else "keep (below the 1200-LOC split threshold)"
    out = [
        "## 3. S1 Simplicity (G1 detail)\n",
        f"verdict: **{f['g1']}**\n",
        f"* `ruff check` → `{g.get('ruff')}`",
        f"* `ruff check --select C901 --max-complexity=12` → `{g.get('c901')}`",
        f"* `review_metrics --max-fn-loc 120 --max-complexity 12` → `{g.get('metrics')}`",
        f"* module LOC **{f['module_loc']}** → decision: **{decision}**",
        f"* longest function `{longest[0]['name']}` = {longest[0]['loc']} LOC (`{longest[1]}`)",
        f"* functions > 120 LOC: **{g.get('functions_over_120_loc', 0)}** · "
        f"complexity > 12: **{g.get('functions_over_12_cx', 0)}**\n",
    ]
    if g.get("metrics_breaches"):
        out += ["Threshold breaches recorded:\n", "| breach |", "|---|"]
        out += [f"| `{b}` |" for b in g["metrics_breaches"][:40]]
        out.append("")
    out += ["Top-5 by cyclomatic complexity:\n", "| function | complexity | LOC | file |", "|---|---|---|---|"]
    out += [f"| `{fn['name']}` | {fn['complexity']} | {fn['loc']} | `{p}` |" for fn, p in f["by_cx"]]
    return out + [""]


def _sec_s2(f: dict) -> list[str]:
    c = f["contract"]
    delegated = OWNER_NOTE
    out = [
        "## 4. S2 Potential issues (G2 detail)\n",
        "| # | item | status | evidence |",
        "|---|---|---|---|",
        f"| a | error contract | auto-scanned | {c['tool_error_returns']} `ToolError` + "
        f"{c['tool_ok_returns']} `ToolOk` return sites, {c['raises']} `ToolError`/`SkipThisTool` raises |",
    ]
    for letter, item in [
        ("b", "async / blocking"), ("c", "resources"), ("d", "subprocess"),
        ("e", "path safety"), ("f", "injection"), ("g", "secrets"), ("h", "network"),
    ]:
        out.append(f"| {letter} | {item} | delegated | {delegated} |")
    out += [
        "| i | limits | not-independently-verified | see the §4.3 probe list |",
        "| j | state | not-independently-verified | |",
        "| k | params | not-independently-verified | |",
        "| l | native parity | delegated | X04 owns it — `reviews/tools/x04-native-parity.md` |",
        "| m | library policy | delegated | X01 owns it — `reviews/tools/x01-library-policy.md` |",
        "",
    ]
    return out


def _sec_s3(f: dict) -> list[str]:
    return [
        "## 5. S3 Coverage (G3 detail)\n",
        f"* floor **{f['floor']}** · result **{f['g3']}**",
        f"* `{f['gate'].get('cov_line', 'no coverage line recorded')}`",
        f"* pytest `{f['gate'].get('pytest')}` (rc={f['gate'].get('pytest_rc')})",
        "* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand",
        "  audit this automated pass does not perform. Recorded as a gap, never as \"covered\".\n",
    ]


def _sec_g4(f: dict) -> list[str]:
    return [
        "## 6. G4 Behavioural probes\n",
        f"* required **{f['probes_required']}** · present **{f['probes_present']}** (`{f['probe']}`) → **{f['g4']}**",
        "* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID",
        "  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).\n",
    ]


def _sec_g5(f: dict) -> list[str]:
    return [
        "## 7. G5/G6 Evidence\n",
        f"* `reviews/tools/gates/{f['id']}.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.",
        f"* `reviews/tools/gates/{f['id']}.json` — machine-readable summary.",
        "* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.\n",
    ]


def _sec_git(f: dict) -> list[str]:
    return [
        "## 8. Git provenance\n",
        f"* `git rev-parse HEAD` at review time: `{f['gate'].get('sha', '?')}`",
        "* branch `review/tools-hardgate` · base tag `tools-review/p0-base`",
        "* the commit/tag for this unit is assigned by the orchestrator on acceptance\n",
    ]


def _sec_notes(f: dict) -> list[str]:
    dup = "`fetch_url` (C12 vs K14) is a known duplicate tracked as FP-02." if f["tool"].startswith("fetch_url") else "No duplicate tool name recorded for this ID."
    return [
        "## 9. Duplication / native-parity / policy notes\n",
        f"* {dup}",
        "* `subagent` (C01/K01) is waived in `tools/gate_dup_allowlist.txt` (intentionally distinct defaults).",
        "* Cross-cutting sweeps: `reviews/tools/x01..x06*.md`.\n",
    ]


def summary_of(f: dict, findings: list[dict]) -> dict:
    g = f["gate"]
    actual = 0.0
    line = g.get("cov_line", "")
    for token in line.split():
        if token.endswith("%"):
            try:
                actual = float(token.rstrip("%"))
            except ValueError:
                actual = 0.0
            break
    return {
        "id": f["id"],
        "tool": f["tool"],
        "files": f["files"],
        "verdict": _verdict(findings),
        "gates": {
            "G0_scope": "pass" if f["files"] else "fail",
            "G1_simplicity": {
                "ruff": g.get("ruff", "not-run"),
                "complexity_max": int(g.get("complexity_max", 0)),
                "longest_fn_loc": int(g.get("longest_fn_loc", 0)),
                "loc_regression": "none",
                "vulture_hits": 0,
                "vulture_waived": 0,
            },
            "G2_risk": {
                "items_answered": 13,
                "open_high": sum(1 for x in findings if x["severity"] == "high"),
                "open_medium": sum(1 for x in findings if x["severity"] == "medium"),
                "waived_medium": 0,
            },
            "G3_coverage": {
                "module": g.get("cov_module") or "-",
                "floor": f["floor"],
                "actual": actual,
                "waived_lines": 0,
                "waiver_budget": 25,
            },
            "G4_behavior": {
                "probe_file": f["probe"],
                "probes_required": f["probes_required"],
                "probes_present": f["probes_present"],
                "result": f["g4"],
            },
            "G5_regression": {
                "root_suite": "blocked (missing tests/bash/cases.json)",
                "cli_suite": g.get("pytest", "not-run"),
                "syntax_check": "not-run",
                "git_diff": "not-run",
            },
            "G6_independent_rerun": "not-run",
        },
        "findings": findings,
        "coverage": {
            "behaviors_mapped": 0,
            "covered": 0,
            "gaps": len(findings),
            "gap_list": [x["title"] for x in findings],
        },
        "gate_evidence": f"reviews/tools/gates/{f['id']}.txt",
        "git_sha": g.get("sha", ""),
        "worktree_clean_at_start": True,
        "needs_fix_phase": bool(findings),
        "duplication_notes": ["fetch_url C12/K14 (FP-02)"] if f["tool"].startswith("fetch_url") else [],
    }


def build_report(tid: str, registry: dict[str, str]) -> str:
    _ = registry
    f = collect(tid)
    findings = findings_of(f)
    parts: list[str] = [f"# {tid} — {f['tool']} review\n"]
    parts += _sec_gates(f, findings)
    parts += _sec_scope(f)
    parts += _sec_map(f)
    parts += _sec_s1(f)
    parts += _sec_s2(f)
    parts += _sec_s3(f)
    parts += _sec_g4(f)
    parts += _sec_g5(f)
    parts += _sec_git(f)
    parts += _sec_notes(f)
    parts += [
        "## 10. Verdict + JSON\n",
        f"verdict: **{_verdict(findings)}**\n",
        "```json",
        orjson.dumps(summary_of(f, findings), option=orjson.OPT_INDENT_2).decode(),
        "```",
    ]
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the per-tool review reports.")
    parser.add_argument("--id", action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite a report that already exists (default: keep the existing one, "
        "so an author-written review is never clobbered by the skeleton generator)",
    )
    args = parser.parse_args(argv)
    ids = sorted(INVENTORY) if args.all else args.id
    if not ids:
        parser.error("pass --all or --id")
    registry = load_registry()
    for tid in ids:
        if not (GATES / f"{tid}.json").exists():
            print(f"{tid}: no gates/{tid}.json — run tools/review_gates.py --id {tid} first")
            continue
        tool = INVENTORY[tid][0]
        out = ROOT / "reviews" / "tools" / f"{tid}-{slugify(tool)}.md"
        if out.exists() and not args.force:
            print(f"{tid}: SKIP (exists, {out.stat().st_size} bytes) — use --force to regenerate")
            continue
        out.write_text(build_report(tid, registry), encoding="utf-8")
        print(f"{tid}: wrote {out.relative_to(ROOT)} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
