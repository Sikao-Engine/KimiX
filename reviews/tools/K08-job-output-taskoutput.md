# K08 — job_output (TaskOutput) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=2 tests=3 registration=`TaskOutput` (src/kimix/tools/background/__init__.py:104) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=29 longest_fn_loc=152 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | fail | `tests/unit/tools/test_job_output_gate.py` required=12 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/background/__init__.py` | 508 |
| `src/kimix/tools/background/utils.py` | 626 |

Tests in scope: `tests/unit/tools/test_taskoutput.py`, `tests/test_background_utils.py`, `tests/test_stream_repeat_output.py`

Module LOC total: **1134**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `TaskOutput` | `src/kimix/tools/background/__init__.py:104` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_get_output` | 152 | 29 | 28 | 4 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `wait_for_output` | 63 | 11 | 10 | 3 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |
| `_kill_task` | 61 | 7 | 6 | 2 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `_get_history_output` | 60 | 10 | 9 | 2 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `wait_with_inactivity_timeout` | 53 | 8 | 7 | 2 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |
| `bounded_append` | 35 | 6 | 5 | 2 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |
| `start` | 35 | 2 | 1 | 2 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |
| `remove_task_id` | 33 | 4 | 3 | 4 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |
| `__call__` | 28 | 7 | 6 | 3 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `_list_tasks` | 26 | 8 | 7 | 2 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `_process_completed_output` | 26 | 3 | 2 | 1 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `drain_output_sync` | 22 | 6 | 5 | 3 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **1134** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `_get_output` = 152 LOC (`C:/dev/kimi-agent/src/kimix/tools/background/__init__.py`)
* functions > 120 LOC: **1** · complexity > 12: **1**

Threshold breaches recorded:

| breach |
|---|
| `src/kimix/tools/background/__init__.py::_get_output loc=152 > 120` |
| `src/kimix/tools/background/__init__.py::_get_output complexity=29 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_get_output` | 29 | 152 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `wait_for_output` | 11 | 63 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |
| `_get_history_output` | 10 | 60 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |
| `wait_with_inactivity_timeout` | 8 | 53 | `C:/dev/kimi-agent/src/kimix/tools/background/utils.py` |
| `_list_tasks` | 8 | 26 | `C:/dev/kimi-agent/src/kimix/tools/background/__init__.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 10 `ToolError` + 4 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* floor **85** · result **pass**
* `no coverage line recorded`
* pytest `pass` (rc=0)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **12** · present **0** (`tests/unit/tools/test_job_output_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K08.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K08.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `52192b7c3a1baa66c96e0015d2868f3b106fc043
[elapsed 0.1s, rc=0]`
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
  "id": "K08",
  "tool": "job_output (TaskOutput)",
  "files": [
    "src/kimix/tools/background/__init__.py",
    "src/kimix/tools/background/utils.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 29,
      "longest_fn_loc": 152,
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
      "module": "kimix.tools.background",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/unit/tools/test_job_output_gate.py",
      "probes_required": 12,
      "probes_present": 0,
      "result": "fail"
    },
    "G5_regression": {
      "root_suite": "blocked (missing tests/bash/cases.json)",
      "cli_suite": "pass",
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
      "file": "src/kimix/tools/background/__init__.py",
      "evidence": "reviews/tools/gates/K08.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "tests/unit/tools/test_job_output_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "1 functions exceed complexity 12",
      "file": "src/kimix/tools/background/__init__.py",
      "evidence": "gates/K08.json complexity_max=29",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "tests/unit/tools/test_job_output_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "1 functions exceed 120 LOC",
      "file": "src/kimix/tools/background/__init__.py",
      "evidence": "longest = _get_output (152 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "tests/unit/tools/test_job_output_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/12)",
      "file": "tests/unit/tools/test_job_output_gate.py",
      "evidence": "tests/unit/tools/test_job_output_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 12 adversarial probes per the plan §4.3 block",
      "test_to_add": "tests/unit/tools/test_job_output_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 4,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "1 functions exceed complexity 12",
      "1 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/12)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K08.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.1s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
