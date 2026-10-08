# C12 — fetch_url (kimi_cli) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=5 tests=2 registration=`fetch_url` (kimi-cli/src/kimi_cli/tools/web/fetch.py:101) |
| G1 simplicity | fail | ruff=pass C901=fail metrics=fail complexity_max=19 longest_fn_loc=165 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=90 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_fetch_url_gate.py` required=11 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/web/fetch.py` | 333 |
| `kimi-cli/src/kimi_cli/tools/web/fetch.md` | 1 |
| `kimi-cli/src/kimi_cli/tools/web/content.py` | 186 |
| `kimi-cli/src/kimi_cli/tools/web/url_safety.py` | 389 |
| `kimi-cli/src/kimi_cli/tools/web/providers.py` | 2077 |

Tests in scope: `kimi-cli/tests/tools/test_fetch_url.py`, `kimi-cli/tests/tools/test_url_safety.py`

Module LOC total: **2985**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `fetch_url` | `kimi-cli/src/kimi_cli/tools/web/fetch.py:101` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `search` | 165 | 11 | 10 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `extract` | 124 | 14 | 13 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `fetch_with_http_get` | 116 | 11 | 10 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/fetch.py` |
| `_fetch_with_service` | 95 | 10 | 9 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/fetch.py` |
| `extract` | 94 | 12 | 11 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `is_safe_url` | 93 | 19 | 18 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/url_safety.py` |
| `extract` | 88 | 19 | 18 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `search` | 73 | 9 | 8 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `extract` | 69 | 5 | 4 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `extract` | 67 | 11 | 10 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `_resolve` | 65 | 13 | 12 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `truncate_with_footer` | 57 | 5 | 4 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/content.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `pass`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **2985** → decision: **split (plan)**
* longest function `search` = 165 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py`)
* functions > 120 LOC: **2** · complexity > 12: **7**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/web/url_safety.py::_is_blocked_ip complexity=15 > 12` |
| `kimi-cli/src/kimi_cli/tools/web/url_safety.py::is_safe_url complexity=19 > 12` |
| `kimi-cli/src/kimi_cli/tools/web/providers.py::_resolve complexity=13 > 12` |
| `kimi-cli/src/kimi_cli/tools/web/providers.py::_extract_web_search_results complexity=13 > 12` |
| `kimi-cli/src/kimi_cli/tools/web/providers.py::search loc=165 > 120` |
| `kimi-cli/src/kimi_cli/tools/web/providers.py::extract loc=124 > 120` |
| `kimi-cli/src/kimi_cli/tools/web/providers.py::extract complexity=14 > 12` |
| `kimi-cli/src/kimi_cli/tools/web/providers.py::extract complexity=19 > 12` |
| `kimi-cli/src/kimi_cli/tools/web/providers.py::_collect_output_text complexity=14 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `is_safe_url` | 19 | 93 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/url_safety.py` |
| `extract` | 19 | 88 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `_is_blocked_ip` | 15 | 21 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/url_safety.py` |
| `extract` | 14 | 124 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |
| `_collect_output_text` | 14 | 28 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/web/providers.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 0 `ToolError` + 0 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* required **11** · present **0** (`kimi-cli/tests/tools/test_fetch_url_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C12.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C12.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `d684f5a8cd5cfc39549a6a3566b83d9a454deec7
[elapsed 0.0s, rc=0]`
* branch `review/tools-hardgate` · base tag `tools-review/p0-base`
* the commit/tag for this unit is assigned by the orchestrator on acceptance

## 9. Duplication / native-parity / policy notes

* `fetch_url` (C12 vs K14) is a known duplicate tracked as FP-02.
* `subagent` (C01/K01) is waived in `tools/gate_dup_allowlist.txt` (intentionally distinct defaults).
* Cross-cutting sweeps: `reviews/tools/x01..x06*.md`.

## 10. Verdict + JSON

verdict: **major**

```json
{
  "id": "C12",
  "tool": "fetch_url (kimi_cli)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/web/fetch.py",
    "kimi-cli/src/kimi_cli/tools/web/fetch.md",
    "kimi-cli/src/kimi_cli/tools/web/content.py",
    "kimi-cli/src/kimi_cli/tools/web/url_safety.py",
    "kimi-cli/src/kimi_cli/tools/web/providers.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "pass",
      "complexity_max": 19,
      "longest_fn_loc": 165,
      "loc_regression": "none",
      "vulture_hits": 0,
      "vulture_waived": 0
    },
    "G2_risk": {
      "items_answered": 13,
      "open_high": 1,
      "open_medium": 1,
      "waived_medium": 0
    },
    "G3_coverage": {
      "module": "kimi_cli.tools.web.fetch",
      "floor": 90,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_fetch_url_gate.py",
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
      "title": "7 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/web/fetch.py",
      "evidence": "gates/C12.json complexity_max=19",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_fetch_url_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "2 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/web/fetch.py",
      "evidence": "longest = search (165 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_fetch_url_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/11)",
      "file": "kimi-cli/tests/tools/test_fetch_url_gate.py",
      "evidence": "kimi-cli/tests/tools/test_fetch_url_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 11 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_fetch_url_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 3,
    "gap_list": [
      "7 functions exceed complexity 12",
      "2 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/11)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C12.txt",
  "git_sha": "d684f5a8cd5cfc39549a6a3566b83d9a454deec7\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": [
    "fetch_url C12/K14 (FP-02)"
  ]
}
```
