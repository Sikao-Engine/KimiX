# K04 — bash (Bash) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=6 tests=6 registration=`Bash` (src/kimix/tools/file/bash/bash_tool.py:636) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=52 longest_fn_loc=325 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | fail | floor=80 no coverage line recorded |
| G4 behavior | pass | `tests/unit/tools/test_bash_gate.py` required=15 present=45 |
| G5 regression | fail | pytest=fail (rc=2); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/file/bash/bash_tool.py` | 1461 |
| `src/kimix/tools/file/bash/bash_fix.py` | 135 |
| `src/kimix/tools/file/bash/shell_common.py` | 159 |
| `src/kimix/tools/file/bash/safety.py` | 665 |
| `src/kimix/tools/file/bash/output_enhance.py` | 106 |
| `src/kimix/tools/file/bash/__init__.py` | 11 |

Tests in scope: `tests/test_bash.py`, `tests/test_bash_e2e.py`, `tests/test_shell_common.py`, `tests/test_shell_safety.py`, `tests/test_output_enhance.py`, `tests/native/test_shell_security_equivalence.py`

Module LOC total: **2537**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `Bash` | `src/kimix/tools/file/bash/bash_tool.py:636` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `__call__` | 325 | 51 | 50 | 4 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `detect_self_kill` | 173 | 52 | 51 | 4 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/safety.py` |
| `_continue_session` | 75 | 12 | 11 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_execute_background` | 65 | 11 | 10 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_find_git_bash_windows` | 51 | 13 | 12 | 4 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_format_session_result` | 51 | 11 | 10 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `__init__` | 45 | 9 | 8 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_windows_parent_map` | 44 | 4 | 3 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/safety.py` |
| `_continuation_may_be_incomplete` | 41 | 5 | 4 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_format_background_output` | 41 | 10 | 9 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_process_output` | 39 | 9 | 8 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_agent_pids` | 34 | 12 | 11 | 3 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/safety.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **2537** → decision: **split (plan)**
* longest function `__call__` = 325 LOC (`C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py`)
* functions > 120 LOC: **2** · complexity > 12: **3**

Threshold breaches recorded:

| breach |
|---|
| `src/kimix/tools/file/bash/bash_tool.py::_find_git_bash_windows complexity=13 > 12` |
| `src/kimix/tools/file/bash/bash_tool.py::__call__ loc=325 > 120` |
| `src/kimix/tools/file/bash/bash_tool.py::__call__ complexity=51 > 12` |
| `src/kimix/tools/file/bash/safety.py::detect_self_kill loc=173 > 120` |
| `src/kimix/tools/file/bash/safety.py::detect_self_kill complexity=52 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `detect_self_kill` | 52 | 173 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/safety.py` |
| `__call__` | 51 | 325 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_find_git_bash_windows` | 13 | 51 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_continue_session` | 12 | 75 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/bash_tool.py` |
| `_agent_pids` | 12 | 34 | `C:/dev/kimi-agent/src/kimix/tools/file/bash/safety.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 15 `ToolError` + 4 `ToolOk` return sites, 2 `ToolError`/`SkipThisTool` raises |
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

* floor **80** · result **fail**
* `no coverage line recorded`
* pytest `fail` (rc=2)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **15** · present **45** (`tests/unit/tools/test_bash_gate.py`) → **pass**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K04.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K04.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `52192b7c3a1baa66c96e0015d2868f3b106fc043
[elapsed 0.0s, rc=0]`
* branch `review/tools-hardgate` · base tag `tools-review/p0-base`
* the commit/tag for this unit is assigned by the orchestrator on acceptance

## 9. Duplication / native-parity / policy notes

* No duplicate tool name recorded for this ID.
* `subagent` (C01/K01) is waived in `tools/gate_dup_allowlist.txt` (intentionally distinct defaults).
* Cross-cutting sweeps: `reviews/tools/x01..x06*.md`.

## 10. Verdict + JSON

verdict: **minor**

```json
{
  "id": "K04",
  "tool": "bash (Bash)",
  "files": [
    "src/kimix/tools/file/bash/bash_tool.py",
    "src/kimix/tools/file/bash/bash_fix.py",
    "src/kimix/tools/file/bash/shell_common.py",
    "src/kimix/tools/file/bash/safety.py",
    "src/kimix/tools/file/bash/output_enhance.py",
    "src/kimix/tools/file/bash/__init__.py"
  ],
  "verdict": "minor",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 52,
      "longest_fn_loc": 325,
      "loc_regression": "none",
      "vulture_hits": 0,
      "vulture_waived": 0
    },
    "G2_risk": {
      "items_answered": 13,
      "open_high": 0,
      "open_medium": 2,
      "waived_medium": 0
    },
    "G3_coverage": {
      "module": "kimix.tools.file.bash.bash_tool",
      "floor": 80,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/unit/tools/test_bash_gate.py",
      "probes_required": 15,
      "probes_present": 45,
      "result": "pass"
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
      "file": "src/kimix/tools/file/bash/bash_tool.py",
      "evidence": "reviews/tools/gates/K04.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "tests/unit/tools/test_bash_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "3 functions exceed complexity 12",
      "file": "src/kimix/tools/file/bash/bash_tool.py",
      "evidence": "gates/K04.json complexity_max=52",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "tests/unit/tools/test_bash_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "2 functions exceed 120 LOC",
      "file": "src/kimix/tools/file/bash/bash_tool.py",
      "evidence": "longest = __call__ (325 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "tests/unit/tools/test_bash_gate.py::test_no_function_over_120_loc"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 3,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "3 functions exceed complexity 12",
      "2 functions exceed 120 LOC"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K04.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
