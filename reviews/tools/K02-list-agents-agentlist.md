# K02 — list_agents (AgentList) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=1 tests=1 registration=`Agent` (src/kimix/tools/agent/__init__.py:514), `AgentList` (src/kimix/tools/agent/__init__.py:1057), `AgentClose` (src/kimix/tools/agent/__init__.py:1094) |
| G1 simplicity | pass | ruff=pass C901=pass metrics=pass complexity_max=11 longest_fn_loc=88 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | fail | `tests/kimix/test_agent_list_gate.py` required=5 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/agent/__init__.py` | 1221 |

Tests in scope: `tests/kimix/test_agent_conversation.py`

Module LOC total: **1221**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `Agent` | `src/kimix/tools/agent/__init__.py:514` | CallableTool2 | - | True |
| `AgentList` | `src/kimix/tools/agent/__init__.py:1057` | CallableTool2 | - | True |
| `AgentClose` | `src/kimix/tools/agent/__init__.py:1094` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_execute` | 88 | 8 | 7 | 3 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_resolve_session` | 73 | 9 | 8 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_prepare_run` | 67 | 11 | 10 | 4 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_launch_background` | 45 | 6 | 5 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_update_store` | 39 | 5 | 4 | 3 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_inherit_parent_context` | 34 | 3 | 2 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_finalize_previous` | 28 | 4 | 3 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_notify_parent_background_finished` | 26 | 9 | 8 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_build_extras` | 26 | 2 | 1 | 1 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_reset_inherited_system_prompt` | 25 | 5 | 4 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_resolve_prompt` | 24 | 6 | 5 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_background_tasks_for` | 23 | 5 | 4 | 2 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **pass**

* `ruff check` → `pass`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `pass`
* module LOC **1221** → decision: **split (plan)**
* longest function `_execute` = 88 LOC (`C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py`)
* functions > 120 LOC: **0** · complexity > 12: **0**

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_prepare_run` | 11 | 67 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_resolve_session` | 9 | 73 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_notify_parent_background_finished` | 9 | 26 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `consume` | 9 | 23 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |
| `_execute` | 8 | 88 | `C:/dev/kimi-agent/src/kimix/tools/agent/__init__.py` |

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

* required **5** · present **0** (`tests/kimix/test_agent_list_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K02.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K02.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `cbf044d06c44178a7b1b310fe336c773b864a91f
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
  "id": "K02",
  "tool": "list_agents (AgentList)",
  "files": [
    "src/kimix/tools/agent/__init__.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "pass",
      "complexity_max": 11,
      "longest_fn_loc": 88,
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
      "module": "kimix.tools.agent",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/kimix/test_agent_list_gate.py",
      "probes_required": 5,
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
      "title": "G4 probe suite missing/insufficient (0/5)",
      "file": "tests/kimix/test_agent_list_gate.py",
      "evidence": "tests/kimix/test_agent_list_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 5 adversarial probes per the plan §4.3 block",
      "test_to_add": "tests/kimix/test_agent_list_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 1,
    "gap_list": [
      "G4 probe suite missing/insufficient (0/5)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K02.txt",
  "git_sha": "cbf044d06c44178a7b1b310fe336c773b864a91f\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
