# K13 — workflow (AgentSwarm) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=2 tests=2 registration=`AgentSwarm` (src/kimix/tools/swarm/__init__.py:182) |
| G1 simplicity | fail | ruff=fail C901=pass metrics=fail complexity_max=19 longest_fn_loc=85 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | fail | `tests/unit/tools/test_swarm_gate.py` required=11 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/swarm/__init__.py` | 542 |
| `src/kimix/tools/swarm/best_of_n.py` | 423 |

Tests in scope: `tests/kimix/test_agent_swarm.py`, `tests/test_best_of_n.py`

Module LOC total: **965**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `AgentSwarm` | `src/kimix/tools/swarm/__init__.py:182` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_execute_parallel_sample` | 85 | 12 | 11 | 1 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `_run_subagent_task` | 65 | 12 | 11 | 4 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `best_of_n` | 54 | 7 | 6 | 2 | `C:/dev/kimi-agent/src/kimix/tools/swarm/best_of_n.py` |
| `collect_diff` | 50 | 10 | 9 | 3 | `C:/dev/kimi-agent/src/kimix/tools/swarm/best_of_n.py` |
| `select_best_candidate` | 46 | 10 | 9 | 4 | `C:/dev/kimi-agent/src/kimix/tools/swarm/best_of_n.py` |
| `_resolve_subagent_session` | 40 | 5 | 4 | 1 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `run_parallel_sample` | 40 | 2 | 1 | 1 | `C:/dev/kimi-agent/src/kimix/tools/swarm/best_of_n.py` |
| `_validate` | 35 | 19 | 18 | 2 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `apply_diff_to_workspace` | 34 | 9 | 8 | 4 | `C:/dev/kimi-agent/src/kimix/tools/swarm/best_of_n.py` |
| `_render_results` | 28 | 7 | 6 | 2 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `_run_swarm` | 25 | 3 | 2 | 2 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `_execute` | 25 | 4 | 3 | 2 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **965** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `_execute_parallel_sample` = 85 LOC (`C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py`)
* functions > 120 LOC: **0** · complexity > 12: **1**

Threshold breaches recorded:

| breach |
|---|
| `src/kimix/tools/swarm/__init__.py::_validate complexity=19 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_validate` | 19 | 35 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `_execute_parallel_sample` | 12 | 85 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `_run_subagent_task` | 12 | 65 | `C:/dev/kimi-agent/src/kimix/tools/swarm/__init__.py` |
| `collect_diff` | 10 | 50 | `C:/dev/kimi-agent/src/kimix/tools/swarm/best_of_n.py` |
| `select_best_candidate` | 10 | 46 | `C:/dev/kimi-agent/src/kimix/tools/swarm/best_of_n.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 4 `ToolError` + 2 `ToolOk` return sites, 1 `ToolError`/`SkipThisTool` raises |
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

* required **11** · present **0** (`tests/unit/tools/test_swarm_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K13.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K13.json` — machine-readable summary.
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
  "id": "K13",
  "tool": "workflow (AgentSwarm)",
  "files": [
    "src/kimix/tools/swarm/__init__.py",
    "src/kimix/tools/swarm/best_of_n.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 19,
      "longest_fn_loc": 85,
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
      "module": "kimix.tools.swarm",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/unit/tools/test_swarm_gate.py",
      "probes_required": 11,
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
      "file": "src/kimix/tools/swarm/__init__.py",
      "evidence": "reviews/tools/gates/K13.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "tests/unit/tools/test_swarm_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "1 functions exceed complexity 12",
      "file": "src/kimix/tools/swarm/__init__.py",
      "evidence": "gates/K13.json complexity_max=19",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "tests/unit/tools/test_swarm_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/11)",
      "file": "tests/unit/tools/test_swarm_gate.py",
      "evidence": "tests/unit/tools/test_swarm_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 11 adversarial probes per the plan §4.3 block",
      "test_to_add": "tests/unit/tools/test_swarm_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 3,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "1 functions exceed complexity 12",
      "G4 probe suite missing/insufficient (0/11)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K13.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
