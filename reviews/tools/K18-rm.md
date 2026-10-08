# K18 — Rm review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=1 tests=0 registration=`Mkdir` (src/kimix/tools/file/__init__.py:13), `Rm` (src/kimix/tools/file/__init__.py:38) |
| G1 simplicity | fail | ruff=fail C901=pass metrics=pass complexity_max=2 longest_fn_loc=19 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | not-run | floor=90 no coverage line recorded |
| G4 behavior | fail | `tests/unit/tools/test_file_ops_gate.py` required=5 present=0 |
| G5 regression | not-run | pytest=not-run (rc=None); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/file/__init__.py` | 61 |

Tests in scope: **none**

Module LOC total: **61**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `Mkdir` | `src/kimix/tools/file/__init__.py:13` | CallableTool2 | - | True |
| `Rm` | `src/kimix/tools/file/__init__.py:38` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `__call__` | 19 | 2 | 1 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/__init__.py` |
| `__call__` | 12 | 2 | 1 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/__init__.py` |
| `_remove` | 5 | 2 | 1 | 1 | `C:/dev/kimi-agent/src/kimix/tools/file/__init__.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `pass`
* module LOC **61** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `__call__` = 19 LOC (`C:/dev/kimi-agent/src/kimix/tools/file/__init__.py`)
* functions > 120 LOC: **0** · complexity > 12: **0**

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `__call__` | 2 | 19 | `C:/dev/kimi-agent/src/kimix/tools/file/__init__.py` |
| `__call__` | 2 | 12 | `C:/dev/kimi-agent/src/kimix/tools/file/__init__.py` |
| `_remove` | 2 | 5 | `C:/dev/kimi-agent/src/kimix/tools/file/__init__.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 2 `ToolError` + 2 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* floor **90** · result **not-run**
* `no coverage line recorded`
* pytest `not-run` (rc=None)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **5** · present **0** (`tests/unit/tools/test_file_ops_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K18.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K18.json` — machine-readable summary.
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
  "id": "K18",
  "tool": "Rm",
  "files": [
    "src/kimix/tools/file/__init__.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 2,
      "longest_fn_loc": 19,
      "loc_regression": "none",
      "vulture_hits": 0,
      "vulture_waived": 0
    },
    "G2_risk": {
      "items_answered": 13,
      "open_high": 2,
      "open_medium": 1,
      "waived_medium": 0
    },
    "G3_coverage": {
      "module": "kimix.tools.file",
      "floor": 90,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/unit/tools/test_file_ops_gate.py",
      "probes_required": 5,
      "probes_present": 0,
      "result": "fail"
    },
    "G5_regression": {
      "root_suite": "blocked (missing tests/bash/cases.json)",
      "cli_suite": "not-run",
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
      "file": "src/kimix/tools/file/__init__.py",
      "evidence": "reviews/tools/gates/K18.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "tests/unit/tools/test_file_ops_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "no tests in scope for this tool",
      "file": "src/kimix/tools/file/__init__.py",
      "evidence": "tools/gen_inventory.py declares no test glob for this ID",
      "impact": "the S3 coverage gate cannot be satisfied at all",
      "fix": "author the probe suite and register it in the inventory",
      "test_to_add": "tests/unit/tools/test_file_ops_gate.py"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/5)",
      "file": "tests/unit/tools/test_file_ops_gate.py",
      "evidence": "tests/unit/tools/test_file_ops_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 5 adversarial probes per the plan §4.3 block",
      "test_to_add": "tests/unit/tools/test_file_ops_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 3,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "no tests in scope for this tool",
      "G4 probe suite missing/insufficient (0/5)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K18.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
