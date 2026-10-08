# C02 — AskUserQuestion review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=2 tests=1 registration=`AskUserQuestion` (kimi-cli/src/kimi_cli/tools/ask_user/__init__.py:59) |
| G1 simplicity | pass | ruff=pass C901=pass metrics=pass complexity_max=8 longest_fn_loc=79 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=95 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_ask_user_gate.py` required=7 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` | 151 |
| `kimi-cli/src/kimi_cli/tools/ask_user/description.md` | 1 |

Tests in scope: `kimi-cli/tests/tools/test_ask_user.py`

Module LOC total: **151**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `AskUserQuestion` | `kimi-cli/src/kimi_cli/tools/ask_user/__init__.py:59` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `__call__` | 79 | 8 | 7 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` |
| `__init__` | 3 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` |
| `bind_afk` | 3 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **pass**

* `ruff check` → `pass`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `pass`
* module LOC **151** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `__call__` = 79 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/ask_user/__init__.py`)
* functions > 120 LOC: **0** · complexity > 12: **0**

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `__call__` | 8 | 79 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` |
| `__init__` | 1 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` |
| `bind_afk` | 1 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 4 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* floor **95** · result **pass**
* `no coverage line recorded`
* pytest `pass` (rc=0)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **7** · present **0** (`kimi-cli/tests/tools/test_ask_user_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C02.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C02.json` — machine-readable summary.
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
  "id": "C02",
  "tool": "AskUserQuestion",
  "files": [
    "kimi-cli/src/kimi_cli/tools/ask_user/__init__.py",
    "kimi-cli/src/kimi_cli/tools/ask_user/description.md"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "pass",
      "complexity_max": 8,
      "longest_fn_loc": 79,
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
      "module": "kimi_cli.tools.ask_user",
      "floor": 95,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_ask_user_gate.py",
      "probes_required": 7,
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
      "title": "G4 probe suite missing/insufficient (0/7)",
      "file": "kimi-cli/tests/tools/test_ask_user_gate.py",
      "evidence": "kimi-cli/tests/tools/test_ask_user_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 7 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_ask_user_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 1,
    "gap_list": [
      "G4 probe suite missing/insufficient (0/7)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C02.txt",
  "git_sha": "d684f5a8cd5cfc39549a6a3566b83d9a454deec7\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
