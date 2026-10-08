# C05 — read (ReadFile) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | files=10 tests=10 registration=`ReadFile` (kimi-cli/src/kimi_cli/tools/file/read.py:449) |
| G1 simplicity | fail | ruff=fail C901=fail metrics=fail complexity_max=35 longest_fn_loc=220 |
| G2 risk | not-independently-verified | item rows in §4; X02/X03 sweeps own the async/security rows |
| G3 coverage | fail | floor=85 no coverage line recorded |
| G4 behavior | fail | `kimi-cli/tests/tools/test_read_gate.py` required=14 present=0 |
| G5 regression | fail | pytest=fail (rc=1); see 00-baseline.txt |
| G6 independent rerun | not-run | orchestrator re-run pending |

## 1. Scope

| source | LOC |
|---|---|
| `kimi-cli/src/kimi_cli/tools/file/read.py` | 1826 |
| `kimi-cli/src/kimi_cli/tools/file/read.md` | 10 |
| `kimi-cli/src/kimi_cli/tools/file/read_extract.py` | 330 |
| `kimi-cli/src/kimi_cli/tools/file/read_archive.py` | 341 |
| `kimi-cli/src/kimi_cli/tools/file/read_sqlite.py` | 500 |
| `kimi-cli/src/kimi_cli/tools/file/read_pdf_pages.py` | 186 |
| `kimi-cli/src/kimi_cli/tools/file/read_profiles.py` | 429 |
| `kimi-cli/src/kimi_cli/tools/file/read_markit.py` | 267 |
| `kimi-cli/src/kimi_cli/tools/file/read_media_shared.py` | 119 |
| `kimi-cli/src/kimi_cli/tools/file/utils.py` | 316 |

Tests in scope: `kimi-cli/tests/tools/test_read_file.py`, `kimi-cli/tests/tools/test_read_extract.py`, `kimi-cli/tests/tools/test_read_conflict.py`, `tests/unit/tools/test_read_archive.py`, `tests/unit/tools/test_read_markit.py`, `tests/unit/tools/test_read_pdf_pages.py`, `tests/unit/tools/test_read_profiles.py`, `tests/unit/tools/test_read_rich_dispatch.py`, `tests/unit/tools/test_read_sqlite.py`, `tests/unit/tools/test_read_window.py`

Module LOC total: **4314**

## 2. Behaviour map

| entry point | file:line | bases | params model(s) | __call__ |
|---|---|---|---|---|
| `ReadFile` | `kimi-cli/src/kimi_cli/tools/file/read.py:449` | CallableTool2 | - | True |

Largest functions (by LOC):

| function | LOC | complexity | branches | max nesting | file |
|---|---|---|---|---|---|
| `_read_single_file` | 220 | 28 | 27 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `__call__` | 164 | 26 | 25 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `_read_rich_format` | 136 | 28 | 27 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `execute_sqlite_read` | 104 | 17 | 16 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_sqlite.py` |
| `_expand_glob_path` | 85 | 15 | 14 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `_render_result` | 82 | 18 | 17 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `_read_archive` | 76 | 7 | 6 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `_render_tail` | 73 | 10 | 9 | 4 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `_convert_docx_to_markdown` | 69 | 19 | 18 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_markit.py` |
| `render_cpu_profile` | 67 | 14 | 13 | 3 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_profiles.py` |
| `render_pdf_page` | 66 | 8 | 7 | 2 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_pdf_pages.py` |
| `list_directory` | 62 | 35 | 34 | 5 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_archive.py` |

## 3. S1 Simplicity (G1 detail)

verdict: **fail**

* `ruff check` → `fail`
* `ruff check --select C901 --max-complexity=12` → `fail`
* `review_metrics --max-fn-loc 120 --max-complexity 12` → `fail`
* module LOC **4314** → decision: **split (plan)**
* longest function `_read_single_file` = 220 LOC (`C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py`)
* functions > 120 LOC: **3** · complexity > 12: **17**

Threshold breaches recorded:

| breach |
|---|
| `kimi-cli/src/kimi_cli/tools/file/read.py::_validate complexity=13 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::_expand_glob_path complexity=15 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::__call__ loc=164 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::__call__ complexity=26 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::_read_single_file loc=220 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::_read_single_file complexity=28 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::_read_rich_format loc=136 > 120` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::_read_rich_format complexity=28 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read.py::_render_result complexity=18 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_archive.py::list_directory complexity=35 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_archive.py::read_file complexity=17 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_sqlite.py::resolve_sqlite_selector complexity=13 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_sqlite.py::execute_sqlite_read complexity=17 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_profiles.py::_compute_self_times complexity=21 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_profiles.py::render_cpu_profile complexity=14 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_profiles.py::_parse_sample_profile complexity=19 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_markit.py::_convert_docx_to_markdown complexity=19 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/read_media_shared.py::build_media_note complexity=16 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/utils.py::sniff_media_from_magic complexity=19 > 12` |
| `kimi-cli/src/kimi_cli/tools/file/utils.py::detect_file_type complexity=16 > 12` |

Top-5 by cyclomatic complexity:

| function | complexity | LOC | file |
|---|---|---|---|
| `list_directory` | 35 | 62 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_archive.py` |
| `_read_single_file` | 28 | 220 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `_read_rich_format` | 28 | 136 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `__call__` | 26 | 164 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read.py` |
| `_compute_self_times` | 21 | 44 | `C:/dev/kimi-agent/kimi-cli/src/kimi_cli/tools/file/read_profiles.py` |

## 4. S2 Potential issues (G2 detail)

| # | item | status | evidence |
|---|---|---|---|
| a | error contract | auto-scanned | 44 `ToolError` + 10 `ToolOk` return sites, 0 `ToolError`/`SkipThisTool` raises |
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

* floor **85** · result **fail**
* `no coverage line recorded`
* pytest `fail` (rc=1)
* Behaviours mapped / covered / gaps: **not counted** — the per-behaviour map needs a hand
  audit this automated pass does not perform. Recorded as a gap, never as "covered".

## 6. G4 Behavioural probes

* required **14** · present **0** (`kimi-cli/tests/tools/test_read_gate.py`) → **fail**
* The required probe topics are enumerated verbatim in the plan §4.3 block for this ID
  (reproduced under `.kimix_cache/tmp_30108/p2_prompts/`).

## 7. G5/G6 Evidence

* `reviews/tools/gates/C05.txt` — raw output; line 1 = commit SHA, line 2 = `git status --porcelain`.
* `reviews/tools/gates/C05.json` — machine-readable summary.
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
  "id": "C05",
  "tool": "read (ReadFile)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/read.py",
    "kimi-cli/src/kimi_cli/tools/file/read.md",
    "kimi-cli/src/kimi_cli/tools/file/read_extract.py",
    "kimi-cli/src/kimi_cli/tools/file/read_archive.py",
    "kimi-cli/src/kimi_cli/tools/file/read_sqlite.py",
    "kimi-cli/src/kimi_cli/tools/file/read_pdf_pages.py",
    "kimi-cli/src/kimi_cli/tools/file/read_profiles.py",
    "kimi-cli/src/kimi_cli/tools/file/read_markit.py",
    "kimi-cli/src/kimi_cli/tools/file/read_media_shared.py",
    "kimi-cli/src/kimi_cli/tools/file/utils.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {
      "ruff": "fail",
      "complexity_max": 35,
      "longest_fn_loc": 220,
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
      "module": "kimi_cli.tools.file.read",
      "floor": 85,
      "actual": 0.0,
      "waived_lines": 0,
      "waiver_budget": 25
    },
    "G4_behavior": {
      "probe_file": "kimi-cli/tests/tools/test_read_gate.py",
      "probes_required": 14,
      "probes_present": 0,
      "result": "fail"
    },
    "G5_regression": {
      "root_suite": "blocked (missing tests/bash/cases.json)",
      "cli_suite": "fail",
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
      "file": "kimi-cli/src/kimi_cli/tools/file/read.py",
      "evidence": "reviews/tools/gates/C05.txt §ruff (rc=1)",
      "impact": "G1 lint gate is red; import-order/style/naming violations accumulate",
      "fix": "uv run ruff check --fix <files>, then hand-fix the remainder",
      "test_to_add": "kimi-cli/tests/tools/test_read_gate.py::test_ruff_clean_in_scope"
    },
    {
      "severity": "medium",
      "axis": "simplicity",
      "title": "17 functions exceed complexity 12",
      "file": "kimi-cli/src/kimi_cli/tools/file/read.py",
      "evidence": "gates/C05.json complexity_max=35",
      "impact": "hard G1 breach for the >15 offenders; branches are untestable",
      "fix": "extract cohesive helpers until every function is <= 12",
      "test_to_add": "kimi-cli/tests/tools/test_read_gate.py::test_no_function_over_complexity_12"
    },
    {
      "severity": "low",
      "axis": "simplicity",
      "title": "3 functions exceed 120 LOC",
      "file": "kimi-cli/src/kimi_cli/tools/file/read.py",
      "evidence": "longest = _read_single_file (220 LOC)",
      "impact": "long functions resist review and coverage",
      "fix": "split along the natural sub-steps",
      "test_to_add": "kimi-cli/tests/tools/test_read_gate.py::test_no_function_over_120_loc"
    },
    {
      "severity": "high",
      "axis": "coverage",
      "title": "G4 probe suite missing/insufficient (0/14)",
      "file": "kimi-cli/tests/tools/test_read_gate.py",
      "evidence": "kimi-cli/tests/tools/test_read_gate.py contains 0 test functions",
      "impact": "no binary behavioural evidence for this tool",
      "fix": "author >= 14 adversarial probes per the plan §4.3 block",
      "test_to_add": "kimi-cli/tests/tools/test_read_gate.py"
    }
  ],
  "coverage": {
    "behaviors_mapped": 0,
    "covered": 0,
    "gaps": 4,
    "gap_list": [
      "ruff check fails on the in-scope files",
      "17 functions exceed complexity 12",
      "3 functions exceed 120 LOC",
      "G4 probe suite missing/insufficient (0/14)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/C05.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043\n[elapsed 0.0s, rc=0]",
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": []
}
```
