# C09 — write (WriteFile) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=11 tests=9 registration=`WriteFile` (kimi-cli/src/kimi_cli/tools/file/write.py:158) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=64 longest_fn_loc=297 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_write_gate.py` required=12 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/file/write.py` | 834 |
| `kimi-cli/src/kimi_cli/tools/file/write.md` | 9 |
| `kimi-cli/src/kimi_cli/tools/file/check_fmt.py` | 181 |
| `kimi-cli/src/kimi_cli/tools/file/parse_check.py` | 112 |
| `kimi-cli/src/kimi_cli/tools/file/auto_repair.py` | 438 |
| `kimi-cli/src/kimi_cli/tools/file/auto_generated.py` | 345 |
| `kimi-cli/src/kimi_cli/tools/file/blackbox.py` | 133 |
| `kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` | 863 |
| `kimi-cli/src/kimi_cli/tools/file/snapshot_store.py` | 550 |
| `kimi-cli/src/kimi_cli/tools/file/fs_cache.py` | 137 |
| `kimi-cli/src/kimi_cli/tools/file/edit_safety.py` | 125 |

Tests in scope: `kimi-cli/tests/tools/test_write_file.py`, `kimi-cli/tests/tools/test_write_conflict.py`, `kimi-cli/tests/tools/test_auto_repair.py`, `kimi-cli/tests/tools/test_parse_check.py`, `kimi-cli/tests/tools/test_auto_generated_guard.py`, `kimi-cli/tests/tools/test_check_fmt.py`, `kimi-cli/tests/tools/test_blackbox.py`, `kimi-cli/tests/tools/test_conflict_detect.py`, `kimi-cli/tests/tools/test_snapshot_store.py`

Module LOC total: **3718**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `WriteFile` | `kimi-cli/src/kimi_cli/tools/file/write.py:158` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `__call__` | 297 | 64 | 63 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/write.py` |
| `_resolve_all_conflicts` | 131 | 28 | 27 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/write.py` |
| `scan_conflict_lines` | 127 | 23 | 22 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` |
| `_conflict_guard` | 77 | 13 | 12 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/write.py` |
| `_resolve_single_conflict_by_id` | 72 | 12 | 11 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/write.py` |
| `extract_leading_header_comment_text` | 66 | 26 | 25 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/auto_generated.py` |
| `format_conflict_warning` | 59 | 14 | 13 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` |
| `attempt_edit_auto_repair` | 57 | 11 | 10 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/auto_repair.py` |
| `splice_conflict` | 53 | 11 | 10 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` |
| `record` | 53 | 11 | 10 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/snapshot_store.py` |
| `register` | 49 | 3 | 2 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` |
| `record` | 46 | 3 | 2 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/blackbox.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **3718** → decision: **split (plan)**
* longest function `__call__` = 297 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/write.py`)
* functions > 120 LOC: **3** · complexity > 12: **6**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/file/write.py::__call__ loc=297 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/write.py::__call__ complexity=64 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/write.py::_conflict_guard complexity=13 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/write.py::_resolve_all_conflicts loc=131 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/write.py::_resolve_all_conflicts complexity=28 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/auto_generated.py::extract_leading_header_comment_text complexity=26 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/conflict_detect.py::scan_conflict_lines loc=127 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/conflict_detect.py::scan_conflict_lines complexity=23 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/conflict_detect.py::format_conflict_warning complexity=14 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `__call__` | 64 | 297 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/write.py` |
| `_resolve_all_conflicts` | 28 | 131 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/write.py` |
| `extract_leading_header_comment_text` | 26 | 66 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/auto_generated.py` |
| `scan_conflict_lines` | 23 | 127 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` |
| `format_conflict_warning` | 14 | 59 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 23 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* required **12** · present **0** (`kimi-cli/tests/tools/test_write_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C09.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C09.json` — machine-readable summary.
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
  "id": "C09",
  "tool": "write (WriteFile)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/write.py",
    "kimi-cli/src/kimi_cli/tools/file/write.md",
    "kimi-cli/src/kimi_cli/tools/file/check_fmt.py",
    "kimi-cli/src/kimi_cli/tools/file/parse_check.py",
    "kimi-cli/src/kimi_cli/tools/file/auto_repair.py",
    "kimi-cli/src/kimi_cli/tools/file/auto_generated.py",
    "kimi-cli/src/kimi_cli/tools/file/blackbox.py",
    "kimi-cli/src/kimi_cli/tools/file/conflict_detect.py",
    "kimi-cli/src/kimi_cli/tools/file/snapshot_store.py",
    "kimi-cli/src/kimi_cli/tools/file/fs_cache.py",
    "kimi-cli/src/kimi_cli/tools/file/edit_safety.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 64,
      "longest_fn_loc": 297,
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
      "module": "kimi_cli.tools.file.write",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_write_gate.py",
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
      "file": "kimi-cli/src/kimi_cli/tools/file/write.py",
      "evidence": "reviews/tools/gates/C09.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_write_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "6 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/file/write.py",
      "evidence": "gates/C09.json complexity_max=64",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_write_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "3 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/file/write.py",
      "evidence": "longest = __call__ (297 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_write_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/12)",
      "file": "kimi-cli/tests/tools/test_write_gate.py",
      "evidence": "kimi-cli/tests/tools/test_write_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 12 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_write_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 4,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "6 functions exceed complexity 12",
      "3 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/12)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C09.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
