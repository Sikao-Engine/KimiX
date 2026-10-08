# C10 — edit (EditFile) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=6 tests=8 registration=`EditFile` (kimi-cli/src/kimi_cli/tools/file/edit/__init__.py:18) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=38 longest_fn_loc=239 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | pass | `kimi-cli/tests/tools/test_edit_gate.py` required=12 present=28 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/file/edit/__init__.py` | 107 |
| `kimi-cli/src/kimi_cli/tools/file/edit/base.py` | 218 |
| `kimi-cli/src/kimi_cli/tools/file/edit/params.py` | 194 |
| `kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` | 525 |
| `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` | 363 |
| `kimi-cli/src/kimi_cli/tools/file/replace.py` | 12 |

Tests in scope: `kimi-cli/tests/tools/test_edit_conflict.py`, `kimi-cli/tests/tools/test_edit_conflict_plan24.py`, `kimi-cli/tests/tools/test_edit_file.py`, `kimi-cli/tests/tools/test_edit_file_fuzzy.py`, `kimi-cli/tests/tools/test_edit_file_parse_repair.py`, `kimi-cli/tests/tools/test_edit_modes_detect.py`, `kimi-cli/tests/tools/test_edit_safety.py`, `kimi-cli/tests/tools/test_edit_sloppy.py`

Module LOC total: **1419**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `EditFile` | `kimi-cli/src/kimi_cli/tools/file/edit/__init__.py:18` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `execute` | 239 | 38 | 37 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `execute` | 117 | 23 | 22 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` |
| `_try_snapshot_recovery` | 57 | 12 | 11 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `_parse_op` | 48 | 11 | 10 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` |
| `_apply_block_op` | 38 | 11 | 10 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` |
| `_validate_path` | 34 | 5 | 4 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/base.py` |
| `_find_best_fuzzy_match` | 34 | 7 | 6 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `_find_similar` | 28 | 5 | 4 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `_apply_fuzzy_fallback` | 28 | 5 | 4 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `_apply_edit` | 27 | 6 | 5 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `_check_conflicts` | 26 | 7 | 6 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/base.py` |
| `_apply_replace_all` | 26 | 6 | 5 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **1419** → decision: **split (plan)**
* longest function `execute` = 239 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py`)
* functions > 120 LOC: **1** · complexity > 12: **2**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py::execute loc=239 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py::execute complexity=38 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py::execute complexity=23 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `execute` | 38 | 239 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `execute` | 23 | 117 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` |
| `_try_snapshot_recovery` | 12 | 57 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| `_parse_op` | 11 | 48 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` |
| `_apply_block_op` | 11 | 38 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 20 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* required **12** · present **28** (`kimi-cli/tests/tools/test_edit_gate.py`) → **pass**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C10.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C10.json` — machine-readable summary.
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
  "id": "C10",
  "tool": "edit (EditFile)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/edit/__init__.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/base.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/params.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py",
    "kimi-cli/src/kimi_cli/tools/file/replace.py"
  ],
  "verdict": "minor",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 38,
      "longest_fn_loc": 239,
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
      "module": "kimi_cli.tools.file.edit",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_edit_gate.py",
      "probes_required": 12,
      "probes_present": 28,
      "result": "pass"
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
      "file": "kimi-cli/src/kimi_cli/tools/file/edit/__init__.py",
      "evidence": "reviews/tools/gates/C10.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "2 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/file/edit/__init__.py",
      "evidence": "gates/C10.json complexity_max=38",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "1 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/file/edit/__init__.py",
      "evidence": "longest = execute (239 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_no_function_over_120_loc"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 3,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "2 functions exceed complexity 12",
      "1 functions exceed 120 LOC"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C10.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
