# X02 — async-resources

## 0. Method

Frozen commit: `52192b7c3a1baa66c96e0015d2868f3b106fc043`. Worktree state at start (line 2 of the
evidence file): only this sweep's own artifacts untracked (`?? reviews/tools/gates/X01.txt`,
`?? reviews/tools/gates/X02.txt`) — clean for the in-scope source trees.

Scope: `kimi-cli/src/kimi_cli/tools/**` and `src/kimix/tools/**`, plus the tests that pin them under
`kimi-cli/tests/**` and `tests/**`.

Every command below was executed once and its raw output appended to `reviews/tools/gates/X02.txt`
(that file is the sole evidence base for this report; nothing here was re-run and no source beyond
those recorded outputs was read):

1. `git rev-parse HEAD; git status --porcelain` — binds the sweep to the SHA above (file lines 1–3).
2. `ls src/kimix/tools/eval/ && find src/kimix/tools/eval -name '*.py'` and
   `git ls-files src/kimix/tools/eval` — stale-bytecode / leftover check.
3. `git ls-files 'src/kimix/tools/**' | wc -l` → 31 tracked modules.
4. `grep -rn --include='*.py' 'async def' kimi-cli/src/kimi_cli/tools src/kimix/tools | wc -l` → 229,
   plus the full `async def` listing (defines the coroutine surface audited in (a)).
5. `grep -rn --include='*.py' 'create_task\|ensure_future\|TaskGroup' …` — task-creation inventory (c).
6. `grep -rn --include='*.py' 'subprocess' …` — subprocess inventory (d).
7. `grep -rn --include='*.py' -E 'time\.sleep|requests\.|import requests|urllib\.request|urlopen|sqlite3\.connect|apsw\.Connection|os\.walk|os\.listdir|shutil\.(copy|move|rmtree|make_archive)' …`
   — blocking-call inventory (a)/(e).
8. `grep -rn --include='*.py' -E 'to_thread|run_sync|anyio\.' …` — offload inventory, used as the
   negative control for (a)/(b)/(f).
9. Test inventory: `ls -d kimi-cli/tests/tools tests/unit/tools tests`, the three named test files,
   and `find kimi-cli/tests -name '*.py'` (266 files; 40 in `kimi-cli/tests/tools`).
10. Gate run: `uv run pytest kimi-cli/tests/tools tests/unit/tools -q -k 'async or background or stream'`
    → **2 failed, 1125 passed, 14 skipped, 1089 deselected, EXIT=1**.

Dimensions checked (the seven items required by the sweep): **(a)** blocking IO inside `async def`
without `to_thread`/`run_sync`; **(b)** anyio-vs-asyncio mixing in one call path; **(c)** task
lifetime (awaited / cancelled / reaped vs fire-and-forget); **(d)** subprocess timeout + kill +
`wait` + stdin close + pipe drain; **(e)** temp-file cleanup on success and error paths;
**(f)** file-handle/sqlite-connection scoping; **(g)** cancellation swallowed by `except`/bare
`except`. Each dimension maps to a finding or an explicit "checked, clean" row in §2 — including the
two rows where the recorded evidence is insufficient to claim clean, which are marked as gaps
rather than silently passed.

Limitation stated up front: items (a)–(f) below are grounded in the recorded **grep inventories**
(module, line, call shape) and the recorded test run. Where the surrounding control flow was not
captured in the evidence file, the row says so and the classification is labelled a hypothesis; no
call-graph claim in §1 is asserted as proven beyond the cited lines.

## 1. Findings

| severity | title | file:line | evidence | impact | fix | test_to_add |
|---|---|---|---|---|---|---|
| medium | FindStr walks the filesystem on the event loop (os.walk / os.listdir, no to_thread) | src/kimix/tools/file/find_str.py:32 (async `__call__`); blocking calls at :78 os.walk, :87 os.listdir, :98 os.listdir | Blocking-call grep records `os.walk` at find_str.py:78 and `os.listdir` at :87 and :98; the same module's coroutine entry is `async def __call__(self, params: FindStrParams)` at :32. `find_str.py` does **not** appear anywhere in the `to_thread\|run_sync\|anyio\.` inventory, i.e. none of its sites is offloaded. (Call-graph hypothesis: the walk/listdir sites are executed from the coroutine; the grep proves the calls and their module, not the exact nesting.) | A recursive `os.walk` plus two `os.listdir` sweeps over a large tree runs synchronously inside the tool coroutine, so the whole event loop — including the streaming stdout/stderr readers used by concurrent subprocess tools (see src/kimix/tools/common.py:2109–2113) — is stalled for the full scan, with no timeout and no progress yield. | Move the enumeration into `await anyio.to_thread.run_sync(...)` (or `asyncio.to_thread`) and return only the matched paths to the coroutine; bound the scan with a deadline and a max-entries cap so a pathological tree cannot pin a worker forever. | tests/unit/tools/test_find_str.py (or `native/test_behavior_equivalence.py`, which already touches FindStr): add a liveness probe that runs a concurrent ticker coroutine and asserts it advances while FindStr scans a synthetic deep tree, plus a large-tree timeout/boundary test. |
| medium | Workflow/best-of-N runs git subprocesses and whole-workspace copies on the event loop | src/kimix/tools/swarm/best_of_n.py:87, :114, :132, :168, :177, :225, :233 (`subprocess.run`), :124 & :250 (`shutil.copytree`), :140 & :142 (`shutil.rmtree`), :150 (`os.walk`), :246 (`shutil.copyfile`); async entry points :258 `run_parallel_sample`, :317 `select_best_candidate`, :370 `best_of_n` | `subprocess` grep lists seven synchronous `subprocess.run` call sites in this one module (:87, :114, :132, :168, :177, :225, :233); the blocking-call grep lists `shutil.copytree` :124/:250, `shutil.rmtree` :140/:142, `os.walk` :150, `shutil.copyfile` :246; the `async def` listing shows the module's entry points at :258/:317/:370. `best_of_n.py` has **no** entry in the `to_thread\|run_sync\|anyio\.` inventory. | candidate/worker-setup, `git diff`/`git status` inspection and the winner copy-back (main work dir) all execute synchronously inside async orchestration; with k parallel samples (the parallel-sample path) this serializes on the loop and freezes streaming/tool I/O for the whole sample+diff cycle. Same class as X01-F3 (this module is already flagged for the difflib policy hit), so one change set can address both. | Offload the sync `subprocess.run`/`shutil.*`/`os.walk` calls with `anyio.to_thread.run_sync`; prefer `asyncio.create_subprocess_exec` for the git probes so they are awaitable and cancellable; keep `shutil` copy/cleanup inside the worker thread. | tests/test_best_of_n.py: event-loop-liveness test (ticker advances while N samples run) + a cancellation test asserting the copied worker dirs are removed when the task group is cancelled mid-run. |
| low | Temp-folder cleanup retries `time.sleep` synchronously between rmtree attempts | src/kimix/tools/common.py:577 (`time.sleep(delay)`; retry loop around `shutil.rmtree(path)` at :570) | Blocking-call grep records `shutil.rmtree` :570 and `time.sleep` :577 in the same retry helper (the preceding comment at :564–566 explains the "file held for a moment" case a single rmtree fails on). The module appears in the to_thread inventory only via `anyio.open_file` (:723), not for this helper. | On Windows contention (a just-exited subprocess still holding a handle) the cleanup path blocks the loop for each retry delay instead of yielding; the delay is small but the retries are un-bounded in count in the recorded snippet, so a permanently locked path can stall the loop repeatedly. | Run the whole remove-with-retry helper under `anyio.to_thread.run_sync` (the sleep then happens off-loop), or `await asyncio.sleep(delay)` if the helper is reached from an `async` frame; cap retries and return the last error instead of spinning. | tests/test_temp_folder_cleanup.py (already exists and pins this area): assert that cleaning a directory held open by another handle still dismisses without blocking a concurrently scheduled coroutine, and that the retry count is bounded. |
| low | `node --check` parse check runs `subprocess.run` without an offload | kimi-cli/src/kimi_cli/tools/file/parse_check.py:93 (`subprocess.run`; `subprocess.TimeoutExpired` handled at :101) | `subprocess` grep records the checker at :68 (docstring: optional `node --check` subprocess checker) and `subprocess.run(...)` at :93 with `except (OSError, subprocess.TimeoutExpired)` at :101. `parse_check.py` does **not** appear in the `to_thread\|run_sync\|anyio\.` inventory. (Hypothesis: it is invoked from the write/edit parse-repair flow, which is async; the recorded evidence proves only that the call is synchronous and un-offloaded.) | A timeout is present, so this cannot hang forever, but the node process latency (tens–hundreds of ms per file, more on cold start) is spent on the loop for every checked JS/TS write/edit, delaying every other coroutine. | Wrap the `subprocess.run` in `anyio.to_thread.run_sync`/`asyncio.to_thread` (the timeout still applies inside the thread), or use `asyncio.create_subprocess_exec` with `wait_for`. | kimi-cli/tests/tools/test_parse_check.py (exists): liveness assertion that a concurrent coroutine progresses during the check, plus an assertion that the timeout still returns the documented "cannot prove a parse failure" result. |

Note on how §1 maps to the seven dimensions: (a) is covered by X02-F1/F2/F4 (blocking in async),
(c) is a checked-clean waiver, (d) is split — the *async* subprocess paths are checked-clean, the
*sync* ones are X02-F2/F4, (e) is X02-F3 plus a waiver with a stated gap, (f) is a checked-clean
waiver, (b) is a checked-clean waiver.

## 2. Waivers / non-findings

| item | why it is not a finding |
|---|---|
| (a) remaining blocking sites: kimi-cli/src/kimi_cli/tools/file/glob.py:324 (`os.walk`), kimi-cli/src/kimi_cli/tools/file/grep_local.py:2245 (`os.walk`), src/kimix/tools/file/__init__.py:49 (`shutil.rmtree` inside async `Rm.__call__:43`) | Checked, clean at the evidence level: glob.py already offloads its gitignore scan (`await asyncio.to_thread(...)` at :563) and grep_local.py offloads mtime probes (`:772`), so these are partial-offload modules where the remaining walk may sit in a helper; `src/kimix/tools/file/__init__.py` deliberately routes the remove through `await anyio.to_thread.run_sync(_remove)` at :54. The recorded greps cannot prove these individual lines execute on the loop, so they are recorded as **same-class candidates** for the X02-F1 fix rather than separate findings (no double counting). |
| (b) anyio vs asyncio | Checked, clean. The offload inventory shows anyio adoption in `src/kimix/tools/{common.py:723, background/__init__.py:306,428,454, file/run.py:516, file/__init__.py:22,54, note/__init__.py:63,142,147,469, py/__init__.py:554}` and `kimi-cli/.../read_media.py:562`; direct asyncio usage is widespread too (`asyncio.create_task` in common.py:2109–2113 and grep_local.py:1128/1136, `asyncio.to_thread` in kimi-cli file/edit/read/web helpers). No recorded site shows both frameworks bound in a *single call path* (e.g. an `asyncio.create_task` spawned inside an anyio task group); the closest overlap is common.py, where `asyncio.create_task` (process-output readers) and `anyio.open_file` (:723) live in different functions of one module. Flagged only as a module-level coexistence observation. |
| (c) task lifetime | Checked, clean (inventory level). Every `create_task`/`ensure_future`/handle site is accounted for: `grep_local.py:1128` `self._rg_path_task = …` and `:1136` `self._rtk_path_task = …` store the handle on the tool instance and the await path exists at `:1138` `_resolve_rtk_path`; `common.py:2109/2112/2113` store `stdout_task`/`stderr_task`/`stdin_task` for the subprocess reader join; `background/__init__.py:126` schedules `discard_all_tasks(session)` through `ensure_future` on the session-closed hook; `src/kimix/tools/agent/__init__.py:665` `task = asyncio.create_task(self._run_background(prepared))` has companion supervision (`wait_for_background_agents` :67, `_notify_parent_background_finished` :707, and `background/utils.py` `join_task` :599 / `discard_all_tasks` :619). No fire-and-forget task is provable from the recorded evidence; the :665 handle is the one worth re-confirming when its body is next touched. |
| (d) async subprocess paths (timeout / kill / wait / stdin / drain) | Checked, clean. Timeout+kill paths exist where async subprocesses are used: `grep_local.py:567 _kill_process` (used by the rg subprocess at :1327–:1337 with `params.timeout`), `bash/bash_tool.py:1159 _stop_after_timeout`, `bash/pwsh_tool.py:846 _stop_after_timeout`; `common.py` creates the process with `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` (:1988) and drains all three pipes with dedicated tasks (`read_stdout` :2016, `read_stderr` :2051, `write_stdin` :2091, tasks created :2109–2113, elapsed accounting `_subprocess_elapsed` :1060). The synchronous `subprocess.run` sites are handled as findings X02-F2 (best_of_n) and X02-F4 (parse_check), not here. |
| (e) temp files: cleanup on success and error paths | Checked at inventory level — but **with a verification gap**: the cleanup helper and the asyncio temp-export helpers exist (`common.py:564–577` remove-with-retry; `_export_to_temp_file_async` :700; `_maybe_export_output_async` :823; `_save_original_output_async` :1707), and the pinning test `tests/test_temp_folder_cleanup.py` is present in the recorded test inventory. The sweep prompt's second command (`uv run pytest tests/test_tools_async.py tests/test_stream_repeat_output.py tests/test_temp_folder_cleanup.py -q`) is **not present in gates/X02.txt**, so "cleaned up on the error path" is not proven by this evidence file — the remaining blocking-sleep issue is X02-F3. Backlog: run that command and append the output before claiming (e) fully clean. |
| (f) file handles / sqlite connections | Checked, clean with one residual: every recorded `open` site uses a scoped async context manager — `async with await anyio.open_file(...)` at `common.py:723`, `background/__init__.py:306,428,454`, `file/run.py:516`, `note/__init__.py:65,68,178,227`, `py/__init__.py:554`, `read_media.py:562` — so no bare `open` is visible in the evidence. SQLite: `kimi-cli/.../read_sqlite.py` builds read-only connections via `open_read_connection` (:84, `apsw.Connection` :96/:101) and the async entry `read_sqlite` (:480) wraps the sync work in `asyncio.to_thread` (:491). Residual: the evidence does not show an explicit `conn.close()`/context-manager for those `apsw.Connection` objects — record as an open question, not a proven leak. |
| (g) cancellation: `except Exception` / bare `except` swallowing `CancelledError` | **Not verified — evidence gap, cannot claim clean.** The required scan for `except Exception`/bare `except`/`except BaseException` (and thus for `CancelledError` swallowing) was never run into `gates/X02.txt`; the only related lines are the narrow `except (OSError, subprocess.TimeoutExpired)` in parse_check/pwsh/bash, which do not catch `BaseException`. Backlog item for the owning follow-up: run the bare-except/CancelledError grep over both trees, append to the evidence file, and classify each hit. |
| Gate failures from the recorded pytest run (2 tests) | Not an async-resource defect: `kimi-cli/tests/tools/test_read_file.py::test_read_with_relative_path_outside_work_dir` (:243) and `TestReadFileGlob::test_read_glob_outside_workspace_relative` (:1379) both assert `"absolute path" in result.message.lower()` but the tools return `` `../…` does not exist.`` / `Directory for `../…` does not exist.`` The failures are an error-message-contract mismatch in the read/glob path, and the sweep prompt documents that 43 kimi-cli failures pre-exist — consistent with these being baseline failures. Owned by C05/C07, recorded here only because they make this sweep's gate red (§3). |
| Orphan bytecode under src/kimix/tools/eval/ | Not an async-resource issue: `src/kimix/tools/eval/` contains only `__pycache__` and empty-ish `runners/{julia,node,python,ruby}` holding a single `__pycache__/runner.cpython-314.pyc`, while `git ls-files src/kimix/tools/eval` returns nothing (all `.py` sources already deleted). Stale-artifact/dead-code concern owned by X05, recorded here only as an observation. |

## 3. Gate result

**FAIL (blocking).** The sweep's gate is the prescribed test run; raw output is in
`reviews/tools/gates/X02.txt` (final section):

```
uv run pytest kimi-cli/tests/tools tests/unit/tools -q -k 'async or background or stream'
...
2 failed, 1125 passed, 14 skipped, 1089 deselected in 41.72s
EXIT=1
```

- Red rows: `kimi-cli/tests/tools/test_read_file.py::test_read_with_relative_path_outside_work_dir`
  and `kimi-cli/tests/tools/test_read_file.py::TestReadFileGlob::test_read_glob_outside_workspace_relative`
  — both fail on the assertion `"absolute path" in result.message.lower()` while the tools return a
  "does not exist" message. This is the only pass/fail helper for this sweep; no G-POLICY-style AST
  gate is defined for X02.
- The grep inventories (§0 items 4–8) are evidence collectors, not pass/fail gates; they are green
  (they ran and were captured) and produced findings X02-F1…F4.
- The second prescribed command (`uv run pytest tests/test_tools_async.py
  tests/test_stream_repeat_output.py tests/test_temp_folder_cleanup.py -q`) is absent from the
  evidence file; the temp-file dimension (e) is therefore only partially verified (§2).
- Verdict: **major** — the gate exits non-zero and the sweep leaves four (2 medium) un-offloaded
  blocking-I/O findings plus one dimension ((g)) unverified. None of the findings is a data-loss or
  hang hazard on its own, so this is not "blocked".
- Exit criteria for this unit: (1) X02-F1…F4 fixed (offload or async-subprocess rewrite, no
  behavioural delta for the tool result payloads), (2) item (g) scan run and appended to the evidence
  file with each hit either waived in writing or fixed, (3) the temp-cleanup command run and appended,
  (4) the 2 read_file failures either fixed with the message contract restored or re-classified as
  documented baseline by their owning units, and (5) a green re-run of the sweep's pytest gate.

## 4. JSON summary

```json
{
  "id": "X02",
  "sweep": "async-resources",
  "scope_dirs": ["kimi-cli/src/kimi_cli/tools", "src/kimix/tools"],
  "verdict": "major",
  "findings": [
    {"severity": "medium", "title": "FindStr walks the filesystem on the event loop (os.walk / os.listdir, no to_thread)", "file": "src/kimix/tools/file/find_str.py:32", "evidence": "blocking grep: os.walk :78, os.listdir :87, :98; async __call__ :32; module absent from the to_thread/run_sync inventory", "impact": "synchronous recursive walk on the loop stalls all concurrent coroutines for the whole scan, no timeout", "fix": "wrap the enumeration in anyio.to_thread.run_sync / asyncio.to_thread, bound entries + deadline", "test_to_add": "tests/unit/tools/test_find_str.py::test_walk_does_not_block_event_loop"},
    {"severity": "medium", "title": "Workflow/best-of-N runs git subprocesses and workspace copies on the event loop", "file": "src/kimix/tools/swarm/best_of_n.py:87", "evidence": "subprocess.run :87,:114,:132,:168,:177,:225,:233; shutil.copytree :124,:250; rmtree :140,:142; os.walk :150; copyfile :246; async entry points :258,:317,:370; module absent from the to_thread/run_sync inventory", "impact": "git probes + full worker copies serialize on the loop; parallel samples freeze streaming/tool I/O", "fix": "offload via anyio.to_thread.run_sync; use asyncio.create_subprocess_exec for git probes", "test_to_add": "tests/test_best_of_n.py::test_parallel_sample_keeps_loop_responsive"},
    {"severity": "low", "title": "Temp-folder cleanup retries time.sleep synchronously between rmtree attempts", "file": "src/kimix/tools/common.py:577", "evidence": "shutil.rmtree :570 retried with time.sleep(delay) :577 in the same helper; no to_thread entry for this helper", "impact": "locked-path cleanup blocks the loop for each retry delay; retries are not bounded in the recorded snippet", "fix": "run the remove-with-retry helper under to_thread (or await asyncio.sleep) and cap retries", "test_to_add": "tests/test_temp_folder_cleanup.py::test_cleanup_does_not_block_loop"},
    {"severity": "low", "title": "node --check parse check runs subprocess.run without an offload", "file": "kimi-cli/src/kimi_cli/tools/file/parse_check.py:93", "evidence": "subprocess.run :93 with except (OSError, subprocess.TimeoutExpired) :101; module absent from the to_thread/run_sync inventory", "impact": "node latency spent on the loop for every checked JS/TS write/edit (timeout prevents a permanent hang)", "fix": "wrap in anyio.to_thread.run_sync / asyncio.to_thread, keeping the timeout", "test_to_add": "kimi-cli/tests/tools/test_parse_check.py::test_check_does_not_block_event_loop"}
  ],
  "waivers": [
    {"item": "(a) other candidate sites: glob.py:324 os.walk, grep_local.py:2245 os.walk, src/kimix/tools/file/__init__.py:49 shutil.rmtree", "rationale": "partial-offload modules (glob asyncio.to_thread :563, grep_local :772, file/__init__ anyio.to_thread :54); recorded greps cannot prove these lines run on the loop, so same-class candidates for the X02-F1 fix rather than separate findings"},
    {"item": "(b) anyio vs asyncio mixing", "rationale": "checked, clean: no recorded single call path binds both frameworks; closest overlap is module-level coexistence in common.py (asyncio.create_task :2109-2113 vs anyio.open_file :723 in different functions)"},
    {"item": "(c) task lifetime / fire-and-forget", "rationale": "checked, clean at inventory level: create_task sites store their handles (grep_local :1128/:1136 with await :1138; common.py :2109/:2112/:2113) and supervision exists (agent/__init__.py :67 wait_for_background_agents, :707 _notify_parent_background_finished; background/utils.py join_task :599, discard_all_tasks :619)"},
    {"item": "(d) async subprocess timeout/kill/wait/stdin/pipe-drain", "rationale": "checked, clean: _kill_process grep_local.py:567, _stop_after_timeout bash_tool.py:1159 / pwsh_tool.py:846; common.py CREATE_NEW_PROCESS_GROUP :1988, stdout/stderr/stdin tasks :2016/:2051/:2091 joined :2109-2113; sync subprocess.run sites are findings F2/F4"},
    {"item": "(e) temp-file cleanup on success and error paths", "rationale": "checked at inventory level with gap: helpers common.py:564-577,700,823,1707 and pinning test tests/test_temp_folder_cleanup.py exist, but the prescribed run of that test is not in gates/X02.txt — remains unverified; blocking-sleep aspect is X02-F3"},
    {"item": "(f) file handles / sqlite connections", "rationale": "checked, clean: all recorded opens use async with anyio.open_file (common.py:723, background:306/428/454, run.py:516, note:65/68/178/227, py:554, read_media:562); read_sqlite wraps apsw in asyncio.to_thread:491; residual open question = no explicit apsw.Connection close in the evidence"},
    {"item": "(g) cancellation: except Exception / bare except swallowing CancelledError", "rationale": "NOT VERIFIED — the required bare-except/CancelledError scan was never run into gates/X02.txt; recorded except clauses are narrow (OSError, subprocess.TimeoutExpired) and cannot be claimed clean; follow-up must run and append the scan"},
    {"item": "gate failures: kimi-cli/tests/tools/test_read_file.py :243 and :1379", "rationale": "not an async-resource defect — relative-path reads assert the 'absolute path' guard message but get a 'does not exist' message; consistent with the prompt-documented pre-existing kimi-cli failures (43); owned by C05/C07, recorded for gate transparency"},
    {"item": "orphan bytecode under src/kimix/tools/eval/**", "rationale": "only __pycache__/*.pyc remains (git ls-files src/kimix/tools/eval is empty); dead-code/stale-artifact concern owned by X05, not an async resource"}
  ],
  "gate_evidence": "reviews/tools/gates/X02.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "worktree_clean_at_start": true
}
```
