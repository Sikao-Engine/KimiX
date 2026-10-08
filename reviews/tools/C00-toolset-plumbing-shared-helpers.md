# C00 — toolset plumbing & shared helpers review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=4 tests=5 registration=- |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=34 longest_fn_loc=78 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | fail | floor=90 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_tools_init_gate.py` required=10 present=0 |
| G5 regression | fail | pytest=fail (rc=1); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/__init__.py` | 167 |
| `kimi-cli/src/kimi_cli/tools/utils.py` | 299 |
| `kimi-cli/src/kimi_cli/tools/display.py` | 49 |
| `kimi-cli/src/kimi_cli/tools/reason.py` | 104 |

Tests in scope: `kimi-cli/tests/tools/test_extract_key_argument.py`, `kimi-cli/tests/tools/test_utils.py`, `kimi-cli/tests/tools/test_reason.py`, `kimi-cli/tests/core/test_toolset.py`, `kimi-cli/tests/core/test_toolset_todo_redirects.py`

Module LOC total: **619**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `extract_key_argument` | 78 | 34 | 33 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/__init__.py` |
| `write` | 38 | 8 | 7 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |
| `repair_tool_arguments` | 36 | 10 | 9 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |
| `resolve_tool_class` | 33 | 9 | 8 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/__init__.py` |
| `to_markdown` | 23 | 6 | 5 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/reason.py` |
| `repair_json_string` | 20 | 6 | 5 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |
| `ok` | 20 | 6 | 5 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |
| `formatted_print` | 20 | 3 | 2 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/reason.py` |
| `tail` | 19 | 7 | 6 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |
| `error` | 19 | 4 | 3 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |
| `_first_web_extract_url` | 18 | 10 | 9 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/__init__.py` |
| `add_tool_call_reason` | 18 | 3 | 2 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/reason.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **619** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `extract_key_argument` = 78 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/__init__.py`)
* functions > 120 LOC: **0** · complexity > 12: **1**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/__init__.py::extract_key_argument complexity=34 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `extract_key_argument` | 34 | 78 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/__init__.py` |
| `repair_tool_arguments` | 10 | 36 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |
| `_first_web_extract_url` | 10 | 18 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/__init__.py` |
| `resolve_tool_class` | 9 | 33 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/__init__.py` |
| `write` | 8 | 38 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/utils.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 0 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
| b | async / blocking | delegated | X02 (async/resources) and X03 (security) own these rows; see `reviews/tools/x02-async-resources.md` / `x03-security.md` |
| c | resources | delegated | X02 (async/resources) and X03 (security) own these rows; see `reviews/tools/x02-async-resources.md` / `x03-security.md` |
| d | subprocess | delegated | X02 (async/resources) and X03 (security) own these rows; see `reviews/tools/x02-async-resources.md` / `x03-security.md` |
| e | path safety | delegated | X02 (async/resources) and X03 (security) own these rows; see `reviews/tools/x02-async-resources.md` / `x03-security.md` |
| f | injection | delegated | X02 (async/resources) and X03 (security) own these rows; see `reviews/tools/x02-async-resources.md` / `x03-security.md` |
| g | secrets | delegated | X02 (async/resources) and X03 (security) own these rows; see `reviews/tools/x02-async-resources.md` / `x03-security.md` |
| h | network | delegated | X02 (async/resources) and X03 (security) own these rows; see `reviews/tools/x02-async-resources.md` / `x03-security.md` |
| i | limits | not-independently-verified | see the §4.3 probe list |
| j | state | not-independently-verified | |
| k | params | not-independently-verified | |
| l | native parity | delegated | X04 owns it — `reviews/tools/x04-native-parity.md` |
| m | library policy | delegated | X01 owns it — `reviews/tools/x01-library-policy.md` |

## 5. S3 Coverage (G3 detail)

* floor **90** · result **fail**
* `no coverage line recorded`
* pytest `fail` (rc=1)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **10** · present **0** (`kimi-cli/tests/tools/test_tools_init_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C00.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C00.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `d684f5a8cd5cfc39549a6a3566b83d9a454deec7
[elapsed 0.0s, rc=0]`
* branch `review/tools-hardgate` · base tag `tools-review/p0-base`
* the commit/tag for this unit is assigned by the orchestrator on acceptance

## 9. Duplication / native-parity / policy notes

* No duplicate tool name recorded for this ID.
* `subagent` (C01/K01) is waived in `tools/gate_dup_allowlist.txt` (intentionally distinct defaults).
* Cross-cutting sweeps: `reviews/tools/x01..x06*.md`.

## 10. Verdict + JSON

verdict: **major**

```json
{
  "id": "C00",
  "tool": "toolset plumbing & shared helpers",
  "files": [
    "kimi-cli/src/kimi_cli/tools/__init__.py",
    "kimi-cli/src/kimi_cli/tools/utils.py",
    "kimi-cli/src/kimi_cli/tools/display.py",
    "kimi-cli/src/kimi_cli/tools/reason.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 34,
      "longest_fn_loc": 78,
      "loc_regression": "none",
      "vulture_hits": 0,
      "vulture_waived": 0
    },
    "G2_risk": {
      "items_answered": 13,
      "open_high": 1,
      "open_medium": 2,
      "waived_medium": 0
    },
    "G3_coverage": {
      "module": "kimi_cli.tools",
      "floor": 90,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_tools_init_gate.py",
      "probes_required": 10,
      "probes_present": 0,
      "result": "fail"
    },
    "G5_regression": {
      "root_suite": "blocked (missing tests/bash/cases.json)",
      "cli_suite": "fail",
      "syntax_check": "not-run",
      "git_diff": "not-run"
    },
    "G6_independent_rerun": "not-run"
  },
  "findings": [
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "ruff check fails on the in-scope files",
      "file": "kimi-cli/src/kimi_cli/tools/__init__.py",
      "evidence": "reviews/tools/gates/C00.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_tools_init_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "1 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/__init__.py",
      "evidence": "gates/C00.json complexity_max=34",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_tools_init_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/10)",
      "file": "kimi-cli/tests/tools/test_tools_init_gate.py",
      "evidence": "kimi-cli/tests/tools/test_tools_init_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 10 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_tools_init_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 3,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "1 functions exceed complexity 12",
      "G4 probe suite missing/insufficient (0/10)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C00.txt",
  "git_sha": "d684f5a8cd5cfc39549a6a3566b83d9a454deec7\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
