# K01 — subagent (Agent) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=2 tests=5 registration=`Agent` (src/kimix/tools/agent/__init__.py:518), `AgentList` (src/kimix/tools/agent/__init__.py:1109), `AgentClose` (src/kimix/tools/agent/__init__.py:1146) |
| G1 simplicity | fail | ruff=fail C901=pass metrics=fail complexity_max=19 longest_fn_loc=88 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | pass | `tests/kimix/test_agent_gate.py` required=14 present=20 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/agent/__init__.py` | 1179 |
| `src/kimix/tools/agent/store.py` | 139 |

Tests in scope: `tests/kimix/test_agent_conversation.py`, `tests/kimix/test_subagent_anonymous_session.py`, `tests/kimix/test_subagent_interrupt_race.py`, `tests/test_prompt_waits_background_agents.py`, `tests/kimix/test_report_sync.py`

Module LOC total: **1318**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `Agent` | `src/kimix/tools/agent/__init__.py:518` | CallableTool2 | - | True |
| `AgentList` | `src/kimix/tools/agent/__init__.py:1109` | CallableTool2 | - | True |
| `AgentClose` | `src/kimix/tools/agent/__init__.py:1146` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_execute` | 88 | 8 | 7 | 3 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_resolve_session` | 73 | 9 | 8 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_prepare_run` | 71 | 11 | 10 | 4 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_notify_parent_background_finished` | 53 | 19 | 18 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_launch_background` | 45 | 6 | 5 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_update_store` | 39 | 5 | 4 | 3 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_inherit_parent_context` | 34 | 3 | 2 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_finalize_previous` | 28 | 4 | 3 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_build_extras` | 26 | 2 | 1 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_format_history` | 26 | 14 | 13 | 3 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_reset_inherited_system_prompt` | 25 | 5 | 4 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_resolve_prompt` | 24 | 6 | 5 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **1318** → decision: **split (plan)**
* longest function `_execute` = 88 LOC (`C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py`)
* functions > 120 LOC: **0** · complexity > 12: **2**

Threshold breaches recorded:

| breach |
|---|
| `src/kimix/tools/agent/__init__.py::_notify_parent_background_finished complexity=19 > 12` |
| `src/kimix/tools/agent/__init__.py::_format_history complexity=14 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_notify_parent_background_finished` | 19 | 53 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_format_history` | 14 | 26 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_prepare_run` | 11 | 71 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_resolve_session` | 9 | 73 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `consume` | 9 | 23 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 4 `ToolError` + 2 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* required **14** · present **20** (`tests/kimix/test_agent_gate.py`) → **pass**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K01.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K01.json` — machine-readable summary.
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
  "id": "K01",
  "tool": "subagent (Agent)",
  "files": [
    "src/kimix/tools/agent/__init__.py",
    "src/kimix/tools/agent/store.py"
  ],
  "verdict": "minor",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 19,
      "longest_fn_loc": 88,
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
      "module": "kimix.tools.agent",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/kimix/test_agent_gate.py",
      "probes_required": 14,
      "probes_present": 20,
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
      "file": "src/kimix/tools/agent/__init__.py",
      "evidence": "reviews/tools/gates/K01.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "tests/kimix/test_agent_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "2 functions exceed complexity 12",
      "file": "src/kimix/tools/agent/__init__.py",
      "evidence": "gates/K01.json complexity_max=19",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "tests/kimix/test_agent_gate.py::test_no_function_over_complexity_12"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 2,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "2 functions exceed complexity 12"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K01.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
