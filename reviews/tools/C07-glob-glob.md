# C07 — glob (Glob) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=4 tests=5 registration=`Glob` (kimi-cli/src/kimi_cli/tools/file/glob.py:476) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=37 longest_fn_loc=199 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_glob_gate.py` required=12 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/file/glob.py` | 707 |
| `kimi-cli/src/kimi_cli/tools/file/glob.md` | 3 |
| `kimi-cli/src/kimi_cli/tools/file/output_utils.py` | 239 |
| `kimi-cli/src/kimi_cli/tools/file/fs_cache.py` | 137 |

Tests in scope: `kimi-cli/tests/tools/test_glob.py`, `kimi-cli/tests/tools/test_fs_cache_invalidation.py`, `kimi-cli/tests/tools/test_additional_dirs.py`, `kimi-cli/tests/tools/test_work_dir_resolution.py`, `kimi-cli/tests/tools/test_output_utils.py`

Module LOC total: **1083**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `Glob` | `kimi-cli/src/kimi_cli/tools/file/glob.py:476` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `__call__` | 199 | 37 | 36 | 7 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `parse_rtk_rg_output` | 79 | 7 | 6 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/output_utils.py` |
| `_gitignore_match` | 70 | 24 | 23 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_parse_gitignore` | 52 | 11 | 10 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_is_ignored_by_gitignore` | 46 | 11 | 10 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_get_gitignore_rules` | 38 | 8 | 7 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `fold_lines` | 38 | 7 | 6 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/output_utils.py` |
| `dedup_lines` | 38 | 7 | 6 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/output_utils.py` |
| `invalidate_gitignore_cache` | 29 | 9 | 8 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_is_unsafe_recursive_pattern` | 22 | 2 | 1 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_top_dirs_summary` | 22 | 6 | 5 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `invalidate_fs_scan_after_rename` | 22 | 5 | 4 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/fs_cache.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **1083** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `__call__` = 199 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py`)
* functions > 120 LOC: **1** · complexity > 12: **2**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/file/glob.py::_gitignore_match complexity=24 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/glob.py::__call__ loc=199 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/glob.py::__call__ complexity=37 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `__call__` | 37 | 199 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_gitignore_match` | 24 | 70 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_parse_gitignore` | 11 | 52 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `_is_ignored_by_gitignore` | 11 | 46 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |
| `invalidate_gitignore_cache` | 9 | 29 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/glob.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 4 `ToolError` + 1 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* required **12** · present **0** (`kimi-cli/tests/tools/test_glob_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C07.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C07.json` — machine-readable summary.
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
  "id": "C07",
  "tool": "glob (Glob)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/glob.py",
    "kimi-cli/src/kimi_cli/tools/file/glob.md",
    "kimi-cli/src/kimi_cli/tools/file/output_utils.py",
    "kimi-cli/src/kimi_cli/tools/file/fs_cache.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 37,
      "longest_fn_loc": 199,
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
      "module": "kimi_cli.tools.file.glob",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_glob_gate.py",
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
      "file": "kimi-cli/src/kimi_cli/tools/file/glob.py",
      "evidence": "reviews/tools/gates/C07.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_glob_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "2 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/file/glob.py",
      "evidence": "gates/C07.json complexity_max=37",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_glob_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "1 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/file/glob.py",
      "evidence": "longest = __call__ (199 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_glob_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/12)",
      "file": "kimi-cli/tests/tools/test_glob_gate.py",
      "evidence": "kimi-cli/tests/tools/test_glob_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 12 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_glob_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 4,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "2 functions exceed complexity 12",
      "1 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/12)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C07.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
