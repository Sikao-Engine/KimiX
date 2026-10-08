# C06 — read_image (ReadMediaFile) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=3 tests=2 registration=`ReadMediaFile` (kimi-cli/src/kimi_cli/tools/file/read_media.py:232) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=29 longest_fn_loc=224 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | pass | floor=85 no coverage line recorded |
| G4 behavior | pass | `kimi-cli/tests/tools/test_read_media_gate.py` required=10 present=15 |
| G5 regression | pass | pytest=pass (rc=0); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/file/read_media.py` | 673 |
| `kimi-cli/src/kimi_cli/tools/file/read_media.md` | 20 |
| `kimi-cli/src/kimi_cli/tools/file/read_media_shared.py` | 119 |

Tests in scope: `kimi-cli/tests/tools/test_read_media_file.py`, `kimi-cli/tests/tools/test_read_media_file_desc.py`

Module LOC total: **792**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `ReadMediaFile` | `kimi-cli/src/kimi_cli/tools/file/read_media.py:232` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_read_media` | 236 | 26 | 25 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `__call__` | 157 | 29 | 28 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `build_media_note` | 60 | 16 | 15 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media_shared.py` |
| `_try_mipmap_fallback` | 38 | 4 | 3 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `_validate_path` | 19 | 3 | 2 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `__init__` | 18 | 4 | 3 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `_build_image_delivery_limit_error` | 17 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `_build_image_decode_limit_error` | 8 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `build_image_delivery_limit_error` | 8 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media_shared.py` |
| `_build_full_resolution_limit_error` | 7 | 1 | 0 | 0 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `_validate_region` | 4 | 3 | 2 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `_validate_video_params` | 4 | 3 | 2 | 1 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **792** → decision: **keep (below the 1200-LOC split threshold)**
* longest function `_read_media` = 236 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py`)
* functions > 120 LOC: **2** · complexity > 12: **3**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/file/read_media.py::_read_media loc=224 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/read_media.py::_read_media complexity=26 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_media.py::__call__ loc=157 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/read_media.py::__call__ complexity=29 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_media_shared.py::build_media_note complexity=16 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `__call__` | 29 | 157 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `_read_media` | 26 | 236 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `build_media_note` | 16 | 60 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media_shared.py` |
| `_try_mipmap_fallback` | 4 | 38 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| `__init__` | 4 | 18 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_media.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 22 `ToolError` + 2 `ToolOk` return sites, 1 `ToolError`/`SkipThisTool` raises |
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

* required **10** · present **15** (`kimi-cli/tests/tools/test_read_media_gate.py`) → **pass**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C06.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C06.json` — machine-readable summary.
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
  "id": "C06",
  "tool": "read_image (ReadMediaFile)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/read_media.py",
    "kimi-cli/src/kimi_cli/tools/file/read_media.md",
    "kimi-cli/src/kimi_cli/tools/file/read_media_shared.py"
  ],
  "verdict": "minor",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 29,
      "longest_fn_loc": 224,
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
      "module": "kimi_cli.tools.file.read_media",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_read_media_gate.py",
      "probes_required": 10,
      "probes_present": 15,
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
      "file": "kimi-cli/src/kimi_cli/tools/file/read_media.py",
      "evidence": "reviews/tools/gates/C06.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_read_media_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "3 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/file/read_media.py",
      "evidence": "gates/C06.json complexity_max=29",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_read_media_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "2 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/file/read_media.py",
      "evidence": "longest = _read_media (224 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_read_media_gate.py::test_no_function_over_120_loc"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 3,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "3 functions exceed complexity 12",
      "2 functions exceed 120 LOC"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C06.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
