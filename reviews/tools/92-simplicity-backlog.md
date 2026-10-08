# 92 — Simplicity backlog (refreshed after Phase 4)

Source: `tools/review_metrics.py` over the 87 in-scope `.py` files, plus the
per-tool `ruff` result in `reviews/tools/gates/<ID>.json`. Numbers here are
measured at the final revision, not copied from the Phase-2 baseline.

## Thresholds (plan §3 G1)

| metric | default | hard fail |
|---|---|---|
| max function LOC | 120 | 200 |
| max cyclomatic complexity | 12 | 15 |
| module LOC | 1200 | -> mandatory keep/split decision |
| max nesting depth | 5 | |

## Progress against the Phase-2 baseline

| metric | baseline | final |
|---|---|---|
| functions over complexity 12 | 90 | **76** |
| functions over 120 LOC | 29 | **27** |
| modules >= 1200 LOC | 6 | **7** |
| `E501` line-too-long | 101 | **98** |
| tools passing `ruff check` | 0 / 36 | **25 / 33** |
| tools passing all of G1 | 0 / 36 | **15 / 33** |

## Worst 30 functions by cyclomatic complexity

| complexity | LOC | function | file | tool IDs |
|---|---|---|---|---|
| 86 | 351 | `__call__` | `src/kimix/tools/file/run.py` | K06 |
| 77 | 297 | `_postprocess` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | C08 |
| 64 | 301 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/write.py` | C09 |
| 61 | 205 | `apply_hashline_edits` | `kimi-cli/src/kimi_cli/tools/file/hash_line.py` | C15 |
| 57 | 370 | `__call__` | `src/kimix/tools/file/bash/pwsh_tool.py` | K05 |
| 52 | 173 | `detect_self_kill` | `src/kimix/tools/file/bash/safety.py` | K04 |
| 51 | 325 | `__call__` | `src/kimix/tools/file/bash/bash_tool.py` | K04 |
| 51 | 233 | `_backup_grep_search` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | C08 |
| 38 | 239 | `execute` | `kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` | C10 |
| 37 | 199 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/glob.py` | C07 |
| 35 | 60 | `list_directory` | `kimi-cli/src/kimi_cli/tools/file/read_archive.py` | C05 |
| 34 | 78 | `extract_key_argument` | `kimi-cli/src/kimi_cli/tools/__init__.py` | C00 |
| 32 | 273 | `_run_process_bg` | `src/kimix/tools/common.py` | K00 |
| 31 | 158 | `_backup_grep_impl` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | C08 |
| 30 | 151 | `_rich_call` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | C08 |
| 29 | 158 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/read_media.py` | C06 |
| 29 | 154 | `_get_output` | `src/kimix/tools/background/__init__.py` | K08 |
| 28 | 220 | `_read_single_file` | `kimi-cli/src/kimi_cli/tools/file/read.py` | C05 |
| 28 | 210 | `_execute_code` | `src/kimix/tools/py/__init__.py` | K07 |
| 28 | 136 | `_read_rich_format` | `kimi-cli/src/kimi_cli/tools/file/read.py` | C05 |
| 28 | 131 | `_resolve_all_conflicts` | `kimi-cli/src/kimi_cli/tools/file/write.py` | C09 |
| 27 | 166 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | C08 |
| 26 | 237 | `_read_media` | `kimi-cli/src/kimi_cli/tools/file/read_media.py` | C06 |
| 26 | 164 | `__call__` | `kimi-cli/src/kimi_cli/tools/file/read.py` | C05 |
| 26 | 105 | `_build_rg_args` | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | C08 |
| 26 | 64 | `extract_leading_header_comment_text` | `kimi-cli/src/kimi_cli/tools/file/auto_generated.py` | C09 |
| 24 | 70 | `_gitignore_match` | `kimi-cli/src/kimi_cli/tools/file/glob.py` | C07 |
| 23 | 127 | `scan_conflict_lines` | `kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` | C09 |
| 23 | 117 | `execute` | `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` | C10 |
| 23 | 80 | `_split_shell_segments` | `src/kimix/tools/common.py` | K00 |

## Modules >= 1200 LOC (mandatory keep/split decision)

| LOC | module | tool IDs | decision |
|---|---|---|---|
| 2458 | `kimi-cli/src/kimi_cli/tools/todo/__init__.py` | C03 | split |
| 2357 | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` | C08 | split |
| 2340 | `src/kimix/tools/common.py` | K00 | split |
| 2077 | `kimi-cli/src/kimi_cli/tools/web/providers.py` | C11, C12, C13 | split |
| 1824 | `kimi-cli/src/kimi_cli/tools/file/read.py` | C05 | split |
| 1462 | `src/kimix/tools/file/bash/bash_tool.py` | K04 | split |
| 1221 | `src/kimix/tools/agent/__init__.py` | K01, K02, K03 | split |

> `src/kimix/tools/agent/__init__.py` crossed 1200 as a *result* of the G1
> refactors (extracted helpers + docstrings). That is the fix growing the module
> for a net complexity reduction; the split decision now applies to it too.

## Cheapest remaining wins (one function each, `ruff` already green)

| tool | function | cx | file |
|---|---|---|---|
| C11 | `extract` (Parallel provider adapter) | 19 | `kimi-cli/src/kimi_cli/tools/web/providers.py` |
| C12 | `is_safe_url` | 19 | `kimi-cli/src/kimi_cli/tools/web/url_safety.py` |
| C13 | `extract` + `is_safe_url` (shared with C11/C12) | 22 / 19 | `web/extract.py`, `web/url_safety.py` |

`C11` is worth doing first: `providers.py` is in C11's, C12's *and* C13's scope,
so one refactor moves three tools' metrics check at once.

## `E501`

The kimi-cli ruff config does not ignore `E501` (the root config does), so the
remaining 98 long lines are all under
`kimi-cli/src/kimi_cli/tools`. Most contain long string literals or f-strings,
where a mechanical wrap risks changing model-facing text, so they are left for a
deliberate pass or an owner-approved `line-length`/per-file ignore.
