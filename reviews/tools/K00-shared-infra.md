# K00 — shared infra review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=4 tests=7 registration=- |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=35 longest_fn_loc=279 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | fail | floor=80 no coverage line recorded |
| G4 behavior | fail | `tests/unit/tools/test_common_gate.py` required=16 present=0 |
| G5 regression | fail | pytest=fail (rc=1); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/common.py` | 2342 |
| `src/kimix/tools/prompt_common.py` | 134 |
| `src/kimix/tools/security.py` | 91 |
| `src/kimix/tools/check_fmt.py` | 33 |

Tests in scope: `tests/test_tools_async.py`, `tests/unit/tools/test_prompt_common.py`, `tests/unit/tools/test_security.py`, `tests/test_check_fmt.py`, `tests/test_token_filter.py`, `tests/test_filter_output.py`, `tests/test_temp_folder_cleanup.py`

Module LOC total: **2600**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_run_process_bg` | 279 | 35 | 34 | 5 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_token_filter_output` | 112 | 12 | 11 | 2 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_dedup_output` | 104 | 19 | 18 | 4 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_summarize_long_output_async` | 90 | 8 | 7 | 3 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_split_shell_segments` | 80 | 23 | 22 | 10 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_maybe_rewrite_shell_command_with_rtk` | 70 | 18 | 17 | 2 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_read_shell_word` | 66 | 21 | 20 | 7 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_truncate_lines` | 65 | 11 | 10 | 3 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_extract_and_save_long_param` | 61 | 11 | 10 | 5 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `kill_child_tree` | 53 | 7 | 6 | 2 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_looks_like_malformed_json_param` | 48 | 21 | 20 | 4 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_extract_content_from_malformed` | 47 | 20 | 19 | 5 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **2600** → decision: **split (plan)**
* longest function `_run_process_bg` = 279 LOC (`C:/dev/kimi-agent/src/kimix/tools/common.py`)
* functions > 120 LOC: **1** · complexity > 12: **7**

Threshold breaches recorded:

| breach |
|---|
| `src/kimix/tools/common.py::_looks_like_malformed_json_param complexity=21 > 12` |
| `src/kimix/tools/common.py::_extract_content_from_malformed complexity=20 > 12` |
| `src/kimix/tools/common.py::_dedup_output complexity=19 > 12` |
| `src/kimix/tools/common.py::_split_shell_segments complexity=23 > 12` |
| `src/kimix/tools/common.py::_read_shell_word complexity=21 > 12` |
| `src/kimix/tools/common.py::_maybe_rewrite_shell_command_with_rtk complexity=18 > 12` |
| `src/kimix/tools/common.py::_run_process_bg loc=279 > 120` |
| `src/kimix/tools/common.py::_run_process_bg complexity=35 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_run_process_bg` | 35 | 279 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_split_shell_segments` | 23 | 80 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_read_shell_word` | 21 | 66 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_looks_like_malformed_json_param` | 21 | 48 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |
| `_extract_content_from_malformed` | 20 | 47 | `C:/dev/kimi-agent/src/kimix/tools/common.py` |

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

* floor **80** · result **fail**
* `no coverage line recorded`
* pytest `fail` (rc=1)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **16** · present **0** (`tests/unit/tools/test_common_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K00.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K00.json` — machine-readable summary.
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

verdict: **major**

```json
{
  "id": "K00",
  "tool": "shared infra",
  "files": [
    "src/kimix/tools/common.py",
    "src/kimix/tools/prompt_common.py",
    "src/kimix/tools/security.py",
    "src/kimix/tools/check_fmt.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 35,
      "longest_fn_loc": 279,
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
      "module": "kimix.tools.common",
      "floor": 80,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/unit/tools/test_common_gate.py",
      "probes_required": 16,
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
      "file": "src/kimix/tools/common.py",
      "evidence": "reviews/tools/gates/K00.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "tests/unit/tools/test_common_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "7 functions exceed complexity 12",
      "file": "src/kimix/tools/common.py",
      "evidence": "gates/K00.json complexity_max=35",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "tests/unit/tools/test_common_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "1 functions exceed 120 LOC",
      "file": "src/kimix/tools/common.py",
      "evidence": "longest = _run_process_bg (279 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "tests/unit/tools/test_common_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/16)",
      "file": "tests/unit/tools/test_common_gate.py",
      "evidence": "tests/unit/tools/test_common_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 16 adversarial probes per the plan §4.3 block",
      "test_to_add": "tests/unit/tools/test_common_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 4,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "7 functions exceed complexity 12",
      "1 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/16)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K00.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
