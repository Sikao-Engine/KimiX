# C15 — HashRead / HashLine / HashEdit review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=1 tests=3 registration=`HashRead` (kimi-cli/src/kimi_cli/tools/file/hash_line.py:680), `HashEdit` (kimi-cli/src/kimi_cli/tools/file/hash_line.py:875) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=61 longest_fn_loc=205 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_hash_line_gate.py` required=10 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/file/hash_line.py` | 1157 |

Tests in scope: `kimi-cli/tests/tools/test_hash_line.py`, `kimi-cli/tests/tools/test_plan25_integration.py`, `kimi-cli/tests/native/test_diff_glob_tools_equivalence.py`

Module LOC total: **1157**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `HashRead` | `kimi-cli/src/kimi_cli/tools/file/hash_line.py:680` | CallableTool2 | - | True |
| `HashEdit` | `kimi-cli/src/kimi_cli/tools/file/hash_line.py:875` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `apply_hashline_edits` | 205 | 61 | 60 | 6 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_do_edit` | 135 | 18 | 17 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_do_read` | 110 | 15 | 14 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `generate_hash_aware_diff` | 89 | 20 | 19 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_recover_stale_anchors` | 70 | 12 | 11 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `validate_anchor_ref` | 52 | 12 | 11 | 6 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_validate_path` | 39 | 5 | 4 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_validate_path` | 39 | 5 | 4 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `__str__` | 36 | 8 | 7 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `compute_line_hash` | 33 | 9 | 8 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_deduplicate_edits` | 27 | 10 | 9 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_cumulative_hashes` | 21 | 6 | 5 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **1157** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `apply_hashline_edits` = 205 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py`)
* functions > 120 LOC: **2** · complexity > 12: **4**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/file/hash_line.py::apply_hashline_edits loc=205 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/hash_line.py::apply_hashline_edits complexity=61 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/hash_line.py::generate_hash_aware_diff complexity=20 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/hash_line.py::_do_read complexity=15 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/hash_line.py::_do_edit loc=135 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/hash_line.py::_do_edit complexity=18 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `apply_hashline_edits` | 61 | 205 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `generate_hash_aware_diff` | 20 | 89 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_do_edit` | 18 | 135 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_do_read` | 15 | 110 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| `_recover_stale_anchors` | 12 | 70 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/hash_line.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 18 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* required **10** · present **0** (`kimi-cli/tests/tools/test_hash_line_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C15.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C15.json` — machine-readable summary.
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
  "id": "C15",
  "tool": "HashRead / HashLine / HashEdit",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/hash_line.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 61,
      "longest_fn_loc": 205,
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
      "module": "kimi_cli.tools.file.hash_line",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_hash_line_gate.py",
      "probes_required": 10,
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
      "file": "kimi-cli/src/kimi_cli/tools/file/hash_line.py",
      "evidence": "reviews/tools/gates/C15.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_hash_line_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "4 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/file/hash_line.py",
      "evidence": "gates/C15.json complexity_max=61",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_hash_line_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "2 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/file/hash_line.py",
      "evidence": "longest = apply_hashline_edits (205 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_hash_line_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/10)",
      "file": "kimi-cli/tests/tools/test_hash_line_gate.py",
      "evidence": "kimi-cli/tests/tools/test_hash_line_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 10 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_hash_line_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 4,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "4 functions exceed complexity 12",
      "2 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/10)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C15.txt",
  "git_sha": "d684f5a8cd5cfc39549a6a3566b83d9a454deec7\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
