# Built-in tools review — 00 inventory (frozen)

Phase 0 freeze of the reviewed surface. Branch `review/tools-hardgate`.

## Roots

* `kimi-cli/src/kimi_cli/tools/**` — 54 `.py`, 10 `.md`, 23746 LOC
* `src/kimix/tools/**` — 30 `.py`, 1 `.md`, 12972 LOC

> Note: the plan §1 mentions `_ripgrep_common.py` (C08) and a second
> `kimix/tools` `.md`; neither exists in the frozen tree (recorded as a plan/tree drift finding).

## Per-unit inventory

| ID | tool | source (LOC) | tests present | tests missing |
|---|---|---|---|---|
| C00 | toolset plumbing & shared helpers | `kimi-cli/src/kimi_cli/tools/__init__.py` (166)<br>`kimi-cli/src/kimi_cli/tools/utils.py` (298)<br>`kimi-cli/src/kimi_cli/tools/display.py` (49)<br>`kimi-cli/src/kimi_cli/tools/reason.py` (103) | `kimi-cli/tests/tools/test_extract_key_argument.py`<br>`kimi-cli/tests/tools/test_utils.py`<br>`kimi-cli/tests/tools/test_reason.py`<br>`kimi-cli/tests/core/test_toolset.py`<br>`kimi-cli/tests/core/test_toolset_todo_redirects.py` |  |
| C01 | subagent (AgentTool) | `kimi-cli/src/kimi_cli/tools/agent/__init__.py` (269)<br>`kimi-cli/src/kimi_cli/tools/agent/description.md` (9) | `kimi-cli/tests/tools/test_agent_tool.py`<br>`kimi-cli/tests/tools/test_tool_descriptions.py` |  |
| C02 | AskUserQuestion | `kimi-cli/src/kimi_cli/tools/ask_user/__init__.py` (151)<br>`kimi-cli/src/kimi_cli/tools/ask_user/description.md` (1) | `kimi-cli/tests/tools/test_ask_user.py` |  |
| C03 | todo_list (TodoList) | `kimi-cli/src/kimi_cli/tools/todo/__init__.py` (2452)<br>`kimi-cli/src/kimi_cli/tools/display.py` (49) | `kimi-cli/tests/tools/test_todo.py`<br>`kimi-cli/tests/tools/test_todo_edits.py`<br>`kimi-cli/tests/tools/test_todo_fixes.py`<br>`kimi-cli/tests/tools/test_todo_list_compat.py`<br>`kimi-cli/tests/tools/test_todo_list_conflict.py`<br>`kimi-cli/tests/tools/test_todo_list_contract.py`<br>`kimi-cli/tests/tools/test_todo_stack.py`<br>`kimi-cli/tests/tools/test_todo_title_diagnosis.py` |  |
| C04 | retrieve | `kimi-cli/src/kimi_cli/tools/memory/__init__.py` (131) | `kimi-cli/tests/tools/test_memory_retrieve.py` |  |
| C05 | read (ReadFile) | `kimi-cli/src/kimi_cli/tools/file/read.py` (1826)<br>`kimi-cli/src/kimi_cli/tools/file/read.md` (10)<br>`kimi-cli/src/kimi_cli/tools/file/read_extract.py` (330)<br>`kimi-cli/src/kimi_cli/tools/file/read_archive.py` (341)<br>`kimi-cli/src/kimi_cli/tools/file/read_sqlite.py` (500)<br>`kimi-cli/src/kimi_cli/tools/file/read_pdf_pages.py` (186)<br>`kimi-cli/src/kimi_cli/tools/file/read_profiles.py` (429)<br>`kimi-cli/src/kimi_cli/tools/file/read_markit.py` (267)<br>`kimi-cli/src/kimi_cli/tools/file/read_media_shared.py` (119)<br>`kimi-cli/src/kimi_cli/tools/file/utils.py` (316) | `kimi-cli/tests/tools/test_read_file.py`<br>`kimi-cli/tests/tools/test_read_extract.py`<br>`kimi-cli/tests/tools/test_read_conflict.py`<br>`tests/unit/tools/test_read_archive.py`<br>`tests/unit/tools/test_read_markit.py`<br>`tests/unit/tools/test_read_pdf_pages.py`<br>`tests/unit/tools/test_read_profiles.py`<br>`tests/unit/tools/test_read_rich_dispatch.py`<br>`tests/unit/tools/test_read_sqlite.py`<br>`tests/unit/tools/test_read_window.py` |  |
| C06 | read_image (ReadMediaFile) | `kimi-cli/src/kimi_cli/tools/file/read_media.py` (642)<br>`kimi-cli/src/kimi_cli/tools/file/read_media.md` (20)<br>`kimi-cli/src/kimi_cli/tools/file/read_media_shared.py` (119) | `kimi-cli/tests/tools/test_read_media_file.py`<br>`kimi-cli/tests/tools/test_read_media_file_desc.py` |  |
| C07 | glob (Glob) | `kimi-cli/src/kimi_cli/tools/file/glob.py` (707)<br>`kimi-cli/src/kimi_cli/tools/file/glob.md` (3)<br>`kimi-cli/src/kimi_cli/tools/file/output_utils.py` (239)<br>`kimi-cli/src/kimi_cli/tools/file/fs_cache.py` (137) | `kimi-cli/tests/tools/test_glob.py`<br>`kimi-cli/tests/tools/test_fs_cache_invalidation.py`<br>`kimi-cli/tests/tools/test_additional_dirs.py`<br>`kimi-cli/tests/tools/test_work_dir_resolution.py`<br>`kimi-cli/tests/tools/test_output_utils.py` |  |
| C08 | grep (Grep) | `kimi-cli/src/kimi_cli/tools/file/grep_local.py` (2355)<br>`kimi-cli/src/kimi_cli/tools/file/grep_archive.py` (155)<br>`kimi-cli/src/kimi_cli/tools/file/grep_output.py` (115)<br>`kimi-cli/src/kimi_cli/tools/file/grep_recorder.py` (97)<br>`kimi-cli/src/kimi_cli/tools/file/grep_selectors.py` (320) | `kimi-cli/tests/tools/test_grep.py`<br>`kimi-cli/tests/tools/test_grep_archive.py`<br>`kimi-cli/tests/tools/test_grep_output.py`<br>`kimi-cli/tests/tools/test_grep_recorder.py`<br>`kimi-cli/tests/tools/test_grep_rich.py`<br>`kimi-cli/tests/tools/test_grep_ripgrep_only.py`<br>`kimi-cli/tests/tools/test_grep_selectors.py` |  |
| C09 | write (WriteFile) | `kimi-cli/src/kimi_cli/tools/file/write.py` (834)<br>`kimi-cli/src/kimi_cli/tools/file/write.md` (9)<br>`kimi-cli/src/kimi_cli/tools/file/check_fmt.py` (181)<br>`kimi-cli/src/kimi_cli/tools/file/parse_check.py` (112)<br>`kimi-cli/src/kimi_cli/tools/file/auto_repair.py` (438)<br>`kimi-cli/src/kimi_cli/tools/file/auto_generated.py` (345)<br>`kimi-cli/src/kimi_cli/tools/file/blackbox.py` (133)<br>`kimi-cli/src/kimi_cli/tools/file/conflict_detect.py` (864)<br>`kimi-cli/src/kimi_cli/tools/file/snapshot_store.py` (550)<br>`kimi-cli/src/kimi_cli/tools/file/fs_cache.py` (137)<br>`kimi-cli/src/kimi_cli/tools/file/edit_safety.py` (125) | `kimi-cli/tests/tools/test_write_file.py`<br>`kimi-cli/tests/tools/test_write_conflict.py`<br>`kimi-cli/tests/tools/test_auto_repair.py`<br>`kimi-cli/tests/tools/test_parse_check.py`<br>`kimi-cli/tests/tools/test_auto_generated_guard.py`<br>`kimi-cli/tests/tools/test_check_fmt.py`<br>`kimi-cli/tests/tools/test_blackbox.py`<br>`kimi-cli/tests/tools/test_conflict_detect.py`<br>`kimi-cli/tests/tools/test_snapshot_store.py` |  |
| C10 | edit (EditFile) | `kimi-cli/src/kimi_cli/tools/file/edit/__init__.py` (107)<br>`kimi-cli/src/kimi_cli/tools/file/edit/base.py` (218)<br>`kimi-cli/src/kimi_cli/tools/file/edit/params.py` (194)<br>`kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` (525)<br>`kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` (363)<br>`kimi-cli/src/kimi_cli/tools/file/replace.py` (12) | `kimi-cli/tests/tools/test_edit_conflict.py`<br>`kimi-cli/tests/tools/test_edit_conflict_plan24.py`<br>`kimi-cli/tests/tools/test_edit_file.py`<br>`kimi-cli/tests/tools/test_edit_file_fuzzy.py`<br>`kimi-cli/tests/tools/test_edit_file_parse_repair.py`<br>`kimi-cli/tests/tools/test_edit_modes_detect.py`<br>`kimi-cli/tests/tools/test_edit_safety.py`<br>`kimi-cli/tests/tools/test_edit_sloppy.py` |  |
| C11 | web_search (SearchWeb) | `kimi-cli/src/kimi_cli/tools/web/search.py` (125)<br>`kimi-cli/src/kimi_cli/tools/web/search.md` (1)<br>`kimi-cli/src/kimi_cli/tools/web/providers.py` (2077)<br>`kimi-cli/src/kimi_cli/tools/web/content.py` (186) | `kimi-cli/tests/tools/test_web_search_dispatch.py`<br>`kimi-cli/tests/tools/test_web_providers.py`<br>`kimi-cli/tests/tools/test_web_providers_extra.py` |  |
| C12 | fetch_url (kimi_cli) | `kimi-cli/src/kimi_cli/tools/web/fetch.py` (333)<br>`kimi-cli/src/kimi_cli/tools/web/fetch.md` (1)<br>`kimi-cli/src/kimi_cli/tools/web/content.py` (186)<br>`kimi-cli/src/kimi_cli/tools/web/url_safety.py` (389)<br>`kimi-cli/src/kimi_cli/tools/web/providers.py` (2077) | `kimi-cli/tests/tools/test_fetch_url.py`<br>`kimi-cli/tests/tools/test_url_safety.py` |  |
| C13 | web_extract (WebExtract) | `kimi-cli/src/kimi_cli/tools/web/extract.py` (246)<br>`kimi-cli/src/kimi_cli/tools/web/extract.md` (1)<br>`kimi-cli/src/kimi_cli/tools/web/content.py` (186)<br>`kimi-cli/src/kimi_cli/tools/web/url_safety.py` (389)<br>`kimi-cli/src/kimi_cli/tools/web/providers.py` (2077) | `kimi-cli/tests/tools/test_web_extract.py`<br>`kimi-cli/tests/tools/test_url_safety.py` |  |
| C14 | context_prune | `kimi-cli/src/kimi_cli/tools/context_prune.py` (449) | `kimi-cli/tests/tools/test_context_prune.py`<br>`kimi-cli/tests/core/test_kimisoul_context_prune.py`<br>`kimi-cli/tests/core/test_current_turn_start_index.py` |  |
| C15 | HashRead / HashLine / HashEdit | `kimi-cli/src/kimi_cli/tools/file/hash_line.py` (1156) | `kimi-cli/tests/tools/test_hash_line.py`<br>`kimi-cli/tests/tools/test_plan25_integration.py`<br>`kimi-cli/tests/native/test_diff_glob_tools_equivalence.py` |  |
| C16 | demo tools plus / compare / panic | `kimi-cli/src/kimi_cli/tools/test.py` (55) | **none** | **no test glob declared** |
| K00 | shared infra | `src/kimix/tools/common.py` (2342)<br>`src/kimix/tools/prompt_common.py` (134)<br>`src/kimix/tools/security.py` (91)<br>`src/kimix/tools/check_fmt.py` (33) | `tests/test_tools_async.py`<br>`tests/unit/tools/test_prompt_common.py`<br>`tests/unit/tools/test_security.py`<br>`tests/test_check_fmt.py`<br>`tests/test_token_filter.py`<br>`tests/test_filter_output.py`<br>`tests/test_temp_folder_cleanup.py` |  |
| K01 | subagent (Agent) | `src/kimix/tools/agent/__init__.py` (1179)<br>`src/kimix/tools/agent/store.py` (139) | `tests/kimix/test_agent_conversation.py`<br>`tests/kimix/test_subagent_anonymous_session.py`<br>`tests/kimix/test_subagent_interrupt_race.py`<br>`tests/test_prompt_waits_background_agents.py`<br>`tests/kimix/test_report_sync.py` |  |
| K02 | list_agents (AgentList) | `src/kimix/tools/agent/__init__.py` (1179) | `tests/kimix/test_agent_conversation.py` |  |
| K03 | interrupt_agent (AgentClose) | `src/kimix/tools/agent/__init__.py` (1179) | `tests/kimix/test_agent_conversation.py` |  |
| K04 | bash (Bash) | `src/kimix/tools/file/bash/bash_tool.py` (1461)<br>`src/kimix/tools/file/bash/bash_fix.py` (135)<br>`src/kimix/tools/file/bash/shell_common.py` (159)<br>`src/kimix/tools/file/bash/safety.py` (665)<br>`src/kimix/tools/file/bash/output_enhance.py` (106)<br>`src/kimix/tools/file/bash/__init__.py` (11) | `tests/test_bash.py`<br>`tests/test_bash_e2e.py`<br>`tests/test_shell_common.py`<br>`tests/test_shell_safety.py`<br>`tests/test_output_enhance.py`<br>`tests/native/test_shell_security_equivalence.py` |  |
| K05 | pwsh (Powershell) | `src/kimix/tools/file/bash/pwsh_tool.py` (1147)<br>`src/kimix/tools/file/bash/pwsh_fix.py` (113)<br>`src/kimix/tools/file/bash/process_pwsh.py` (94)<br>`src/kimix/tools/file/bash/pwsh_tool.md` (6) | `tests/test_powershell.py`<br>`tests/test_pwsh_fix.py`<br>`tests/test_process_pwsh.py`<br>`tests/test_windows_env.py` |  |
| K06 | Run | `src/kimix/tools/file/run.py` (799) | `tests/test_run.py`<br>`tests/test_custom_config.py`<br>`tests/test_windows_env.py` |  |
| K07 | python | `src/kimix/tools/py/__init__.py` (816) | `tests/unit/tools/test_python.py`<br>`tests/test_tools_async.py`<br>`tests/unit/tools/test_elapsed_report.py` |  |
| K08 | job_output (TaskOutput) | `src/kimix/tools/background/__init__.py` (508)<br>`src/kimix/tools/background/utils.py` (626) | `tests/unit/tools/test_taskoutput.py`<br>`tests/test_background_utils.py`<br>`tests/test_stream_repeat_output.py` |  |
| K09 | compact | `src/kimix/tools/context/__init__.py` (130) | `tests/unit/tools/test_context_compact.py`<br>`tests/test_integration_compaction.py` |  |
| K10 | WritePlan | `src/kimix/tools/note/__init__.py` (517) | `tests/test_note.py` |  |
| K11 | ReadPlan | `src/kimix/tools/note/__init__.py` (517) | `tests/test_note.py` |  |
| K12 | EditPlan | `src/kimix/tools/note/__init__.py` (517) | `tests/test_note.py` |  |
| K13 | workflow (AgentSwarm) | `src/kimix/tools/swarm/__init__.py` (542)<br>`src/kimix/tools/swarm/best_of_n.py` (423) | `tests/kimix/test_agent_swarm.py`<br>`tests/test_best_of_n.py` |  |
| K14 | fetch_url (kimix) | `src/kimix/tools/web/fetch_url.py` (72)<br>`src/kimix/tools/web/web_fetcher/__init__.py` (3)<br>`src/kimix/tools/web/web_fetcher/fetcher.py` (233) | `tests/unit/tools/test_fetchurl.py` |  |
| K15 | ParserTool | `src/kimix/tools/parser/__init__.py` (237) | `tests/test_parsers.py` |  |
| K16 | FindStr | `src/kimix/tools/file/find_str.py` (193) | `tests/native/test_behavior_equivalence.py` |  |
| K17 | Mkdir | `src/kimix/tools/file/__init__.py` (61) | **none** | **no test glob declared** |
| K18 | Rm | `src/kimix/tools/file/__init__.py` (61) | **none** | **no test glob declared** |

## Plan-declared test files that do NOT exist

* (none)

## Registration sources

* `kimi-cli/src/kimi_cli/agents/{default,okabe}/*.yaml`
* `src/kimix/agent_{worker,subagent,boss,planner,readonly}.json`
* programmatic: `kimi-cli/src/kimi_cli/soul/kimisoul.py:488-490`

## Known tree drift vs the plan

* `file/_ripgrep_common.py` listed for C08 — absent.
* `file/grep_output.py` exists and is in C08 scope.
* `ignore`d stale `__pycache__` trees exist under both roots (FP-06).
* Plan §4.3 places the `read` unit tests at `kimi-cli/tests/unit/tools/`; they actually live at `tests/unit/tools/test_read_*.py` (root project). Gate commands were corrected accordingly.
* Plan §4.3 also names tests that do not exist in either tree (e.g. `kimi-cli/tests/tools/test_snapshot_store.py` exists but `kimi-cli/tests/unit/tools/...` does not).

## Declared-scope additions (deviation from plan §4.1 file list)
* `tools/gate_dup_allowlist.txt` — G-DUP needs a durable waiver file to stay re-runnable with the bare command; created in P0.
* `tools/gen_inventory.py` — reproducible generator for this inventory.
