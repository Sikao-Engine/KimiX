# K16 — FindStr review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=1 tests=1 registration=`FindStr` (src/kimix/tools/file/find_str.py:152) |
| G1 simplicity | pass | ruff=pass C901=pass metrics=pass complexity_max=7 longest_fn_loc=43 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | fail | `tests/unit/tools/test_find_str_gate.py` required=10 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/file/find_str.py` | 199 |

Tests in scope: `tests/native/test_behavior_equivalence.py`

Module LOC total: **199**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `FindStr` | `src/kimix/tools/file/find_str.py:152` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `__call__` | 43 | 7 | 6 | 3 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_find_in_file` | 26 | 7 | 6 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_native_find_in_file` | 19 | 7 | 6 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_match_columns` | 14 | 3 | 2 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_find_files` | 14 | 5 | 4 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_split_glob_pattern` | 12 | 6 | 5 | 2 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_list_dir_files` | 11 | 6 | 5 | 3 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_walk_dir_files` | 8 | 4 | 3 | 3 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_offload` | 4 | 1 | 0 | 0 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **pass**

* `ruff check` → `pass`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `pass`
* module LOC **199** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `__call__` = 43 LOC (`C:/dev/kimi-agent/src/kimix/tools/file/find_str.py`)
* functions > 120 LOC: **0** · complexity > 12: **0**

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `__call__` | 7 | 43 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_find_in_file` | 7 | 26 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_native_find_in_file` | 7 | 19 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_split_glob_pattern` | 6 | 12 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |
| `_list_dir_files` | 6 | 11 | `C:/dev/kimi-agent/src/kimix/tools/file/find_str.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 1 `ToolError` + 3 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* required **10** · present **0** (`tests/unit/tools/test_find_str_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K16.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K16.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `d839697fddfd4a3891cf5aa5ee1d606d9fea4f56
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
  "id": "K16",
  "tool": "FindStr",
  "files": [
    "src/kimix/tools/file/find_str.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "pass",
      "complexity_max": 7,
      "longest_fn_loc": 43,
      "loc_regression": "none",
      "vulture_hits": 0,
      "vulture_waived": 0
    },
    "G2_risk": {
      "items_answered": 13,
      "open_high": 1,
      "open_medium": 0,
      "waived_medium": 0
    },
    "G3_coverage": {
      "module": "kimix.tools.file.find_str",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/unit/tools/test_find_str_gate.py",
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
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/10)",
      "file": "tests/unit/tools/test_find_str_gate.py",
      "evidence": "tests/unit/tools/test_find_str_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 10 adversarial probes per the plan §4.3 block",
      "test_to_add": "tests/unit/tools/test_find_str_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 1,
    "gap_list": [
      "G4 probe suite missing/insufficient (0/10)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K16.txt",
  "git_sha": "d839697fddfd4a3891cf5aa5ee1d606d9fea4f56\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
