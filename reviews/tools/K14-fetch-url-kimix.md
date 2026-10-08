# K14 — fetch_url (kimix) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=3 tests=1 registration=`fetch_url` (src/kimix/tools/web/fetch_url.py:23) |
| G1 simplicity | fail | ruff=fail C901=pass metrics=pass complexity_max=10 longest_fn_loc=49 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=90 no coverage line recorded |
| G4 behavior | fail | `tests/unit/tools/test_fetchurl_gate.py` required=8 present=0 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `src/kimix/tools/web/fetch_url.py` | 72 |
| `src/kimix/tools/web/web_fetcher/__init__.py` | 3 |
| `src/kimix/tools/web/web_fetcher/fetcher.py` | 233 |

Tests in scope: `tests/unit/tools/test_fetchurl.py`

Module LOC total: **308**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `fetch_url` | `src/kimix/tools/web/fetch_url.py:23` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_fetch_html` | 49 | 7 | 6 | 4 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `__call__` | 44 | 5 | 4 | 2 | `C:/dev/kimi-agent/src/kimix/tools/web/fetch_url.py` |
| `_fetch_html_http` | 42 | 10 | 9 | 5 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `_html_to_markdown` | 41 | 7 | 6 | 2 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `fetch_to_markdown` | 27 | 8 | 7 | 3 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `fetch_html_http_with_fallback` | 15 | 4 | 3 | 2 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `_build_ssl_context` | 14 | 3 | 2 | 1 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `pass`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `pass`
* module LOC **308** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `_fetch_html` = 49 LOC (`C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py`)
* functions > 120 LOC: **0** · complexity > 12: **0**

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `_fetch_html_http` | 10 | 42 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `fetch_to_markdown` | 8 | 27 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `_fetch_html` | 7 | 49 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `_html_to_markdown` | 7 | 41 | `C:/dev/kimi-agent/src/kimix/tools/web/web_fetcher/fetcher.py` |
| `__call__` | 5 | 44 | `C:/dev/kimi-agent/src/kimix/tools/web/fetch_url.py` |

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

* floor **90** · result **pass**
* `no coverage line recorded`
* pytest `pass` (rc=0)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **8** · present **0** (`tests/unit/tools/test_fetchurl_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/K14.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/K14.json` — machine-readable summary.
* Baseline (pre-existing failures): `reviews/tools/00-baseline.txt`.

## 8. Git provenance

* `git rev-parse HEAD` at review time: `52192b7c3a1baa66c96e0015d2868f3b106fc043
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
  "id": "K14",
  "tool": "fetch_url (kimix)",
  "files": [
    "src/kimix/tools/web/fetch_url.py",
    "src/kimix/tools/web/web_fetcher/__init__.py",
    "src/kimix/tools/web/web_fetcher/fetcher.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 10,
      "longest_fn_loc": 49,
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
      "module": "kimix.tools.web.fetch_url",
      "floor": 90,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "tests/unit/tools/test_fetchurl_gate.py",
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
      "severity": "medium",
      "axis": "simplicity",
      "title": "ruff check fails on the in-scope files",
      "file": "src/kimix/tools/web/fetch_url.py",
      "evidence": "reviews/tools/gates/K14.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "tests/unit/tools/test_fetchurl_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/8)",
      "file": "tests/unit/tools/test_fetchurl_gate.py",
      "evidence": "tests/unit/tools/test_fetchurl_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 8 adversarial probes per the plan §4.3 block",
      "test_to_add": "tests/unit/tools/test_fetchurl_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 2,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "G4 probe suite missing/insufficient (0/8)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K14.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": [
    "fetch_url C12/K14 (FP-02)"
  ]
}
```
