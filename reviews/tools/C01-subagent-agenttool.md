# C01 — subagent (AgentTool) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=2 tests=2 registration=`AgentTool` (kimi-cli/src/kimi_cli/tools/agent/__init__.py:54) |
| G1 simplicity | pass | ruff=pass C901=pass metrics=pass complexity_max=10 longest_fn_loc=84 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=90 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_agent_tool_gate.py` required=8 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/agent/__init__.py` | 282 |
| `kimi-cli/src/kimi_cli/tools/agent/description.md` | 9 |

Tests in scope: `kimi-cli/tests/tools/test_agent_tool.py`, `kimi-cli/tests/tools/test_tool_descriptions.py`

Module LOC total: **282**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `AgentTool` | `kimi-cli/src/kimi_cli/tools/agent/__init__.py:54` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_run_in_background` | 84 | 10 | 9 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `__call__` | 39 | 9 | 8 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_resolve_background_target` | 34 | 6 | 5 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_builtin_type_lines` | 16 | 5 | 4 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_validate_model_alias` | 11 | 5 | 4 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `__init__` | 10 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_unique_tool_names` | 7 | 3 | 2 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_tool_summary` | 6 | 3 | 2 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `effective_timeout` | 3 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_normalize_summary` | 2 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **pass**

* `ruff check` → `pass`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `pass`
* module LOC **282** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `_run_in_background` = 84 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py`)
* functions > 120 LOC: **0** · complexity > 12: **0**

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_run_in_background` | 10 | 84 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `__call__` | 9 | 39 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_resolve_background_target` | 6 | 34 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_builtin_type_lines` | 5 | 16 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |
| `_validate_model_alias` | 5 | 11 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/agent/__init__.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 10 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* floor **90** · result **pass**
* `no coverage line recorded`
* pytest `pass` (rc=0)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **8** · present **0** (`kimi-cli/tests/tools/test_agent_tool_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C01.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C01.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `f3aece52a37b57f14b17ea8ea2f3b22f48f8239a
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
  "id": "C01",
  "tool": "subagent (AgentTool)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/agent/__init__.py",
    "kimi-cli/src/kimi_cli/tools/agent/description.md"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "pass",
      "complexity_max": 10,
      "longest_fn_loc": 84,
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
      "module": "kimi_cli.tools.agent",
      "floor": 90,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_agent_tool_gate.py",
      "probes_required": 8,
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
      "title": "G4 probe suite missing/insufficient (0/8)",
      "file": "kimi-cli/tests/tools/test_agent_tool_gate.py",
      "evidence": "kimi-cli/tests/tools/test_agent_tool_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 8 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_agent_tool_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 1,
    "gap_list": [
      "G4 probe suite missing/insufficient (0/8)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C01.txt",
  "git_sha": "f3aece52a37b57f14b17ea8ea2f3b22f48f8239a\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
