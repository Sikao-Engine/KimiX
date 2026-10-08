# 92 — Simplicity backlog

Source: `reviews/tools/00-metrics-baseline.json` (frozen at `tools-review/p0-base`),
plus the per-tool `ruff` result recorded in `reviews/tools/gates/<ID>.json`.

## Thresholds (plan §3 G1)

| metric | default | hard fail |
|---|---|---|
| max function LOC | 120 | 200 |
| max cyclomatic complexity | 12 | 15 |
| module LOC | 1200 | -> mandatory keep/split decision |
| max nesting depth | 5 | |

## Headline

* **90 functions exceed complexity 12** (hard fail > 15: **63**).
* **29 functions exceed 120 LOC** (hard fail > 200: **12**).
* **every** in-scope module fails `uv run ruff check` (see `gates/<ID>.txt` §ruff).
* total: 84 files, 36718 LOC, 1153 functions.

## Worst 30 functions by cyclomatic complexity

| complexity | LOC | function | file |
|---|---|---|---|
| 92 | 361 | `__call__` | `src/kimix/tools/file/run.py` |
| 77 | 297 | `_postprocess` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| 64 | 297 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/write.py` |
| 61 | 205 | `apply_hashline_edits` | `kimi-cli/src/kimi_cli/tools/file/hash_line.py` |
| 57 | 370 | `__call__` | `src/kimix/tools/file/bash/pwsh_tool.py` |
| 52 | 173 | `detect_self_kill` | `src/kimix/tools/file/bash/safety.py` |
| 51 | 325 | `__call__` | `src/kimix/tools/file/bash/bash_tool.py` |
| 51 | 233 | `_backup_grep_search` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| 38 | 239 | `execute` | `kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` |
| 37 | 199 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/glob.py` |
| 35 | 279 | `_run_process_bg` | `src/kimix/tools/common.py` |
| 35 | 62 | `list_directory` | `kimi-cli/src/kimi_cli/tools/file/read_archive.py` |
| 34 | 74 | `extract_key_argument` | `kimi-cli/src/kimi_cli/tools/__init__.py` |
| 31 | 158 | `_backup_grep_impl` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| 30 | 151 | `_rich_call` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| 29 | 157 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| 29 | 152 | `_get_output` | `src/kimix/tools/background/__init__.py` |
| 28 | 220 | `_read_single_file` | `kimi-cli/src/kimi_cli/tools/file/read.py` |
| 28 | 210 | `_execute_code` | `src/kimix/tools/py/__init__.py` |
| 28 | 136 | `_read_rich_format` | `kimi-cli/src/kimi_cli/tools/file/read.py` |
| 28 | 131 | `_resolve_all_conflicts` | `kimi-cli/src/kimi_cli/tools/file/write.py` |
| 27 | 166 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| 26 | 224 | `_read_media` | `kimi-cli/src/kimi_cli/tools/file/read_media.py` |
| 26 | 164 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/read.py` |
| 26 | 105 | `_build_rg_args` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` |
| 26 | 66 | `extract_leading_header_comment_text` | `kimi-cli/src/kimi_cli/tools/file/auto_generated.py` |
| 24 | 70 | `_gitignore_match` | `kimi-cli/src/kimi_cli/tools/file/glob.py` |
| 23 | 127 | `scan_conflict_lines` | `kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` |
| 23 | 117 | `execute` | `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` |
| 23 | 80 | `_split_shell_segments` | `src/kimix/tools/common.py` |

## Modules >= 1200 LOC (mandatory keep/split decision)

| LOC | module | decision |
|---|---|---|
| 2452 | `kimi-cli/src/kimi_cli/tools/todo/__init__.py` | **split (plan)** — no rationale recorded for keeping |
| 2355 | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | **split (plan)** — no rationale recorded for keeping |
| 2342 | `src/kimix/tools/common.py` | **split (plan)** — no rationale recorded for keeping |
| 2077 | `kimi-cli/src/kimi_cli/tools/web/providers.py` | **split (plan)** — no rationale recorded for keeping |
| 1826 | `kimi-cli/src/kimi_cli/tools/file/read.py` | **split (plan)** — no rationale recorded for keeping |
| 1461 | `src/kimix/tools/file/bash/bash_tool.py` | **split (plan)** — no rationale recorded for keeping |

## ruff categories across both trees

| count | rule | tree |
|---|---|---|
| 101 | E501 line-too-long | kimi-cli |
| 65 | I001 unsorted-imports | both |
| 35 | I001 unsorted-imports | kimi-cli |
| 30 | I001 unsorted-imports | kimix |
| 14 | W293 blank-line-with-whitespace | kimix |
| 12 | UP* pyupgrade | both |
| 11 | F841 unused-variable | both |
| 11 | F401 unused-import | both |
| 12 | SIM* flake8-simplify | both |
| 9 | E402 module-import-not-at-top-of-file | kimix |
| 9 | B* flake8-bugbear | both |
| 4 | E741 ambiguous-variable-name | kimi-cli |
| 3 | N801 invalid-class-name | kimix |
| 4 | F541 f-string-missing-placeholders | both |

*(raw counts from `uv run ruff check --statistics` on both trees; the F821/F822/F811
subset is fixed by FP-08.)*
