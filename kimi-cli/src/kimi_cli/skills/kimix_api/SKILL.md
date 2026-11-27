---
name: kimix_api
description: Guide for using KimiX API utilities covering kimix, kimix.utils, kimix.base, kimix.dag, kimix.network, kimix.server, kimix.parser, kimix.cot, kimix.retrieval, kimix.ui, and kimi_agent_sdk.
---

# Kimi API Utilities Guide

High-level guide to `kimix.utils`, `kimix.base`, the full public API of `src/kimix` and `src/kimi_agent_sdk`. Full code examples per section: read `references/api.md`.

_(Verified against `kimix` 0.1.29 — re-check signatures when bumping the version.)_

## Initialization & Config (`kimix.utils.config`)

- `init(config_path=None, config_json=None, *, yolo=True, think=True, skill_dir=None, manually_cot=False, colorful_print=True, clean=False)` — initialize global state (auto-resolves provider from `default_config.json` → env vars → OAuth if skipped). `skill_dir` is a `list[str]`.
- `_create_config(provider_dict=None) -> (Config, provider_dict | None)` — build a validated `Config` from a provider dict.
- Also: `load_config_json(work_dir=None)`, `clear_config_json_cache()`, `get_backup_providers()`, `sub_provider_differs(role="sub_agent", main_provider=None)`, `provider_identity(provider)`.

## Session Management (`kimix.utils`)

- `create_session(session_id=None, work_dir=KaosPath, skills_dir=None, agent_file=None, resume=False, provider_dict=None, chat_provider=None, agent_type=SystemPromptType.Worker, vfs_path=None, extra_system_prompt=None, anonymous=False, custom_data=None, weaker_provider=False)`; close with `close_session(session)` / `await close_session_async(session)`.
- `_create_session_async(...)` — same params, async; `create_supervisor_session(...)` — Supervisor role with `agent_boss.json` (no `agent_file`/`agent_type`).
- Default session: `_create_default_session(resume=True)`, `get_default_session()`, `_create_default_session_async(resume=True)`.
- `prompt(prompt_str, session=None, output_function=None, info_print=True, cancel_callable=None, close_session_after_prompt=False, merge_wire_messages=None, ensure_todo_finished=True, export_todo_list_path=None, format_output=False, timeout=None, sanitize=True)` / `await prompt_async(...)` — `sanitize` runs `prepare_prompt_text` (path escaping); exports >64 KiB prompts to temp files; retries non-API errors up to 3×, then fails over to backup providers; runs todo reminders, clears todos.
- Steer a running turn: `steer_session(session, content) -> bool`, `steer_session_sync(session, content) -> bool`.
- Cancel: `cancel_prompt(session)`, `get_cancel_event(session)`.
- Context: `clear_default_context(force_create=False, resume=False, print_info=True)`, `compact_default_context()`, `print_usage(session=None)`, `context_path()`, `delete_session_dir()`; per-session `clear_context`/`compact_context` (+ `_async`) from `kimix.utils.session`.
- Tool errors: `get_tool_call_errors(session=None)`; richer parsing in `kimix.utils.tool_calls` — `ToolInvocation`, `collect_tool_invocations`, `iter_tool_invocations`, `session_history`, `split_tool_result`, `tool_call_error_dicts`.

## System Prompt Types (`kimix.utils.system_prompt`)

`SystemPromptType.Worker/TodoMaker/Thinker/TrivialSubAgent/Supervisor/Reader/SwarmLeader/BrowserUse`; build prompts via `get_system_prompt(yolo=None, work_dir=None, extra_system_prompt=None, agent_role=SystemPromptType.Worker, max_system_prompt_tokens=4000, weaker_provider=False)` returning a callable `(runtime, is_compacting=False, compact_export_path=None) -> str`; customize with `SystemPromptCallback.role_callback` (append bullets) or `SystemPromptCallback.prompt_override` (replace the whole assembled prompt, e.g. to make one document the entire system prompt).

## Colorful Printing (`kimix.base`, defined in `kimix.ui.printing` / `kimix.ui.stream`)

`print_success/print_error/print_warning/print_info/print_debug/print_string`, `colorful_print(text, fg, bg, styles, end="\n", file=None, flush=False)`, `colorful_text(...)` with `Color`/`BgColor`/`Style`; **await** `print_agent_json(wire_msg, session, output_function=None, format_output=False)` — streams wire messages (auto-resolves approvals, skips step markers, streams tool args/results) — and `print_agent_json_flush_text()`. One-line arg summaries: `format_tool_args(args)`; tool-name repair: `resolve_tool_name(name, valid_names, ...)` / `normalize_tool_name(name)`.

## Threading / Process (`kimix.base`)

- `run_thread(func, args)` (blocks past 8 live threads), `sync_all()`.
- `async_prompt(text, session=None)`, `async_fix_error(command, extra_prompt=None, keycode=..., skip_success=True, max_loop=4, session=None)` — both return a `threading.Thread`.
- `run_process_with_error(command, keycode, skip_success=True)` (+ `_async`); `run_script(path)` (new console window).

## File / Plan / Fix (`kimix.utils`)

- `prompt_path(path, split_word=None, session=None, after_prompt_coro=None)`.
- `fix_error(command, extra_prompt=None, keycode=..., skip_success=True, session=None, max_loop=4, merge_wire_messages=False)` (+ `fix_error_async` — coroutine, importable from `kimix.utils.fix_error` only, **not** in the `kimix.utils` top level).
- `prompt_plan(requirement, plan_file="plan.md")` / `prompt_plan_async` — TodoMaker planner → review → implement → verify; `build_planner_provider_dict()`, `lock_planner_session_read_only(session)`.

## Configuration Variables (`kimix.base`)

`_default_thinking`, `_default_yolo`, `_default_agent_file(_dir)`, `_default_skill_dirs`, `_default_provider`, `_default_sub_providers`, `_default_manually_cot`, `_quiet`, `_colorful_print`, `_stream`, `COMMON_SKILL_DIRS`; setters `set_default_thinking/yolo/agent_file/agent_file_dir/skill_dirs/provider/sub_providers/manually_cot`; `get_default_sub_provider(role="sub_agent")`, `get_default_sub_providers_by_role(role="backup")`, `get_skill_dirs(use_kaos_path=True)`; utils `percentage_str`, `percentage_and_token(session)`, `generate_memory` (a prompt `str` constant).

Path helpers live in `kimix.utils` (not `kimix.base`): `make_kaos_dir(obj)`, `context_path()`, `delete_session_dir()`.

## Prompt String Utilities (`kimix.utils.prompt_str`)

- `escape_file_paths(text, *, max_chars=0, truncate_msg="")` — backtick-wraps legal paths, then removes only tokenizer-hostile characters (lone surrogates/noncharacters, invisible padding, C0/C1 controls); preserves emoji, valid special characters, case, spacing, punctuation runs, full-width forms, long char runs, code blocks, quotes/URLs/fractions/dates. No NFKC, no case folding, no Traditional → Simplified conversion.
- `clean_text(text, keep_newlines=True)` — remove zero-width/control chars, NFC-normalize.
- `prefilter_tool_result(text, *, tool_name)` — trim a tool result before it enters the context.

## Windows Environment

`refresh_env_from_registry()` — reload HKLM/HKCU env vars into `os.environ`.

## `kimi_agent_sdk`

- `kimi_agent_sdk.prompt(user_input, *, work_dir, config, model, thinking, yolo, approval_handler_fn, agent_file, mcp_configs, skills_dir(s), max_steps_per_turn, max_retries_per_step, final_message_only)` — async generator of `Message`.
- `Session.create(work_dir=None, *, session_id, config, model, thinking, yolo, plan_mode, agent_file, mcp_configs, skills_dir(s), anonymous, max_steps_per_turn, max_retries_per_step, config_json, **custom)` / `Session.resume(work_dir, session_id=None, *, ...same...)` — async context manager: `prompt(user_input, *, merge_wire_messages=False, max_restarts=3)` (async generator of `WireMessage`), `cancel()`, `close()`, `close_sync()`, `clear(**custom)`, `rename(new_id)`, `compact(*, custom_instruction="")`, `export(output_path=None, format=ExportFormat.Markdown) -> (Path, int)`, `get_custom_data()`, `get_custom_config()`, `get_server_processes()`; properties `id`, `model_name`, `status`.
- Re-exports: `CallableTool2`/`ToolOk`/`ToolError`, wire/message types, `Config`/`MCPConfig`, exceptions, `ApprovalHandlerFn`/`ApprovalRequest`, `MessageAggregator(final_message_only=False)` (`feed(wire_msg) -> list[Message]`, `flush()`).

## `kimix.dag` — DAG Execution

`DAG.add_node(TaskNode(name, func, params, dependencies, retries))`, `add_edge(upstream, downstream)`, `get_node`, `validate`, `nodes`/`edges`; `Context.get/set/update/cancel/cancelled/check_cancelled`; `Executor(max_workers).execute(dag, ctx=None) -> dict`; `TopologicalSorter(edges).sort()`; `detect_cycle`, `validate_dag`, exceptions `DAGValidationError`/`CycleError`/`DependencyError`/`ExecutionError`.

## `kimix.network` — TCP / JSON-RPC

**Synchronous API** (no `await`). Package-level re-exports: `from kimix.network import TCPClient, TCPServer, TcpGroupServer, JSONRPCClient, JSONRPCServer`.

- `TCPClient(host="127.0.0.1", port=8888)` — `connect(blocking=False)`, `disconnect()`, `send`, `send_bytes`, `is_connected`, `on_connect/on_disconnect/on_message`.
- `TCPServer(host, port)` — `start(blocking=False)`, `stop`, `send`, `send_bytes`, `disconnect_client`, `is_client_connected`, `on_*`.
- `TcpGroupServer(host, port, max_workers)` — `start/stop`, `send(client_id, msg)`, `broadcast`, `disconnect_client`, `get_client_ids`, `get_client_count`, `wait_for_clients(count, timeout=5.0)`, `on_client_connect/on_client_disconnect/on_message/on_raw_data`.
- `JSONRPCClient.call(method, *args, timeout=5.0)` (+ `connect/disconnect/is_connected`); `JSONRPCServer.register/register_function/start/stop/wait_for_connection/disconnect_client/start_websocket_server/stop_websocket_server`.

## `kimix.server` — HTTP Server

Package-level re-exports: `create_app`, `KimixAsyncClient`, `SessionManager`, `BusEvent`, `EventBus`, `serve_cli`. The singletons live one level down: `kimix.server.session_manager.session_manager` and `kimix.server.bus.bus`.

- `create_app()` — FastAPI with `/global/health`, `/event` + `/global/event` (SSE), `/session` CRUD, `/session/{id}/{prompt_async,abort,message,status,clear,context,compact,summarize,export,todo,plan,plan/confirm,permissions/{pid}}`, `/agent`, `/config`, `/config/providers`, `/path`, `/project`, `/project/current`, `/session/status`, `/experimental/features`.
- `KimixAsyncClient(host="127.0.0.1", port=4096, timeout=30.0)` — `health_check`, `create_session`, `get/list/delete_session`, `get_messages(limit=10)`, `get_session_status`, `send_prompt_async`, `abort_session`, `clear_session`, `compact_session`, `export_session`, `stream_events`, `stream_events_robust`.
- `SessionManager` (global `session_manager`), `BusEvent`/`EventBus` (global `bus`), `serve_cli(args)`.

## `kimix.parser` — Comment Parsers

`PythonParser/CParser/ShellParser/HtmlParser/PascalParser/LispParser/SqlParser` — `parse(source_code) -> ParseResult` (`total_comments`, `comment_lines`, `get_comments_by_kind`), inherited `BaseParser.parse_file(file_path, encoding="utf-8")`; `Comment(content, line, column, kind)`.

## Built-in Agent Tools (`kimi_cli.tools`)

Tools are registered as `module:name` in the agent JSON (e.g. `kimi_cli.tools.file.bash:bash`). Registry name → class:

| tool name | module |
|---|---|
| `bash` / `pwsh` | `kimi_cli.tools.file.bash` (`Bash` / `Powershell`) |
| `Run` | `kimi_cli.tools.file.run` (`Run`, disabled by default) |
| `FindStr` | `kimi_cli.tools.file.find_str` |
| `read` / `read_image` / `write` / `edit` / `glob` / `grep` | `kimi_cli.tools.file` |
| `HashRead` / `HashEdit` | `kimi_cli.tools.file.hash_line` |
| `python` | `kimi_cli.tools.py` |
| `job_output` / `BackgroundStream` | `kimi_cli.tools.background` |
| `todo_list` / `compact` / `skill` / `research` | `kimi_cli.tools.{todo,context,skill,research}` |
| `subagent` / `list_agents` / `interrupt_agent` | `kimi_cli.tools.agent`, `kimi_cli.tools.subagent` |
| `workflow` | `kimi_cli.tools.swarm` (`AgentSwarm`) |
| `broadcast` / `AskUserQuestion` | `kimi_cli.tools.broadcast`, `kimi_cli.tools.ask_user` |
| `write_plan` / `read_plan` / `edit_plan` | `kimi_cli.tools.note` |
| `ParserTool` | `kimi_cli.tools.parser` |
| `fetch_url` / `web_search` / `web_extract` | `kimi_cli.tools.web.fetch` (or `web.kimix_fetch`), `web.search`, `web.extract` |
| `computer_use` / `browser_use` | `kimi_cli.tools.computer_use`, `kimi_cli.tools.browser_use` |

`kimi_cli.tools` has no top-level re-exports — import from the submodule (e.g. `from kimi_cli.tools.background import TaskOutput`). Shared helpers: `kimi_cli.tools.common` / `kimi_cli.tools.security` / `kimi_cli.tools.prompt_common`. `src/kimix/tools/` is a stale empty tree — real tools live under `kimi-cli/src/kimi_cli/tools/`.

## `kimix.cot` — Chain-of-Thought

`cot_prompt(prompt_str, self_verify=True, existing_thinking=None, max_iterations=10) -> CoTResult(thinking, quit)` (+ `cot_prompt_async`); two-pass `cot_prompt_with_verification` (+ `_async`) — note: it takes only `(prompt_str, existing_thinking=None)`.

## `kimix.retrieval` — BM25

`NgramTokenizer`, `InvertedIndex`, `BM25Scorer.score/score_topk`, `Searcher.search(query, top_k)`, `LevenshteinAutomaton`, `SimHash`/`SimHashLSH`, `RM3Expander`/`RocchioExpander`, `LambdaMART`/`CoordinateAscent`/`RankSVM`/`RankBoost`, `QueryPerformancePredictor`, `MinHash`, `NoisyChannelSpeller`, plus utility functions (`jaro_winkler_similarity`, `cosine_similarity_tfidf`, `mmr_rerank`, `xquad_rerank`, `clarity_score`, `scq`, `i_match_fingerprint`, `soundex`/`metaphone`/`porter_stem`, ...).

## `kimix.cli`

`cli()` — launches the interactive Kimix CLI.

## Compaction / Summarize (no `kimix.summarize` module)

`kimix.summarize` was deleted. Compaction now lives in:

- `kimix.utils`: `compact_default_context()`, `compact_context(session)` / `compact_context_async(session)` (from `kimix.utils.session`), i.e. `Session.compact(custom_instruction="")`.
- HTTP: `SessionManager.compact_session(id, keep=None)` and the `POST /session/{id}/summarize` route.
- Pipeline internals: `kimi_cli/soul/compaction.py` (+ `compaction_ledger.py`, `tool_pairing.py`, `context_overflow.py`).

## System Reminders

`<system-reminder>` user messages are appended to the context by `KimiSoul` (auto-retrieved memory citations on step 1, the verification gate, and notifications) and stripped/re-injected fresh each step. `normalize_history()` (in `kimi_cli/soul/message.py`, `(history) -> list[Message]`) merges adjacent user messages for the API call but never merges system-reminder or notification messages.

## Context Pruning

`ContextPruner`/`PruningResult`/`ElidedRecord` in `kimi_cli/soul/context_pruning.py`, integrated in `KimiSoul._step()`. Tier A drops consumed ephemera; Tier B elides stale/oversized content with archived stubs. Config via `LoopControl.prune_*` fields (master switch `context_pruning_enabled`). Manual trigger: `/prune`.

## Complete Package Index

| Package / Module | Description |
|------------------|-------------|
| `kimi_agent_sdk` | Python SDK for building AI agents (Session, prompt, wire/message types, `MessageAggregator`, config, exceptions) |
| `kimix.base` | Colorful printing, threading, process execution, config variables (re-exports `kimix.ui.*`) |
| `kimix.cli` | Interactive CLI entry point (`cli()`) |
| `kimix.cot` | Chain-of-thought reasoning utilities |
| `kimix.dag` | DAG task dependency execution engine |
| `kimix.network` | TCP / JSON-RPC networking layer (sync API) |
| `kimix.parser` | Source code comment parsers for multiple languages |
| `kimix.retrieval` | BM25 retrieval engine, fuzzy search, ranking, query performance prediction |
| `kimix.server` | Opencode-style HTTP server (FastAPI + SSE) |
| `kimix.ui` | Terminal printing (`printing`) and wire-message streaming (`stream`) |
| `kimi_cli.tools` | Built-in agent tools: bash/pwsh/py, file read/write/edit/glob/grep, todo, plan, subagents, web, browser/computer use, background jobs (all under `kimi-cli/src/kimi_cli/tools/`) |
| `kimix.utils` | Session management, prompting, plan execution, error fixing, init, prompt string utilities |
| `kimix.utils.config` | `init`, `_create_config`, `load_config_json`, provider helpers |
| `kimix.utils.event_loop` | `get_process_event_loop`, `run_on_event_loop`, `close_process_event_loop` |
| `kimix.utils.fix_error` | `fix_error`, `fix_error_async`, `async_prompt`, `async_fix_error` |
| `kimix.utils.prompt` | `prompt`, `prompt_async`, `prompt_plan(_async)`, `prompt_path`, `prepare_prompt_text`, `steer_session(_sync)` |
| `kimix.utils.prompt_str` | `escape_file_paths`, `clean_text`, `prefilter_tool_result` |
| `kimix.utils.session` | Session creation, resumption, context management, lifecycle |
| `kimix.utils.system_prompt` | System prompt types and builders |
| `kimix.utils.tool_calls` | Tool-invocation extraction from session history |
| `kimix.utils.windows_env` | Windows registry environment refresh |
