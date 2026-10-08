# K01 — `subagent` (`Agent`) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | `reviews/tools/gates/K01.txt` (§G1…§H) — all 2 in-scope sources + 5 test files read; ≥1 `path:line` citation per file below; registration recorded in §1 |
| G1 simplicity | **fail** | `ruff check` exit 1 (4 errors: F841 `agent/__init__.py:584`, F401×2 + I001 `agent/store.py:3,4,1`); `ruff --select C901` exit 0; `review_metrics --max-fn-loc 120 --max-complexity 12` exit 1 → `complexity_max=19` (`_notify_parent_background_finished`, hard-fail >15) and 14 (`_format_history`); vulture 1 hit (waived FP); git_diff clean/M | 
| G2 risk | pass | 13/13 items answered, `open_high=0`, `open_medium=4` (all logged in §4) |
| G3 coverage | **fail** | module `kimix.tools.agent` + `kimix.tools.agent.store`; floor **85**; actual **87.66 %** (dotted re-run) / 89.42 % verbatim / 92.5 % with probes; `waived_lines=53 > waiver_budget=25`; the verbatim `--cov=src/...` form emits `CoverageWarning: module-not-imported` and measures **no row for `agent/__init__.py`** |
| G4 behavior | pass | `tests/kimix/test_agent_gate.py` created; required 14, present **20**; 20 passed |
| G5 regression | pass | root suite = pre-existing collection block (exit 2, `tests/bash/cases.json` missing); CLI suite = same **43** pre-existing failures, **0 new, 0 fixed**; `syntax_check` clean; `git_diff` = only the new probe file |
| G6 independent rerun | pass (self-check) / orchestrator rerun pending | harness `K01.json` reproduces every number; reviewer re-ran all mechanical gates at the moved HEAD → identical (evidence `[RE-VERIFY @ 7aa84025]`). Compare: `complexity_max=19`, `longest_fn_loc=88`, coverage 87.66 %, probes 20, G5 43/43 |

## 1. Scope

**Files read end-to-end (path + LOC):**
- `src/kimix/tools/agent/__init__.py` (1179 LOC) — module registry/helpers L1–311, `SubAgentParams` L314–395, `_PreparedRun` L398–411, `_AgentConversationCollector` L435–515, `Agent` L518–1095, `AgentList` L1098–1131, `AgentClose` L1134–1179.
- `src/kimix/tools/agent/store.py` (139 LOC) — `ConversationTurn` L13–17, `AgentSessionEntry` L20–39, `AgentSessionStore` L42–139.

**Tests in scope (read end-to-end):** `tests/kimix/test_agent_conversation.py` (1419), `tests/kimix/test_subagent_anonymous_session.py` (573), `tests/kimix/test_subagent_interrupt_race.py` (348), `tests/test_prompt_waits_background_agents.py` (95), `tests/kimix/test_report_sync.py` (345).

**Registration:** REGISTERED. Manifest `src/kimix/agent_boss.json:14-16` and `src/kimix/agent_worker.json:16-18` list `kimix.tools.agent:subagent | list_agents | interrupt_agent`; registry `reviews/tools/00-registry.txt:37-39`. Not an ORPHAN. G-DUP note: the model-facing name `subagent` is also implemented by `kimi_cli.tools.agent:AgentTool` (`00-registry.txt:18`) — C01/G7 own that.

## 2. Behaviour map

| entry point | lines | branches / notes |
|---|---|---|
| `_background_tasks_for(session)` | 42–64 | CLI session vs SDK-wrapped (`_cli.session`); no `custom_data` → throwaway `set()`; memoises the set in `custom_data["agent_background_tasks"]` |
| `wait_for_background_agents(session)` | 67–78 | `if pending` → `asyncio.gather(..., return_exceptions=True)`; never raises. Called from `kimix/utils/prompt.py:752` |
| `_register_agent_session` / `_get_agent_session` / `_unregister_agent_session` | 88–193 | guards on empty id / `None` session |
| `_register_child_session` / `_forget_child_session` / `_take_child_sessions` / `_forget_child_session_record` / `_release_child_session` | 106–148 | parent↔child reverse lookup; empty ids ignored; `_take_child_sessions` pops both directions (sorted) |
| `_destroy_child_sessions_async(parent_id)` / `_on_parent_session_closed(session)` | 151–177 | cascade close; per-child `try/except`; hook registered at import (L183) |
| `_cli_session_id` / `_session_work_dir` / `_session_dir` / `_sdk_session_by_id` | 196–257 | SDK unwrap (`_cli.session`); `_session_dir` → `_sdk_sessions_dir(work_dir)/id`, `KaosPath('.')` fallback |
| `_register_entry` / `_get_entry` / `_unregister_entry` (`_agent_entries`, L31) | 260–269 | **write-only registry**: `_get_entry` is never called (see F4) |
| `_resolve_prompt(prompt, base_dir)` | 272–295 | `@`-prefix only; relative → `base_dir`, CWD fallback; `FileNotFoundError` |
| `_prompt_saved_message(prompt, ext)` | 298–311 | `""` for empty; `_create_script_file` + `_display_temp_path`; returns retry hint |
| `_consume_background_task_error(task)` | 414–421 | `cancelled()` guard; `except Exception: pass` |
| `_get_store(session)` | 424–432 | memoised in `custom_data["agent_conversation_store"]`, `on_close=_forget_child_session_record` |
| `_AgentConversationCollector` | 435–515 | `_finalize_previous`, `consume` (Text/Thinking/ToolCalling(Part)/ToolResult), `finalize_user_turn`, `finalize_assistant_turn`; reused by `kimix/tools/swarm/__init__.py:21` |
| `SubAgentParams` | 314–395 | 10 fields; aliases `prompt|task`, `session_id(alias=session)` |
| `Agent.__call__` | 554–572 | recursive guard (`custom_config["is_sub_agent"]`); `try: _prepare_run` → `ToolError`; background vs foreground dispatch |
| `_prepare_run` | 574–644 | session resolve; `@` resolve; 100 KiB offload (L592–597); context block (L601–616); `_cancel_event` mid-run rejection (L621–625); close-policy default (L632–636) |
| `_launch_background` | 646–690 | store `put` (state `running`), `create_task`, per-parent task set + done callbacks, immediate `ToolOk` |
| `_run_background` | 692–705 | defensive `ToolError("Background subagent failed")`; then notify parent |
| `_notify_parent_background_finished` | 707–759 | parent lookup, 2000-char truncation, interrupted-vs-completed wording, 1500-char partial, `Steer.from_session(parent).push` |
| `_execute` | 761–848 | collector, `utils.prompt_async`, error branch (saved-prompt hint, L799–823) → `ToolError`; success → `_build_extras` + `_update_store` → `ToolOk`; outer `except` L843–848 |
| `_build_extras` / `_format_history` | 850–902 | shared extras; `json|markdown|summary` (+ defensive `[]`) |
| `_resolve_session` | 904–976 | reuse active entry; anonymous decision from `_session_dir(...).exists()` (L934); `inherit_context` copy; `_create_session_async(..., resume=True, anonymous=…)`; `is_sub_agent=True`; tombstone discard |
| `_inherit_parent_context` / `_reset_inherited_system_prompt` | 978–1037 | `Session.copy(work_dir, parent_id, target)`; ValueError for missing id/work dir; best-effort prompt reset |
| `_register_agent_sessions` / `_update_store` | 1039–1095 | parent+child registration; close vs keep; tombstone guard against resurrect |
| `AgentList.__call__` | 1127–1131 | `store.list_active()` → `orjson.dumps`; `params` unused |
| `AgentClose.__call__` | 1163–1179 | unknown id → `ToolError("Session not found")`; else close + registry cleanup |
| `AgentSessionStore` | 42–139 | `MAX_SESSIONS=10`, `MAX_TOMBSTONES=64`, `put/close/was_closed/discard_closed/list_active/evict_lru_if_needed` (completed-first eviction) |

## 3. S1 Simplicity (G1 detail)

**verdict: fail (gate-failing)**

| finding | file:line | evidence | change | waiver? |
|---|---|---|---|---|
| F1 unused variable (ruff F841) | `agent/__init__.py:584` | `entry = store.get(session_id)` never read (`store` also re-fetched at 583 while `_resolve_session` already fetched it) | delete the assignment (and the redundant `_get_store`) | no |
| F13b unsorted/unused imports (ruff I001/F401×2) | `agent/store.py:1,3,4` | `import asyncio`, `import time` unused; import block unsorted | remove both imports, sort block | no |
| F4 dead write-only registry | `agent/__init__.py:31,260-269` (+ writes at 663, 911, 1095; pops at 139, 820, 1069) | `_agent_entries` is read **only** by `_get_entry`, which has **zero** call sites in the repo | delete `_agent_entries`, `_register_entry`, `_get_entry`, `_unregister_entry` and their 6 call sites (tests import them → update tests) | no (medium) |
| F2 complexity hard-fail | `agent/__init__.py:707-759` | `complexity=19 > 15`, `branches=18`, 53 LOC — mixes parent lookup, truncation, interrupted/complete wording and steer delivery | split into `_interrupted_notice()` / `_completion_notice()` + `_deliver_notice()`; target ≤12 | no |
| F3 complexity | `agent/__init__.py:877-902` | `complexity=14 > 12` (`_format_history` if/elif chain) | dispatch via a small dict of formatters | no |
| F10 dead params/fields | `agent/__init__.py:317-322` (`description`, never read), `850-857` (`**extra` never passed by the two call sites 811/825), `1099-1106` (`scope`, never read) | greps in `reviews/tools/gates/K01.txt` §G1 show no readers | either use them (surface `description` in the notice) or mark deprecated; the `**extra` hook is single-use over-abstraction | no (low) |
| F12 unnamed magic numbers | `agent/__init__.py:593` (`100*1024`), `723` (`2000`), `742` (`1500`), `541` (`Semaphore(8)`) | literals inline | name them (`_PROMPT_OFFLOAD_BYTES`, `_NOTICE_TEXT_MAX`, `_NOTICE_PARTIAL_MAX`, `_MAX_CONCURRENT_PREPARES`) | no (low) |
| F16 duplicated close sequence | `agent/__init__.py:818-822` vs `1066-1071` (and `137-141`) | the same 5-step teardown (`close_session_async`, `store.close`, `_unregister_entry`, `_unregister_agent_session`, `_forget_child_session`) is copy-pasted in 3 places | extract `async def _release_child(session_id, session)` | no (low) |
| F17 hot-path shadow import | `agent/__init__.py:612` | `import orjson as _orjson` inside `_prepare_run` while `orjson` is already imported at L10 | use the module-level `orjson` | no (info) |
| vulture | `agent/store.py:5` | `unused import 'Callable'` — **false positive**: used only inside the *string* annotation `"Callable[[str], None] \| None"` (L52) | keep, or drop the quotes so vulture sees it | **waived** (1/1 FP; budget 10) |

**long functions (all ≤ 120, none over the hard 200 cap):**

| function | LOC | keep/split decision |
|---|---|---|
| `_execute` | 88 | keep — longest, but linear |
| `_resolve_session` | 73 | keep |
| `_prepare_run` | 71 | keep |
| `_notify_parent_background_finished` | 53 | **split** (F2 — complexity, not LOC) |
| `_launch_background` | 45 | keep |
| `_update_store` | 39 | keep |
| `_inherit_parent_context` | 34 | keep |

**module LOC:** `agent/__init__.py` = **1179** (< the §3 1200 split threshold, but the §4.4 K01 gate demands a recorded decision) → **keep**. Rationale: the file is one cohesive tool family (`Agent` + its 2 companion tools + the per-parent registries they share); the 20 module-level helpers are all small (median ≪ 30 LOC) and are the shared registry/cascade layer that `swarm`, `utils.prompt` and the close hooks already import. Splitting the registries into a new module would move 4 process-wide dicts away from their only writers and add an import cycle risk with `kimix.utils.session` (close hooks). **Decision: keep; the mechanical fixes are F1/F2/F4 only.** `agent/store.py` = 139 → keep.

## 4. S2 Potential issues (G2 detail)

| # | item | severity | file:line | evidence | impact | fix | waiver |
|---|---|---|---|---|---|---|---|
| a | error contract | clean | `agent/__init__.py:561-569, 843-848, 692-705, 1166-1171` | every failure path returns `ToolError`; `__call__` catches `Exception` (not `BaseException`, so `CancelledError` propagates — correct); `AgentList.__call__` cannot fail on its own data. One residual gap: `_launch_background` (called *outside* the `__call__` try at L571) could raise from `store.put`/`create_task` and escape as an exception — hypothesis, unobserved | low | wrap the dispatch in the same try | – |
| b | async | **finding** | `agent/__init__.py:295, 607, 594, 306` | blocking `Path.read_text` for `@path`, `context_files` and `_create_script_file` (sync write) run directly inside `async def` with no `to_thread` | event loop stall proportional to file size (a `@huge-file` blocks every concurrent session task) | `await asyncio.to_thread(...)` for the two reads + the temp write | no (low) |
| b2 | async | clean | whole module | only `asyncio` is used (no mixed `anyio`); `Semaphore(8)` is created in `__init__` (L541) — py3.10+ binds lazily, so constructing the tool outside a loop is safe | – | – | – |
| c | resources | mostly clean | `692-705, 646-669, 151-169, 414-421` | background tasks held in the per-parent set + `discard` done-callback; exceptions retrieved by `_consume_background_task_error`; child sessions closed on every path; temp files live in the shared temp folder reaped by K00's cleanup. Residual: a background run whose parent never closes is never reaped | low | – | – |
| d | subprocess | clean | – | no `subprocess`/`shell` use in either file (N/A) | – | – | – |
| e | path safety | **finding** | `agent/__init__.py:283-295, 606-607` | `_resolve_prompt` has **no work-dir containment**: `subagent(prompt="@../secret.md")` reads a file *outside* the session work dir (probe 17 asserts `TOP SECRET` is read); same for `context_files=["../../etc/hosts"]`. The sibling `read` tool enforces containment, so this is an inconsistency, not a new privilege (the parent LLM authors the path) | medium | add an explicit containment check (reject paths resolving outside `base_dir` unless absolute-and-explicitly-allowed), reuse the `read` tool's guard | no |
| e2 | path safety | clean | `agent/__init__.py:233-242` | `_session_dir` delegates to `_sdk_sessions_dir`, so it cannot drift from the SDK layout; `\` vs `/` normalised by `Path`/`_display_temp_path` | – | – | – |
| f | injection | **finding (low)** | `agent/__init__.py:608, 613, 616` | `context_files`/`context_data` are interpolated into the child prompt inside `<file path='…'>`/`<data>`; a hostile value containing `</context>` can close the wrapper early. ReDoS/format-string/archive traversal: N/A (no regex, no `str.format` on user data, no archives) | low | escape/strip the closing delimiters, or use a unique nonce per block | no |
| g | secrets | clean | – | no child env, no credentials, no redaction needed; subagent text is returned verbatim (inherent to the tool) | – | – | – |
| h | network | clean | – | no direct network use (the provider call is inside `utils.prompt_async`) | – | – | – |
| i | limits | **finding (low)** | `agent/__init__.py:592-597, 723-743, 794-797` | the 100 KiB offload boundary is `> 100*1024` (probe 16 asserts exactly-at stays inline, +1 byte offloads — correct); UTF-8 handling encodes the whole string (no mid-character slicing); notice truncation 2000/1500 is correct. Residual: the successful `ToolOk.output` (subagent text + history) is **unbounded** | low | cap `output_text`/`conversation_history` or document the cap | no |
| j | state | **finding (low, hypothesis)** | `agent/__init__.py:31, 85, 100, 103` | four process-wide mutable dicts mutated from async code with no lock; the critical sections contain no `await`, so the event loop cannot interleave them. Separate hazard (hypothesis): `kimix.utils.session.close_session` (sync, `asyncio.run`) can run the close hook on a *different* loop while a background task mutates the same dicts | low | move the registries behind a single owner object, or guard with a lock on the sync-close path | no |
| j2 | state (caches) | clean | `424-432, 42-64, 42-139 store` | the per-session store/task-set caches live in `custom_data` and die with the session; tombstones are FIFO-capped (64) | – | – | – |
| k | params | **finding** | `agent/__init__.py:340-350` | `session_id` declares `alias="session"`, so the model-facing JSON schema advertises **`session`** while the tool description (L529, L1124) tells the model to pass `session_id`. Both work (`populate_by_name=True`), but the schema/docs drift (probe 2 pins it). Also: `prompt` has no `min_length`, so `prompt=""` runs a **promptless** subagent (probe 16 asserts it succeeds) | medium (drift) / low (empty) | make the canonical spelling `session_id` via `validation_alias=AliasChoices("session_id","session")`, keep `populate_by_name`; add `min_length=1` or an explicit empty-prompt `ToolError` | no |
| k2 | params (schema round-trip) | clean | probes 1, 2, 18 | `model_dump`→`model_validate` identity and `model_validate_json({"task": …})` both hold; wrong types reject (`prompt=123`, `history_format="xml"`, `context_data=["a"]`, `context_files="x"`, `return_history="maybe"`); lax coercions pinned | – | – | – |
| l | native parity | finding (info) | `agent/__init__.py:332-338` vs `kimi_cli/tools/agent` (C01) | the two `subagent` implementations intentionally differ: `run_in_background` defaults **True** here, **False** in C01 (probe 4 pins K01's side; C01 probe 5 pins the contrast); the param surfaces are disjoint (`timeout`/`subagent_type`/`resume` vs `inherit_context`/`context_files`/`context_data`/`return_history`) | info | none (G-DUP / C01 own the name collision) | yes |
| m | library policy | clean (report only) | `agent/__init__.py:10, 612` | `orjson` is used for JSON (compliant). Stdlib used: `uuid`, `asyncio`, `time`, `pathlib`, `dataclasses`, `typing` — none of which the §7 table replaces (`time`/`pendulum` is `datetime`, not `time`). No `json`/`re`/`hashlib`/`base64`/`datetime`/`sqlite3`/`difflib`. X01 owns the finding | info | – | n/a |

## 5. S3 Coverage (G3 detail)

**Gate command (verbatim from plan §4.4 K01) and results**

| form | module row for `agent/__init__.py` | TOTAL | exit |
|---|---|---|---|
| verbatim (`--cov=src/kimix/tools/agent/__init__.py`) | **absent** (`CoverageWarning: Module src/kimix/tools/agent/__init__.py was never imported`) | 412 stmts, 89.42 % | 0 (floor met, but the tool's 1179-LOC module is **unmeasured**) |
| dotted re-run (`--cov=kimix.tools.agent --cov=kimix.tools.agent.store`) | 532 stmts, 51 miss, 176 branch, 38 partial, **87 %** | 606 stmts, **87.66 %** | 0 (floor **85** met) |
| + G4 probe suite | 532 stmts, 24 miss, 176 branch, 31 partial, **92 %** | 606 stmts, **93 %** | 0 |

**G3 verdict: fail** — the coverage floor is met (87.66 % ≥ 85) and there are no skips, but
`waived_lines = 53 > waiver_budget = 25` (the only budget in the plan, §2.2 template; the §4.4 K01
block defines no per-tool budget). Additionally the **verbatim command is unsound**: the
`--cov=<path>/__init__.py` form silently drops the primary module from the report, so the gate as
written can never detect a regression in 1179 of the 1318 in-scope LOC. Proposed fix:
(i) use `--cov=kimix.tools.agent --cov=kimix.tools.agent.store`, and (ii) add
`tests/kimix/test_agent_gate.py` to the G3 test list (lifts 87.66 % → 93 %, removes 27 of the 53
waivers) or raise K01's documented waiver budget.

**Behaviours mapped → asserting test (`file::test_fn`)**
`behaviors_mapped = 59`; `covered = 38` (30 rows covered by the in-scope suite, 8 more reachable only
through the new probe file); `gaps = 21` distinct uncovered behaviours, which the G3 `Missing` column
reports as 53 uncovered lines/arcs.

| behaviour | asserting test | status |
|---|---|---|
| store `get`/`put`/`close`/`was_closed` | `test_agent_conversation.py::test_store_get_put`, `::test_store_close`, `test_subagent_interrupt_race.py::test_store_tombstone_recorded_and_discarded` | covered |
| store `list_active` | `::test_store_list_active` | covered |
| store LRU eviction (incl. completed-first) | `::test_store_lru_eviction`, `::test_store_eviction_prefers_completed_sessions` | covered |
| store `on_close` notify + failure isolation | `::test_store_notifies_when_a_session_leaves`, `::test_store_notification_failure_does_not_break_eviction` | covered |
| tombstone FIFO cap | `test_subagent_interrupt_race.py::test_store_tombstone_capped_fifo` | covered |
| collector text / thinking / tool-call+result / empty | `::test_collector_text_only`, `::test_collector_thinking_excluded_from_output`, `::test_collector_tool_call_and_result`, `::test_collector_empty_output` | covered |
| recursion guard | `::test_agent_recursive_guard` | covered |
| new session (foreground) | `::test_agent_new_session` | covered |
| keep-alive / reuse / close_session param | `::test_agent_keep_alive_stores_session`, `::test_agent_reuse_session`, `::test_agent_close_session_param` | covered |
| `return_history` json | `::test_agent_return_history` | covered |
| error path → `ToolError` | `::test_agent_error_path` | covered |
| error path saves retry file | `::test_agent_error_saves_prompt_file` | covered |
| `@path` from file / missing | `::test_agent_prompt_from_file`, `::test_agent_prompt_file_missing`, `::test_resolve_prompt_cwd_fallback` | covered |
| long-prompt offload | `::test_agent_long_prompt_offloads_to_temp_file` | covered |
| LRU eviction through `Agent` | `::test_agent_lru_eviction` | covered |
| `inherit_context` (copy / explicit id / ignored on reuse / no parent id / prompt reset ×2 / real dir) | `::test_agent_inherit_context_copies_parent_session`, `::…with_explicit_session_id`, `::…ignored_on_reuse`, `::…without_parent_id_errors`, `::…resets_system_prompt`, `::…reset_skips_when_soul_missing`, `::…copies_real_session_dir` | covered |
| `list_agents` / `interrupt_agent` / not-found | `::test_agent_list`, `::test_agent_close`, `::test_agent_close_not_found` | covered |
| parent+child registration, `is_sub_agent` | `::test_agent_resolve_session_registers_parent_and_child` | covered |
| work-dir inheritance (plain / SDK-wrapped / None→CWD) | `::test_agent_work_dir_inherited_by_sub_session`, `::test_agent_work_dir_sdk_wrapped_session`, `::test_agent_work_dir_none_falls_back_to_cwd` | covered |
| `context_files` base dir | `::test_agent_context_files_resolve_against_work_dir` | covered |
| background returns id immediately | `::test_agent_background_returns_durable_id_immediately` | covered |
| background completes/stays addressable, explicit close, foreground default | `::test_agent_background_completes_and_stays_addressable`, `::test_agent_background_explicit_close_session_closes`, `::test_agent_foreground_default_still_closes` | covered |
| parent notice on completion | `::test_agent_background_notifies_parent_on_completion` | covered |
| `wait_for_background_agents` (plain / SDK-wrapped / prompt integration) | `::test_wait_for_background_agents_waits_until_settled`, `::…sdk_wrapped_session`, `tests/test_prompt_waits_background_agents.py::test_prompt_async_waits_for_background_subagents`, `::test_prompt_async_survives_failing_background_subagent` | covered |
| resume-while-running rejected | `::test_agent_resume_while_running_rejected` | covered |
| anonymous session flag / durable dirs / dir deletion / locked file / kept-alive / closed-resume / inherit-copy-anonymous | `test_subagent_anonymous_session.py::test_fresh_subagent_session_is_anonymous`, `::test_resumed_existing_session_keeps_its_directory`, `::test_subagent_session_dir_deleted_after_finish`, `::test_subagent_session_dir_removed_while_a_file_is_locked`, `::test_subagent_session_dir_kept_while_session_is_alive`, `::test_resuming_a_closed_subagent_leaves_no_directory_behind`, `::test_inherit_context_copy_is_anonymous` | covered |
| parent close/clear cascade, named child kept, child leaves registry, throwaway instance, shutdown path | `test_subagent_anonymous_session.py::test_parent_close_deletes_all_subagent_sessions`, `::test_parent_clear_deletes_all_subagent_sessions`, `::test_parent_close_keeps_a_named_child_session_dir`, `::test_child_closed_normally_leaves_the_parent_registry`, `::test_throwaway_agent_instance_keeps_parent_children`, `::test_shutdown_path_cascades_to_subagent_sessions` | covered |
| `_session_dir` CWD fallback | `test_subagent_anonymous_session.py::test_session_dir_falls_back_to_process_cwd` | covered |
| interrupt-race: no resurrect / fresh resume / notice wording | `test_subagent_interrupt_race.py::test_interrupted_background_run_does_not_resurrect`, `::test_fresh_resume_after_close_registers_normally`, `::test_notice_interrupted_run_not_reported_as_completed`, `::test_notice_normal_completion_unchanged` | covered |
| report-sync name/params/aliases/descriptions → `subagent`,`list_agents`,`interrupt_agent` | `test_report_sync.py::test_tool_name_matches_report[…]`, `::test_canonical_params_present_with_report_descriptions[…]`, `::test_legacy_param_aliases_still_validate[…]`, `::test_legacy_tool_names_resolve_via_redirects` | covered |
| `_format_history` markdown/summary | `tests/kimix/test_agent_gate.py::test_probe_return_history_all_three_formats` | probes only |
| `_background_tasks_for` no-custom_data / per-parent | `test_agent_gate.py::test_probe_background_tasks_are_per_parent` | probes only |
| `_session_dir` == `_sdk_sessions_dir/id` | `::test_probe_session_dir_matches_sdk_layout` | probes only |
| `_register_child_session`/`_take_child_sessions` guards | `::test_probe_child_session_reverse_lookup` | probes only |
| parent-close cascade deletes a real dir | `::test_probe_parent_close_cascade_deletes_child_dir` | probes only |
| defensive background error site | `::test_probe_background_defensive_error_and_double_close` | probes only |
| 100 KiB boundary / empty prompt / hostile `@path` | `::test_probe_prompt_size_boundary_and_empty`, `::test_probe_hostile_at_paths` | probes only |
| context block ordering (files+data) | `::test_probe_context_files_and_context_data` | probes only |

**Uncovered lines (waiver table — `agent/__init__.py` dotted run, 53 rows).** Every row is a line/arc
from the `Missing` column of the G3 evidence; "→ probe" means the existing G4 file already covers it.

| line/arc | behaviour | why it matters | proposed test (file) | proposed assertion |
|---|---|---|---|---|
| 55->58, 59 | SDK wrapper without `custom_data` → throwaway set | silent degradation path of the wait mechanism | `test_probe_background_tasks_are_per_parent` (gate) → **probe** | `_background_tasks_for(SimpleNamespace()) == set()` |
| 77->exit | no pending tasks → no `gather` | prompt teardown of a subagent-free session | `test_probe_wait_no_pending` (gate) | returns immediately, `gather` not called |
| 89->exit | `_register_agent_session` guard (empty id / `None`) | registry cannot be poisoned with empty keys | `test_probe_register_agent_session_guard` (gate) | `_get_agent_session("") is None` and no dict entry |
| 109, 130 | `_register_child_session`/`_take_child_sessions` empty-id guards | cascade cannot act on `""` | `test_probe_child_session_reverse_lookup` (gate) → **probe** | `_take_child_sessions("") == []`, no entries |
| 121 | `_forget_child_session` with a missing parent bucket | self-healing on inconsistent state | `test_probe_forget_without_parent_bucket` (gate) | no raise, `_child_parent` cleaned |
| 163->168 | cascade child registered with no live session | child closed by another path | `test_probe_cascade_child_without_session` (gate) | returns the id, no exception |
| 166-167 | cascade swallows a close failure | best-effort contract | `test_probe_cascade_close_failure` (gate) | close raises → cascade still returns the id |
| 188, 203, 221, 253, 256, 265 | `_get_agent_session("")`, `_cli_session_id(None)`, `_session_work_dir(None)`, `_sdk_session_by_id("")`/match, `_get_entry` | tiny defensive guards | `test_probe_identity_helpers` (gate) | each returns the documented `None`/`""` |
| 288->293 | absolute `@path` skips the base-dir join | absolute prompts must work | `test_probe_hostile_at_paths` (gate) → **probe** | missing absolute → `ToolError` |
| 305 | `_prompt_saved_message("")` | empty prompt must not create a temp file | `test_probe_prompt_saved_message_empty` (gate) | `_prompt_saved_message("") == ""` |
| 420-421 | `_consume_background_task_error` re-raise guard | task exceptions must not leak | `test_probe_consume_task_error` (gate) | cancelled task + raising task both no-op |
| 478->480, 483, 484->494, 486->494 | collector same-type runs / empty tool chunk / unknown message type | turn boundaries shape the returned history | `test_probe_collector_boundaries` (gate) | exact `turns` role/metadata sequence |
| 513->507 | `finalize_assistant_turn` skips non-text turns | only assistant text is returned | `test_probe_finalize_assistant_non_text` (gate) | `""` for tool-only turns |
| 603->611 | `context_data` without `context_files` | data-only context | `test_probe_context_data_only` (gate) | prompt has `<data>` and no `<file ` |
| 650->652 | background run resuming an existing store entry | resume bookkeeping (`created_at` reuse) | `test_probe_background_resume_existing_entry` (gate) | `created_at` preserved, state `completed` |
| 686 | `return_history` on the *immediate* background return | documented extras contract | `test_probe_background_return_history_immediate` (gate) | extras carry `conversation_history` |
| 717, 724, 738->740, 741, 743, 756->exit, 758-759 | notice: no parent id, >2000 truncation, interrupted wording, `(no text output)`, >1500 partial, steer `None`, steer exception | the whole best-effort notice contract | `test_probe_notice_edges` (gate) | exact notice strings / no raise |
| 772->exit | empty text chunk in `output_function` | collector must ignore empty chunks | `test_probe_output_function_ignores_empty` (gate) | no turn appended for `""` |
| 814->817 | error branch with an empty effective prompt (no retry hint) | empty-prompt failure must not claim a saved file | `test_probe_error_with_empty_prompt` (gate) | `"prompt_file" not in extras` |
| 843-844 | `_execute` outer `except` (defensive) | last-resort contract | `test_probe_execute_outer_except` (gate) | `is_error`, `brief == "Failed to create sub-agent session"` |
| 964->967 | sub-agent session without `custom_config` | `is_sub_agent` flag not set | `test_probe_session_without_custom_config` (gate) | no raise, run succeeds |
| 1008 | `inherit_context` with no work dir | must fail loudly | `test_probe_inherit_without_work_dir` (gate) | `ToolError` "has no work dir" |
| 1034->exit | inherited session with an empty system prompt | reset must be a no-op | `test_probe_reset_prompt_empty` (gate) | `write_system_prompt` not awaited |
| 1052, 1053->exit | `_register_agent_sessions` without a resolvable parent / empty child id | registration guards | `test_probe_register_agent_sessions_guards` (gate) | no entries created |
| store 107->106 | `list_active` skips an inactive entry | `is_active=False` must hide a session | `test_probe_list_active_excludes_inactive` (gate) | listing excludes it |
| store 138-139 | `evict_lru_if_needed` swallows a close failure | eviction must not abort | `test_probe_evict_close_failure` (gate) | eviction completes, entry gone |

**summary:** behaviours mapped 59 / covered 38 (30 by the in-scope suite + 8 more reached only via the
new probe file); uncovered lines 53 (41 already reachable from the probe file, 21 distinct behaviours
→ the proposed tests above). Waiver budget 25 → **exceeded (53)**.

## 6. G4 Behavioural probes

`tests/kimix/test_agent_gate.py` — **created by this review**, 20 test functions (required ≥ 14),
`uv run pytest tests/kimix/test_agent_gate.py -q -rs` → **20 passed, exit 0**.

| probe (§4.4 K01 item) | assertion | result |
|---|---|---|
| 1 `test_probe_prompt_task_alias_parity` | `task` == `prompt` (model_dump + JSON round-trip) | pass |
| 2 `test_probe_session_aliases_are_interchangeable` | `session`/`session_id` parity; schema advertises `session`; `agent_id`/`session`/`session_id` for `interrupt_agent` | pass |
| 3 `test_probe_at_path_resolution` | `@in-work.md`→work dir, `@in-cwd.md`→CWD, `@missing.md`→`ToolError("prompt file not found: missing.md")` | pass |
| 4 `test_probe_run_in_background_defaults_true` | default `True`; bg call returns `status="running"` before `prompt_async` is awaited | pass |
| 5 `test_probe_close_session_defaults_and_overrides` | fg default closes; bg default keeps (`completed`,`is_active`); `True` closes; fg+`False` → `continued` | pass |
| 6 `test_probe_inherit_context_copies_and_resumes` | `Session.copy(work_dir,"parent-1","inherit-1")`, `resume=True`, reset called; no parent id → `ToolError` | pass |
| 7 `test_probe_context_files_and_context_data` | exact `<file path='marker.txt'>\nhello marker\n</file>`, `<data>` with `"k": [`, block precedes task; missing file reported inline | pass |
| 8 `test_probe_return_history_all_three_formats` | `json` list of 4 / `markdown` `### Turn 1: 👤 user` / `summary` exact string / unknown → `[]`; extras type per format | pass |
| 9 `test_probe_session_dir_matches_sdk_layout` | `== _sdk_sessions_dir(work)/id`; CWD fallback | pass |
| 10 `test_probe_background_tasks_are_per_parent` | distinct per parent, memoised, SDK wrapper → same set, no-custom_data → `set()` | pass |
| 11 `test_probe_child_session_reverse_lookup` | exact `_children_by_parent`/`_child_parent`; empty ids ignored; take pops both; forget cleans | pass |
| 12 `test_probe_parent_close_cascade_deletes_child_dir` | real anonymous SDK session dir gone, registries cleaned, unknown parent → `[]` | pass |
| 13 `test_probe_failed_prompt_saves_retry_file` | `brief=="sub-agent task failed"`, `[prompt saved to …]`, `prompt=@…`, `extras["prompt_file"]`, saved content == sent prompt | pass |
| 14 `test_probe_session_id_determinism` | mid-run → exact `ToolError`; unknown id reused verbatim; `""` → uuid4 (36 chars) | pass |
| 15 `test_probe_interrupt_race_leaves_no_orphan` | close during spawn → no store entry, not resurrected, registry clean | pass |
| 16 `test_probe_prompt_size_boundary_and_empty` | empty prompt runs (documented); 100 KiB inline; +1 byte offloads | pass |
| 17 `test_probe_hostile_at_paths` | `"@"`→`ToolError`; missing absolute→`ToolError`; `@../secret.md` escapes the work dir (**finding e**) | pass |
| 18 `test_probe_wrong_types_and_schema_roundtrip` | 6 `ValidationError`s, 2 lax coercions, exact field set, `model_validate(model_dump())` identity | pass |
| 19 `test_probe_recursive_guard_and_companions` | exact guard message/brief; `list_agents` both scopes `[]`, then the real entry | pass |
| 20 `test_probe_background_defensive_error_and_double_close` | `_run_background` defensive `ToolError`; unknown/re-close `interrupt_agent` → `"Session not found"` | pass |

**summary:** required 14 / present 20; params-field coverage 10/10 (`description`, `prompt`,
`run_in_background`, `session_id`, `close_session`, `return_history`, `history_format`,
`context_files`, `context_data`, `inherit_context`); alias coverage 4/4 (`task`, `session`,
`agent_id`, `session_id`); boundary coverage: empty prompt, 100 KiB at/above, uuid, absent
`session_id`, all 3 `history_format` values; error paths: **5 of 6** `ToolError` return sites reached
(`__call__` guard, `__call__` except, `_execute` error branch, `_run_background` defensive,
`AgentClose` not-found). Not reached: the `_execute` outer `except` (defensive, unreachable without
patching) — a probe is proposed in §5.

## 7. G5/G6 Evidence

All raw output: `reviews/tools/gates/K01.txt` (line 1 = `git rev-parse HEAD`, lines 2–14 =
review-start `git status --porcelain`). Sections: `[G1-metrics] … [G1-vulture]`, `[G3-verbatim]`,
`[G3-dotted]`, `[G3+probes]`, `[G4-count]`, `[G4-pytest]`, `[G5-root]`, `[G5-cli]`, `[G5-syntax]`,
`[G5-git-diff-unit]`, `[P]`, `[H]`.

| command | result |
|---|---|
| `uv run tools/review_metrics.py <2 files> --json` | exit 0; `module_loc` 1318, 58 functions, `longest_fn=_execute` 88 |
| `… --max-fn-loc 120 --max-complexity 12` | **exit 1**; breaches: `_notify_parent_background_finished complexity=19>12`, `_format_history complexity=14>12` |
| `uv run ruff check <2 files>` | **exit 1**; 4 errors (F841 `__init__.py:584`; F401 `store.py:3`, `store.py:4`; I001 `store.py:1`) |
| `uv run ruff check --select C901 --config lint.mccabe.max-complexity=12 <2 files>` | exit 0 ("All checks passed!") |
| `uv run --with vulture vulture <2 files> --min-confidence 90 --sort-by-size` | exit 3; 1 hit (`store.py:5` unused `Callable`) → waived FP |
| `uv run tools/git_diff.py <2 files>` | exit 0, "no uncommitted changes" |
| G3 verbatim | exit 0; TOTAL 412/89.42 %; **no `agent/__init__.py` row** (`module-not-imported`) |
| G3 dotted | exit 0; `agent/__init__.py` 87 %, `store.py` 97 %, TOTAL 87.66 %, 114 passed |
| G3 + probes | exit 0; 92 % / 97 %, TOTAL 93 %, 134 passed |
| G4 | 20 probes, 20 passed |
| `uv run pytest tests -q` | **exit 2** — PRE-EXISTING collection block: `tests/test_bash_e2e.py` `ValueError: cannot read corpus file tests\bash\cases.json` (baseline `00-baseline.txt:3-33`, `ROOT_SUITE_EXIT=2`) |
| `uv run pytest kimi-cli/tests -q` | exit 1 — `43 failed, 5215 passed, 105 skipped`. Set-compared against the baseline: **0 new, 0 fixed** (identical 43: 39×`test_grep_engine_parity`, 2×`test_read_file`, 1×`test_session_logging`, 2×`test_toolset_todo_redirects`) |
| `uv run tools/syntax_check.py tests/kimix/test_agent_gate.py` | exit 0, "Syntax OK" |
| `uv run tools/git_diff.py tests/kimix/test_agent_gate.py` | exit 0, diff is exactly the new file |

**G6 (orchestrator):** `tools/review_gates.py --all` independently produced
`reviews/tools/gates/K01.json` for the same SHA with the same numbers —
`ruff=fail c901=pass metrics=fail cx_max=19 longest=88 pytest=pass cov=pass`; compare against
`complexity_max=19`, `longest_fn_loc=88`, coverage 87.66 %, probes 20.
⚠ Infra note (not a K01 source finding): that harness's `K01.json` `sha` field is polluted with the
shell wrapper's own marker (`"52192b7c…\n[elapsed 0.0s, rc=0]"`), which would break a literal SHA
comparison in G6 — the harness's `git rev-parse HEAD` capture needs the same isolation as the
reviewer's.

**HEAD moved during the review.** Three orchestrator commits landed while this review ran:
`72257564 chore(review): per-tool gate harness, report generator, P1 sweep evidence`,
`37850165 fix(FP-08): resolve lint-blocking correctness defects in the tool trees` and
`7aa84025 review(P2+P3): 36 per-tool reports, coverage gaps, simplicity backlog`. The gate evidence
is bound to the review-time SHA `52192b7c…` (line 1). To keep G6 decidable, the `[RE-VERIFY @ 7aa84025]`
block appended to the evidence file re-ran the mechanical gates at the new HEAD and produced
**identical** numbers (ruff 4 errors; `complexity_max=19`, `longest_fn_loc=88`, `module_loc` 1318;
coverage 87.66 % with the same 53-line `Missing` list; probes 20 passed) — and
`git diff --stat 52192b7c… HEAD -- src/kimix/tools/agent/__init__.py src/kimix/tools/agent/store.py`
is **empty**, i.e. the measured modules are byte-identical across the two revisions. FP-08 did not
touch K01's files (it fixed `read_media.py`, `pwsh_tool.py`, `run.py`, `conflict_detect.py`).
A live fix phase for K01 therefore still has the full list in §3/§4.

## 8. Git provenance

- `git rev-parse HEAD` at review time — **52192b7c3a1baa66c96e0015d2868f3b106fc043**, identical to line 1 of `reviews/tools/gates/K01.txt`.
- HEAD at hand-off — **7aa84025422d1a61c34e7d8e11bca524f347a0e0** (orchestrator commits `72257564`, `37850165` (FP-08), `7aa84025` landed during the review). The in-scope modules are byte-identical between the two revisions (`git diff --stat` empty, re-verified in the `[RE-VERIFY @ 7aa84025]` block), so every number in this report holds at HEAD.
- `git status --porcelain` at review start — 13 untracked sibling-unit artifacts (`.c08tmp/`, `reviews/tools/gates/{C03,C08,C09,C10,K01,K04,X01..X06}.txt`); **no modified tracked files** (lines 2–14 of the evidence file). While the review ran, the orchestrator committed the harness/report-generator, FP-08 and the P2/P3 aggregate; sibling units also modified tracked files (`kimi-cli/src/kimi_cli/tools/file/conflict_detect.py`, `read_media.py`, `src/kimix/tools/common.py`, `file/bash/bash_tool.py`, `file/bash/pwsh_tool.py`, `file/run.py`, `py/__init__.py`). **Nothing in K01's scope was modified by anyone.**
- This unit's artifacts (uncommitted at hand-off): `reviews/tools/K01-subagent-agent.md` (M — overwrites the generator's stub committed in `7aa84025`), `reviews/tools/gates/K01.txt` (M), `tests/kimix/test_agent_gate.py` (new, untracked).
- commit SHA (filled in by the orchestrator after acceptance): …
- tag: `tools-review/p2-w1`
- `git diff --stat tools-review/p0-base HEAD -- src/kimix/tools/agent/__init__.py src/kimix/tools/agent/store.py` → **empty** (both in-scope sources are untouched since the base tag; this review is read-only on them).

## 9. Duplication / native-parity / policy notes

- **Native parity / G-DUP:** tool name `subagent` has two implementations — `kimix.tools.agent:Agent` (K01, `run_in_background` default **True**) and `kimi_cli.tools.agent:AgentTool` (C01, default **False**). No delegating shim exists between them; they are independent surfaces. Probe 4 (K01) + C01 probe 5 pin the divergence. G-DUP/G7 own the finding.
- **Shared helpers (primary owner = K01):** `agent/store.py` (`AgentSessionStore`, `AgentSessionEntry`, `ConversationTurn`) is also used by K02/K03. `_AgentConversationCollector` is imported by `kimix/tools/swarm/__init__.py:21`; `wait_for_background_agents` by `kimix/utils/prompt.py:752` — so neither is dead despite low in-file usage.
- **Duplicated logic inside K01 (F16):** the 5-step child-teardown sequence appears 3× (`agent/__init__.py:137-141`, `818-822`, `1066-1071`) → extract one helper.
- **Library policy (report only, X01 owns):** compliant — `orjson` used (L10, L1130); no stdlib counterpart in the policed set is used.
- **Test hygiene:** every test in the 5 in-scope files and in the new probe file contains ≥ 1 real assertion; no skips/xfails in scope.

## 10. Verdict + JSON

**Verdict: major** (G1 and G3 fail; the fixes are small and mechanical — 4 ruff edits, one function
split, one dead-registry deletion, and the G3 command/waiver correction). G0/G2/G4/G5 pass.

```json
{
  "id": "K01",
  "tool": "subagent",
  "files": ["src/kimix/tools/agent/__init__.py", "src/kimix/tools/agent/store.py"],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {"ruff": "fail", "complexity_max": 19, "longest_fn_loc": 88,
      "loc_regression": "none", "vulture_hits": 1, "vulture_waived": 1},
    "G2_risk": {"items_answered": 13, "open_high": 0, "open_medium": 4, "waived_medium": 0},
    "G3_coverage": {"module": "kimix.tools.agent", "floor": 85,
      "actual": 87.66, "waived_lines": 53, "waiver_budget": 25},
    "G4_behavior": {"probe_file": "tests/kimix/test_agent_gate.py",
      "probes_required": 14, "probes_present": 20, "result": "pass"},
    "G5_regression": {"root_suite": "fail-preexisting", "cli_suite": "fail-preexisting-43",
      "syntax_check": "pass", "git_diff": "pass"},
    "G6_independent_rerun": "pass-selfcheck"
  },
  "findings": [
    {"severity": "low", "axis": "simplicity", "title": "ruff fails: unused var, unused imports, unsorted import block", "file": "src/kimix/tools/agent/__init__.py:584", "evidence": "ruff exit 1: F841 __init__.py:584; F401 store.py:3,4; I001 store.py:1", "impact": "G1 lint gate fails", "fix": "delete `entry = store.get(session_id)`; drop `import asyncio`/`import time`; sort the import block", "test_to_add": "gate re-run: uv run ruff check src/kimix/tools/agent/__init__.py src/kimix/tools/agent/store.py (expect exit 0)"},
    {"severity": "medium", "axis": "simplicity", "title": "_notify_parent_background_finished complexity 19 (>15 hard fail)", "file": "src/kimix/tools/agent/__init__.py:707", "evidence": "review_metrics: complexity=19, branches=18, loc=53", "impact": "G1 metric hard fail; notice logic hard to test", "fix": "split into _interrupted_notice/_completion_notice + _deliver_notice", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_notice_edges"},
    {"severity": "low", "axis": "simplicity", "title": "_format_history complexity 14 (>12)", "file": "src/kimix/tools/agent/__init__.py:877", "evidence": "review_metrics: complexity=14, branches=13", "impact": "G1 metric breach", "fix": "dispatch through a small formatter dict", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_return_history_all_three_formats"},
    {"severity": "medium", "axis": "simplicity", "title": "dead write-only registry _agent_entries/_get_entry", "file": "src/kimix/tools/agent/__init__.py:31", "evidence": "_get_entry (L264) has zero call sites repo-wide; only writers are L260/269", "impact": "dead state + maintenance cost; misleading for readers", "fix": "delete _agent_entries and its 3 helpers plus their 6 call sites", "test_to_add": "tests/kimix/test_agent_conversation.py::test_store_get_put"},
    {"severity": "medium", "axis": "risk", "title": "@path/context_files read outside the session work dir (no containment)", "file": "src/kimix/tools/agent/__init__.py:283", "evidence": "probe 17: prompt='@../secret.md' reads TOP SECRET; context_files L606-607 joins base_dir with no check", "impact": "inconsistent with the read tool's containment; a model-authored path can exfiltrate any readable file into the subagent prompt", "fix": "reject paths resolving outside base_dir (reuse the read tool's guard)", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_hostile_at_paths"},
    {"severity": "medium", "axis": "coverage", "title": "G3 waiver budget exceeded + verbatim --cov path form unmeasured", "file": "reviews/tools/gates/K01.txt", "evidence": "waived_lines=53 > budget 25; verbatim run: CoverageWarning module-not-imported, no row for agent/__init__.py", "impact": "the gate as written cannot detect a regression in the primary 1179-LOC module", "fix": "use --cov=kimix.tools.agent + add tests/kimix/test_agent_gate.py to the G3 test list (53→26 waivers, 87.66%→93%)", "test_to_add": "tests/kimix/test_agent_gate.py"},
    {"severity": "low", "axis": "risk", "title": "blocking file IO inside async def (no to_thread)", "file": "src/kimix/tools/agent/__init__.py:295", "evidence": "Path.read_text at L295/L607 and _create_script_file at L594 are sync calls in coroutines", "impact": "a large @file stalls the whole event loop for every session", "fix": "await asyncio.to_thread(...)", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_at_path_resolution"},
    {"severity": "medium", "axis": "risk", "title": "session_id alias 'session' drifts from the documented param name", "file": "src/kimix/tools/agent/__init__.py:340", "evidence": "JSON schema property is 'session'; tool description tells the model to pass session_id (L529/L1124)", "impact": "model-facing docs/schema disagree; report-sync gate does not cover this field", "fix": "validation_alias=AliasChoices('session_id','session')", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_session_aliases_are_interchangeable"},
    {"severity": "low", "axis": "risk", "title": "empty prompt accepted (no min_length) -> promptless subagent", "file": "src/kimix/tools/agent/__init__.py:323", "evidence": "probe 16: SubAgentParams(prompt='') runs and prompt_async receives ''", "impact": "wasted background session + misleading 'completed' status", "fix": "Field(min_length=1) or an explicit ToolError", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_prompt_size_boundary_and_empty"},
    {"severity": "low", "axis": "simplicity", "title": "misleading brief on post-creation failure", "file": "src/kimix/tools/agent/__init__.py:843", "evidence": "_execute's outer except returns brief='Failed to create sub-agent session' although the session exists", "impact": "wrong operator/model signal", "fix": "use a distinct brief (e.g. 'Sub-agent run failed')", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_execute_outer_except"},
    {"severity": "low", "axis": "simplicity", "title": "dead params/fields: description, **extra, scope", "file": "src/kimix/tools/agent/__init__.py:317", "evidence": "params.description never read; _build_extras(**extra) never passed (L811/825); AgentListParams.scope never read (L1127)", "impact": "model spends tokens on unusable params", "fix": "surface description in the notice or deprecate; drop **extra", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_recursive_guard_and_companions"},
    {"severity": "low", "axis": "simplicity", "title": "fragile re-parse of the saved-prompt message", "file": "src/kimix/tools/agent/__init__.py:815", "evidence": "saved_suffix.split('[prompt saved to ',1)[1].split(']',1)[0] reconstructs a path from prose", "impact": "breaks silently if the message format changes", "fix": "have _prompt_saved_message return (message, path)", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_failed_prompt_saves_retry_file"},
    {"severity": "low", "axis": "simplicity", "title": "duplicated 5-step child teardown in 3 places", "file": "src/kimix/tools/agent/__init__.py:818", "evidence": "L137-141, L818-822, L1066-1071 are the same close/unregister sequence", "impact": "one path can drift from another", "fix": "extract _release_child(session_id, session)", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_background_defensive_error_and_double_close"},
    {"severity": "low", "axis": "simplicity", "title": "unnamed magic numbers", "file": "src/kimix/tools/agent/__init__.py:593", "evidence": "100*1024 (L593), 2000 (L723), 1500 (L742), Semaphore(8) (L541) are inline literals", "impact": "limits are undocumented upstream", "fix": "name them", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_prompt_size_boundary_and_empty"},
    {"severity": "info", "axis": "risk", "title": "child-prompt delimiter can be closed by hostile context_data", "file": "src/kimix/tools/agent/__init__.py:613", "evidence": "<data>/<file> blocks interpolate raw caller text", "impact": "prompt-structure injection inside the child prompt", "fix": "nonce the delimiters or strip closing tags", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_context_files_and_context_data"},
    {"severity": "info", "axis": "risk", "title": "evict_lru_if_needed swallows close failures silently", "file": "src/kimix/tools/agent/store.py:136", "evidence": "except Exception: pass around close_session_async", "impact": "a failed close leaves an anonymous dir behind with no signal", "fix": "log at debug", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_evict_close_failure"},
    {"severity": "info", "axis": "simplicity", "title": "hot-path shadow import of orjson", "file": "src/kimix/tools/agent/__init__.py:612", "evidence": "import orjson as _orjson inside _prepare_run while L10 already imports orjson", "impact": "noise; both names refer to the same module", "fix": "use the module-level orjson", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_context_files_and_context_data"},
    {"severity": "info", "axis": "risk", "title": "unbounded successful output (no cap on subagent text/history)", "file": "src/kimix/tools/agent/__init__.py:794", "evidence": "output_text is returned verbatim; conversation_history is unbounded", "impact": "a chatty subagent can blow the parent context on the next turn", "fix": "cap or document", "test_to_add": "tests/kimix/test_agent_gate.py::test_probe_return_history_all_three_formats"}
  ],
  "coverage": {"behaviors_mapped": 59, "covered": 38, "gaps": 21,
    "gap_list": ["wait_for_background_agents no-pending path", "_register_agent_session guard",
      "_forget_child_session without parent bucket", "cascade child without live session",
      "cascade close failure swallowed", "identity helpers guards (_get_agent_session(''), _cli_session_id(None), _session_work_dir(None), _sdk_session_by_id)",
      "collector same-type/empty-chunk/unknown-type boundaries", "_prompt_saved_message('')",
      "_consume_background_task_error guard", "context_data-without-context_files block",
      "background resume of an existing entry + immediate return_history", "notice edges (no parent id, truncations, interrupted wording, steer None/raise)",
      "output_function empty chunk", "error branch without retry hint", "_execute outer except",
      "session without custom_config", "inherit_context without work dir", "reset prompt empty",
      "_register_agent_sessions guards", "store list_active inactive skip", "store evict close failure"]},
  "gate_evidence": "reviews/tools/gates/K01.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "head_at_handoff": "7aa84025422d1a61c34e7d8e11bca524f347a0e0",
  "in_scope_modules_identical_between_sha_and_head": true,
  "worktree_clean_at_start": true,
  "needs_fix_phase": true,
  "duplication_notes": [
    "child-teardown sequence duplicated 3x (agent/__init__.py:137-141, 818-822, 1066-1071)",
    "tool name 'subagent' implemented twice (kimix.tools.agent:Agent vs kimi_cli.tools.agent:AgentTool) - G-DUP/G7 own it",
    "agent/store.py is K01-owned shared infra for K02/K03; _AgentConversationCollector is shared with kimix/tools/swarm"
  ]
}
```
