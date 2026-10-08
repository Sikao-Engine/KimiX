# C08 — grep (Grep) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=5 tests=7 registration=`Grep` (kimi-cli/src/kimi_cli/tools/file/grep_local.py:1091) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=77 longest_fn_loc=297 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=80 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_grep_gate.py` required=15 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | 2357 |
| `kimi-cli/src/kimi_cli/tools/file/grep_archive.py` | 155 |
| `kimi-cli/src/kimi_cli/tools/file/grep_output.py` | 116 |
| `kimi-cli/src/kimi_cli/tools/file/grep_recorder.py` | 97 |
| `kimi-cli/src/kimi_cli/tools/file/grep_selectors.py` | 320 |

Tests in scope: `kimi-cli/tests/tools/test_grep.py`, `kimi-cli/tests/tools/test_grep_archive.py`, `kimi-cli/tests/tools/test_grep_output.py`, `kimi-cli/tests/tools/test_grep_recorder.py`, `kimi-cli/tests/tools/test_grep_rich.py`, `kimi-cli/tests/tools/test_grep_ripgrep_only.py`, `kimi-cli/tests/tools/test_grep_selectors.py`

Module LOC total: **3045**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `Grep` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py:1091` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_postprocess` | 297 | 77 | 76 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_backup_grep_search` | 233 | 51 | 50 | 6 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `__call__` | 166 | 27 | 26 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_backup_grep_impl` | 158 | 31 | 30 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_rich_call` | 151 | 30 | 29 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_build_rg_args` | 105 | 26 | 25 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_resolve_selector_specs` | 97 | 16 | 15 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `materialize_archive_members` | 71 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_archive.py` |
| `_search_content_single` | 66 | 22 | 21 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_run_rg_subprocess` | 49 | 3 | 2 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `split_path_and_sel` | 46 | 11 | 10 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_selectors.py` |
| `_materialize_sync` | 45 | 8 | 7 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_archive.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **3045** → decision: **split (plan)**
* longest function `_postprocess` = 297 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py`)
* functions > 120 LOC: **5** · complexity > 12: **8**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_build_rg_args complexity=26 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_resolve_selector_specs complexity=16 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::__call__ loc=166 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::__call__ complexity=27 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_rich_call loc=151 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_rich_call complexity=30 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_postprocess loc=297 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_postprocess complexity=77 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_backup_grep_impl loc=158 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_backup_grep_impl complexity=31 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_backup_grep_search loc=233 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_backup_grep_search complexity=51 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/grep_local.py::_search_content_single complexity=22 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_postprocess` | 77 | 297 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_backup_grep_search` | 51 | 233 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_backup_grep_impl` | 31 | 158 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `_rich_call` | 30 | 151 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| `__call__` | 27 | 166 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/grep_local.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 27 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* floor **80** · result **pass**
* `no coverage line recorded`
* pytest `pass` (rc=0)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **15** · present **0** (`kimi-cli/tests/tools/test_grep_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C08.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C08.json` — machine-readable summary.
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
  "id": "C08",
  "tool": "grep (Grep)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/grep_local.py",
    "kimi-cli/src/kimi_cli/tools/file/grep_archive.py",
    "kimi-cli/src/kimi_cli/tools/file/grep_output.py",
    "kimi-cli/src/kimi_cli/tools/file/grep_recorder.py",
    "kimi-cli/src/kimi_cli/tools/file/grep_selectors.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 77,
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
      "module": "kimi_cli.tools.file.grep_local",
      "floor": 80,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_grep_gate.py",
      "probes_required": 15,
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
      "file": "kimi-cli/src/kimi_cli/tools/file/grep_local.py",
      "evidence": "reviews/tools/gates/C08.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_grep_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "8 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/file/grep_local.py",
      "evidence": "gates/C08.json complexity_max=77",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_grep_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "5 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/file/grep_local.py",
      "evidence": "longest = _postprocess (297 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_grep_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/15)",
      "file": "kimi-cli/tests/tools/test_grep_gate.py",
      "evidence": "kimi-cli/tests/tools/test_grep_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 15 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_grep_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 4,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "8 functions exceed complexity 12",
      "5 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/15)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C08.txt",
  "git_sha": "d684f5a8cd5cfc39549a6a3566b83d9a454deec7\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
