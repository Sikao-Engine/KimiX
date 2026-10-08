# X06 — test-hygiene

Scope: `kimi-cli/tests/tools/`, `kimi-cli/tests/core/`, `kimi-cli/tests/native/`,
`tests/unit/tools/`, `tests/kimix/`, `tests/native/`, `tests/regression/tools/`.
Raw evidence (every command's unedited output, appended with `>>`):
`reviews/tools/gates/X06.txt`.

## 0. Method

### 0.1 Directories and inventory

| dir | `test_*.py` files |
|---|---|
| `kimi-cli/tests/tools` | 64 |
| `kimi-cli/tests/core` | 80 |
| `kimi-cli/tests/native` | 8 |
| `tests/unit/tools` | 19 |
| `tests/kimix` | 7 |
| `tests/native` | 11 |
| `tests/regression/tools` | 1 |
| **total scanned** | **190 files / 3692 `test_*` functions** |

### 0.2 Throwaway scanners (all under `.kimix_cache/`, none under `tools/`)

| script | purpose |
|---|---|
| `.kimix_cache/x06_no_assert.py` | AST sweep: every `test_*` (incl. class methods) with **zero** `ast.Assert` / `pytest.raises` / `pytest.fail` / `snapshot()` call → prints `file::test_fn`. Also tautology scan (`assert True`, `assert isinstance(x, object)`, `assert x is not None`) and skip constructs. |
| `.kimix_cache/x06_noassert_detail.py` | per-function body calls + line span for every no-assert function |
| `.kimix_cache/x06_tier2.py` | tier-2 refinement (no assert **and** no `*assert*`-named call, no `raise AssertionError`) |
| `.kimix_cache/x06_final.py` | recursive helper resolution: `FIXTURE` / `DELEGATED` / `SMOKE` classification |
| `.kimix_cache/x06_skips.py` | full skip inventory incl. fixtures/classes/module scope + reason presence |
| `.kimix_cache/x06_prompt_diff.py` | exact expected-vs-actual diff for the stale description snapshot |
| `.kimix_cache/x06_probe/test_dump_desc.py` | pytest-side probe dumping the composed tool descriptions |

`uv run tools/syntax_check.py` was run on every script (all `Syntax OK`).

### 0.3 Definitions used

* **no-assert test** — `test_*` function whose body contains no `assert`, no
  `pytest.raises/fail/xfail`, no `snapshot(...)` call (gate definition).
* **tier-1** — the 57 functions matching that definition.
* **DELEGATED** — tier-1 but a call into a helper that itself asserts
  (resolved recursively by name in the same file) → verification exists,
  non-finding.
* **SMOKE** — no verification anywhere in the call graph → the real finding set.
* **unreasoned skip** — `pytest.skip/skipif/xfail` with neither a `reason=`
  keyword nor a positional message string.

### 0.4 Runs recorded (exact totals)

```
uv run pytest kimi-cli/tests/tools -q
  run 1                : 3 failed, 2008 passed, 19 skipped in 55.68s   (exit 1)
  run D (--tb=line)    : 2 failed, 2020 passed, 19 skipped in 36.86s   (exit 1)
  run E (--tb=line)    : 2 failed, 2020 passed, 19 skipped in 38.44s   (exit 1)
  --collect-only (3x)  : 2041 tests collected (stable)
uv run pytest tests/unit/tools tests/kimix tests/native -q --ignore=tests/test_bash_e2e.py
                       : 2 failed, 1073 passed in 75.79s                (exit 1)
```
19 skipped in the tools run: 13 × `skipif(win32)` + 5 × "module does not
declare `__all__`" + 1 × `require_long_paths` fixture.

## 1. Findings

### X06-F1 (high) — 12 tests perform **no verification at all**

Scanners: `x06_no_assert.py` (section A, 57 tier-1), `x06_tier2.py`,
`x06_final.py` (SMOKE = 12). The other 44 tier-1 functions delegate to an
asserting helper (`_assert_equivalent`, `_assert_eq`,
`_assert_misformatted_autocorrects_to_long`, `_run_both`, …) — see §2.
Each item below can pass while the code under test is completely broken
("must not raise" is the only observable).

1. `kimi-cli/tests/tools/test_blackbox.py:51` `test_noop_recorder_never_raises` — awaits `NoOpBlackboxRecorder().record(...)` and drops the result.
   **Fix:** bind and assert the no-op contract:
   `assert await recorder.record(snapshot, variant="replace", arg={}) is None`.
2. `kimi-cli/tests/tools/test_fs_cache_invalidation.py:141` `test_unregister_missing_is_noop` — calls `unregister_invalidator(lambda p: None)` (never registered) with no assertion.
   **Fix:** `from kimi_cli.tools.file import fs_cache` ; `before = list(fs_cache._INVALIDATORS)` ; `fs_cache.unregister_invalidator(lambda p: None)` ; `assert fs_cache._INVALIDATORS == before`.
3. `kimi-cli/tests/tools/test_fs_cache_invalidation.py:173` `test_delete_without_session_is_safe` — `invalidate_fs_scan_after_delete(path, session=None)` only.
   **Fix:** register a spy and assert the canonical path reached it, and the `session=None` branch did not raise:
   `seen = []` ; `register_invalidator(seen.append)` ; `try: invalidate_fs_scan_after_delete(str(p), session=None)` ; `finally: unregister_invalidator(seen.append)` ; `assert seen == [canonical_snapshot_key(str(p))]`.
4. `kimi-cli/tests/core/test_history_index.py:386` `TestHistoryIndexPhaseC::test_rebuild_fts_with_legacy_mode_is_noop` — `HistoryIndex(persist_path=…).rebuild_fts()` with no assertion.
   **Fix:** `assert idx._use_fts is False` (legacy backend selected) before the call, then `idx.rebuild_fts()` and `assert idx._conn is None` (rebuild must not open a SQLite connection) and `assert idx._fts_stale is False`.
5. `kimi-cli/tests/core/test_plugin.py:208` `test_inject_config_noop_when_no_inject` — plugin.json has no `config_file`, so the early return is hit and nothing is observed.
   **Fix:** make the precondition real and assert the file is untouched:
   write `{"name": "p", "version": "1.0.0", "config_file": "config.json"}` plus a `config.json` containing `{"a": 1}`, `spec = parse_plugin_json(...)`, `assert spec.inject == {}`, `before = (plugin_dir / "config.json").read_bytes()`, `inject_config(plugin_dir, spec, {"api_key": "sk-x"})`, `assert (plugin_dir / "config.json").read_bytes() == before`.
6. `kimi-cli/tests/core/test_session.py:614` `test_exception_cleanup_none_session` — **dead test**: it declares a local `_latest_created_session = None`, then re-implements the handler body; the `if` can never run. Production `_latest_created_session` (`kimi-cli/src/kimi_cli/cli/__init__.py:361,408,409`) is **written and never read**, so the cleanup the test claims to cover does not exist (see also X06-F2).
   **Fix:** extract the decision into production
   `async def _cleanup_failed_session(session: Session | None) -> None: if session is not None and session.is_empty(): await _delete_empty_session(session)`,
   call it from the CLI error path, and rewrite this test as
   `async def test_exception_cleanup_none_session(): await _cleanup_failed_session(None)` with the real assertion that the share dir is unchanged (`assert not (share_dir / "sessions").exists() or session_dir.exists()`); delete the inline copy.
7. `kimi-cli/tests/core/test_shutdown_background_tasks.py:225` `test_shutdown_swallows_manager_exception` — declares the unused `capsys` fixture and awaits `cli.shutdown_background_tasks()` after `manager.list_tasks.side_effect = OSError(...)`; nothing is asserted, so a swallowed *and ignored* error passes.
   **Fix:** take `caplog`, keep the `patch("kimi_cli.app.asyncio.sleep", …)` guard, then assert the exercised path and the warning the SUT emits (`kimi-cli/src/kimi_cli/app.py`, `logger.warning("Error during background task shutdown; continuing exit", exc_info=True)`):
   `with caplog.at_level(logging.WARNING, logger="kimi_cli.app"), patch(...): await cli.shutdown_background_tasks()` ; `assert manager.list_tasks.called` ; `assert "Error during background task shutdown" in caplog.text`.
8. `tests/unit/tools/test_taskoutput.py:129` `TestTaskOutputDelCleanup::test_del_without_session_does_nothing` — `TaskOutput.__new__(TaskOutput).__del__()`, no assertion.
   **Fix:** `assert getattr(obj, "_session", None) is None` and bind the call: `assert obj.__del__() is None`.
9. `tests/unit/tools/test_taskoutput.py:134` `…::test_del_with_mock_session_no_event_loop` — `__del__` with a MagicMock session, no assertion.
   **Fix:** `assert obj.__del__() is None` ; `assert obj._session.mock_calls == []` (proves no `discard_all_tasks`/`call_soon_threadsafe` was scheduled without a loop).
10. `tests/unit/tools/test_taskoutput.py:140` `…::test_del_during_finalization_noop` — assigns `sys.is_finalizing` globally and asserts nothing.
    **Fix:** replace the manual assignment with `with mock.patch("sys.is_finalizing", return_value=True): obj.__del__()`, then `assert obj._session.mock_calls == []`.
11. `tests/kimix/test_agent_conversation.py:765` `test_agent_inherit_context_reset_skips_when_soul_missing` — `await agent._reset_inherited_system_prompt(sub_session)` with `sub_session._cli = None`, no assertion.
    **Fix:** `await agent._reset_inherited_system_prompt(sub_session)` ; `assert sub_session.mock_calls == []` (the best-effort early return must touch nothing).
12. `tests/kimix/test_agent_swarm.py:56` `test_validate_uniqueness_ok` — `_validate_uniqueness(["p1", "p2"])`, no assertion.
    **Fix:** `assert _validate_uniqueness(["p1", "p2"]) is None` (the validator is `-> None`; the duplicate case is already covered by `test_validate_uniqueness_detects_duplicates`).

### X06-F2 (high) — `test_session.py` cleanup tests re-implement production logic inline

`kimi-cli/tests/core/test_session.py:628,644,661,683-688` assert Python
semantics of a **copy** of the exception-cleanup body, not of the CLI. They
pass regardless of what the CLI does. Combined with X06-F1(6) (the production
variable is write-only) the "exception cleanup" behaviour is untested and, as
written, unreachable.
**Exact fix:** introduce the `_cleanup_failed_session()` helper (X06-F1(6)),
call it from `cli/__init__.py`'s `except Exception` path (or delete the dead
`_latest_created_session`), and rewrite these three tests to call the helper.

### X06-F3 (medium) — 13 platform skips remove Windows coverage silently

`kimi-cli/tests/tools/test_additional_dirs.py:73,168,250,263`,
`test_glob.py:513,533`, `test_grep.py:1623,1644,1659`,
`test_read_file.py:443,461,471,484` are
`@pytest.mark.skipif(sys.platform == "win32", reason="…")`; this host **is
Windows**, so all 13 never execute here (evidence: `-rs` runs for each file,
`11 passed, 4 skipped`, `42 passed, 3 skipped`, `107 passed, 3 skipped`,
`2 failed, 99 passed, 4 skipped`). The reason text is a TODO
("`[out of work-dir] warning not implemented in Glob/Grep/ReadFile`"), i.e. the
skip hides the same feature that X06-F7's `test_read_file.py` failures show as
half-implemented.
**Exact fix:** either implement the missing warning and drop the `skipif`, or
replace it with an explicit expectation that is true on Windows — e.g.
`@pytest.mark.xfail(sys.platform == "win32", strict=False, reason="GH-<id>: out-of-work-dir warning not implemented (Windows)")`
— and add a Windows assertion that the *error* is still raised with the
documented message, so the behaviour is not silently unverified.

### X06-F4 (medium) — `test_tools_integrity_gate.py:49` converts a real gap into 5 skips

`pytest.skip(f"{module_name} does not declare __all__")` fires for
`kimi_cli.tools.file.{read_media,read,glob,grep_local,write}` (evidence:
`22 passed, 5 skipped`, names in `-rs` output). The `__all__` integrity check
the gate exists for is therefore not enforced on exactly those modules.
**Exact fix:** `pytest.fail(f"{module_name} does not declare __all__")` (or
assert `hasattr(module, "__all__")`) after adding the missing `__all__` lists
to those five modules; do not skip.

### X06-F5 (medium) — unconditional skip in a core test

`kimi-cli/tests/core/test_create_llm.py:15`
`@pytest.mark.skip(reason="inline-snapshot incompatibility with pydantic SecretStr on this platform")`
on `test_augment_provider_with_env_vars_kimi` (evidence: `48 passed, 1 skipped`).
This test can never run on any platform hosting that incompatibility.
**Exact fix:** replace the inline-snapshot comparison with an explicit
assertion on the augmented provider dict, then delete the `@pytest.mark.skip`;
if that is not possible, convert to `pytest.mark.xfail(strict=False, reason="<issue-id>")`
so the test still executes and the failure is visible.

### X06-F6 (medium) — 6 Windows skips with a non-informative reason

`kimi-cli/tests/core/test_agent_cutover.py:11` (1),
`test_default_agent.py:14` (1),
`test_subagent_builder.py:13,38,63,89` (4) all read
`reason="Skipping test on Windows"` and are skipped on this host (evidence:
`1 skipped`, `4 skipped`, `1 skipped`). The reason names no behaviour and no
issue.
**Exact fix:** state the concrete platform fact and the tracking issue
(e.g. `reason="posix path/exec semantics in builder defaults (GH-<id>)"`) and
make the tests run on Windows where the assertion is path-agnostic; the current
text makes the gap invisible in `-rs` output.

### X06-F7 (high) — 4 failing tests in the required runs

1. `kimi-cli/tests/tools/test_read_file.py:243` `test_read_with_relative_path_outside_work_dir`
   — `assert "absolute path" in result.message.lower()` but the tool returns
   ``'`../outside_file.txt` does not exist.'`` (fails in isolation too:
   `2 failed, 99 passed, 4 skipped`).
   **Fix:** align product and test — either emit the "absolute path" branch for
   relative out-of-work-dir paths (in the reader's path validation) or change
   the assertion to the documented message and add a separate test for the
   absolute-path branch.
2. `kimi-cli/tests/tools/test_read_file.py:1379` `TestReadFileGlob::test_read_glob_outside_workspace_relative`
   — same mismatch with ``'Directory for `../outside/*.md` does not exist.'``.
   **Fix:** as above; both tests belong to the same feature as X06-F3's 4 skipped
   `[out of work-dir] warning` tests and should be fixed together.
3. `tests/unit/tools/test_prompt_common.py:177` `test_descriptions_unchanged`
   — the `bash` snapshot literal is **stale**: it still expects
   `"Execute a bash command (native POSIX syntax). Prefer \`glob\`/\`grep\` over
   \`find\`/\`ls\`/\`grep\`/\`rg\` … Use /dev/null, not \`> nul\`, …"` while the
   current composition (`src/kimix/tools/file/bash/bash_tool.py`,
   `description = "Execute a bash command." + _interactive_scope_text(is_shell=True)`,
   plus `self.description += "ALWAYS use native POSIX syntax, even on Windows"`)
   yields (probe `x06_probe/test_dump_desc.py`, dumped in pytest's own env):
   `Execute a bash command.For long sessions: … send 'exit' to close.ALWAYS use
   native POSIX syntax, even on Windows Safety: runs in the agent process
   (PID …) …`. The prose string exists only in the test file
   (`grep -rn "Prefer \`glob\`/\`grep\` over"` → test file only).
   **Fix (two parts):** (a) in `bash_tool.py` restore word separators —
   `"Execute a bash command. " + _interactive_scope_text(is_shell=True)` and
   `self.description += " ALWAYS use native POSIX syntax, even on Windows."` —
   because the run-together wire text (`command.For`, `Windows Safety`) is
   user-visible; (b) regenerate the two snapshot literals for `bash`/`pwsh`
   (import the fragments from `kimix.tools.prompt_common` instead of duplicating
   the prose so the snapshot cannot drift again).
4. `tests/unit/tools/test_bash_gate.py::test_probe05_child_env_scrubbed`
   — fails only when the review harness exports `K04_GATE_API_KEY`
   (`1 failed, 71 passed`); with that var unset the same test passes
   (`unset K04_GATE_API_KEY; … -k test_probe05_child_env_scrubbed` → `1 passed`).
   **Fix:** make the test hermetic — `monkeypatch.delenv("K04_GATE_API_KEY", raising=False)`
   for the vars it does not own, or assert only over the sentinel vars the test
   itself injects (`monkeypatch.setenv`) instead of over the whole ambient env.

### X06-F8 (medium) — order/state-dependent test + unstable collection total

Run 1 of `uv run pytest kimi-cli/tests/tools -q` reported
`3 failed, 2008 passed, 19 skipped` (2030 items) and failed
`kimi-cli/tests/tools/test_edit_gate.py::test_probe17_allow_auto_generated_field`;
runs D/E and the isolation run report 2041 collected items and that test passes
(`28 passed`). Three consecutive `--collect-only` runs report 2041, so the 11
missing items in run 1 are unexplained; no `pytest-randomly`, no
`pytest_collection_modifyitems`/`pytest_generate_tests` hooks and no `nul`
files exist in the workspace.
**Fix:** make `test_probe17…` independent of process-global guard state — build
the file with `monkeypatch`-scoped fixtures only, and assert the detector inputs
explicitly (`assert is_auto_generated_file_name("zz_generated.py")` before the
tool call); re-run the file twice in the same session
(`uv run pytest kimi-cli/tests/tools/test_edit_gate.py -q -p no:cacheprovider`)
and investigate the collection delta before trusting the tools-suite total.

### X06-F9 (low) — fixed sleeps instead of waiting on the observable condition

`kimi-cli/tests/core/test_subagent_store.py:91` `time.sleep(0.01)` and
`tests/kimix/test_subagent_anonymous_session.py:276`
`time.sleep(0.4)  # let the releaser finish before the test tears down`.
**Fix:** replace with a bounded poll on the state the test then asserts
(e.g. `await wait_until(lambda: not child_dir.exists(), timeout=5)`) so a slow
CI host cannot flake and a fast host does not pay the constant.

### X06-F10 (nit) — fixture named `test_*`

`kimi-cli/tests/tools/test_glob.py:16` `@pytest.fixture async def test_files(...)`
— not collected as a test (verified: `--collect-only | grep -c '::test_files$'` → 0)
but it collides with the `test*` collection pattern and hides intent.
**Fix:** rename to `glob_tree`/`prepared_tree` at the definition and all uses.

## 2. Waivers / non-findings

* **44 tier-1 functions are verified via an asserting helper** — e.g. the 19
  `tests/native/test_behavior_equivalence.py` / `kimi-cli/tests/native/test_behavior_equivalence.py`
  cases that call `_assert_equivalent(native, python, case)`
  (`assert native_result == python_result, …`), the 7
  `tests/native/test_src_compat_delegation.py` cases that call `_assert_eq`
  (asserts equality or identical exception types), the 8 `test_toolset.py`
  autocorrect cases via `_assert_misformatted_autocorrects_to_long`, and the 5
  `test_grep_engine_parity.py` cases via `_run_both`
  (`assert mir == eng, (pat, kw, eng, mir)`). Verification exists; not a
  hygiene violation (the gate's literal "zero `assert`" rule over-reports here).
* **`test_glob.py::test_files` fixture** — see the nit above; not collected.
* **Tautologies** — 249 `assert x is not None` occurrences, but **0** functions
  whose *only* assertion is one of them, **0** `assert True`, **0**
  `assert isinstance(x, object)` (sections B/C of the sweep). No finding.
* **Unreasoned skips: 0.** All 64 skip constructs carry a `reason=` or a
  positional message (`.kimix_cache/x06_skips.py` → `=== UNREASONED skips: 0 ===`).
* **11 native-gate `pytest.skip("native runtime not staged")` / `pytest.skip("native TOOLS gate is off")` calls** in
  `kimi-cli/tests/native/{test_loader.py:68,197,test_per_kernel_toggle.py:56,test_compat_missing_dll.py:71}`,
  `tests/native/{test_loader.py:66,198,test_per_kernel_toggle.py:54,test_compress_equivalence.py:238}`
  — reasoned, and none of them fired on this host (each file ran with
  `0 skipped` and full pass counts: `17 passed`, `9 passed`, `3 passed`,
  `188 passed`).
* **2 `@pytest.mark.xfail`** in `kimi-cli/tests/native/test_grep_engine_parity.py:514,546`
  — carry explicit reasons (non-strict xfail is an intentional characterisation).
* **`kimi-cli/tests/tools/test_grep_selectors.py:194`** `pytest.skip("filesystem does not allow ':' in filenames")`
  — reasoned, guards a genuinely impossible precondition; did not fire
  (`55 passed`).
* **`require_long_paths` fixture skip** (`kimi-cli/tests/conftest.py:166-173`)
  — reasoned and environment-driven (Windows `MAX_PATH`); acceptable.
* **`time.time()` usages** (44 hits) build fixtures' timestamps
  (`tests/kimix/test_agent_conversation.py` model state, `test_history_index.py`
  age offsets) and are never used to assert wall-clock behaviour — non-findings.
  No `datetime.now` usage in scope.
* **`tests/kimi-cli/tests/tools/test_grep_ripgrep_only.py` etc.** — no skips.
* **Working-tree state:** `git status --porcelain` also lists
  `reviews/tools/K04-bash-bash.md` and `reviews/tools/gates/K04.txt` as
  modified; those belong to the parallel K04 gate and were **not** touched by
  X06 (no git write command was run and no source/test file was edited — the
  only files X06 wrote are `reviews/tools/gates/X06.txt` (appended),
  `reviews/tools/X06-test-hygiene.md`, and the throwaway runners under
  `.kimix_cache/`).

## 3. Gate result

**G-X06: FAIL.**

* 12 tests (X06-F1) can pass with the code under test arbitrarily broken;
  the gate's "no-assert test" condition is violated by all of them.
* Both required runs exit non-zero:
  `kimi-cli/tests/tools` → `2 failed, 2020 passed, 19 skipped` (run 1:
  `3 failed, 2008 passed`, + flaky `test_probe17…`);
  `tests/unit/tools tests/kimix tests/native` → `2 failed, 1073 passed`.
* 20 skips hide coverage on the primary dev platform (13 Windows `skipif` +
  5 `__all__` misses + 1 unconditional skip + the fixture skip), and the
  reasons for 6 of them name no behaviour.
* Unreasoned skips: **0** (the one sub-condition that passes).
* Collection instability observed once (2030 vs 2041) — must be resolved
  before the tools-suite total can be trusted.

Gate flips to PASS when: all 12 X06-F1 items assert an observable, the four
failing tests (X06-F7) are fixed, the 19 skip constructs are either implemented
or converted to explicit, issue-referenced expectations (X06-F3/F4/F5/F6), and
`test_probe17…` is proven order-independent (X06-F8).

## 4. JSON summary

```json
{
  "id": "X06",
  "title": "test-hygiene",
  "gate": "G-X06",
  "status": "FAIL",
  "evidence": "reviews/tools/gates/X06.txt",
  "scope": {
    "dirs": [
      "kimi-cli/tests/tools",
      "kimi-cli/tests/core",
      "kimi-cli/tests/native",
      "tests/unit/tools",
      "tests/kimix",
      "tests/native",
      "tests/regression/tools"
    ],
    "files_scanned": 190,
    "test_functions_scanned": 3692
  },
  "method": {
    "scanners": [
      ".kimix_cache/x06_no_assert.py",
      ".kimix_cache/x06_noassert_detail.py",
      ".kimix_cache/x06_tier2.py",
      ".kimix_cache/x06_final.py",
      ".kimix_cache/x06_skips.py",
      ".kimix_cache/x06_prompt_diff.py",
      ".kimix_cache/x06_probe/test_dump_desc.py"
    ],
    "no_assert_definition": "test_* function with zero ast.Assert / pytest.raises / pytest.fail / snapshot() call",
    "unreasoned_skip_definition": "pytest.skip/skipif/xfail with neither reason= keyword nor positional message string"
  },
  "metrics": {
    "no_assert_functions_tier1": 57,
    "tier1_delegated_to_asserting_helper": 44,
    "no_verification_functions_smoke": 12,
    "fixtures_named_test": 1,
    "tautology_assert_is_not_none_total": 249,
    "tautology_sole_assert_is_not_none": 0,
    "assert_true": 0,
    "assert_isinstance_object": 0,
    "skip_constructs_total": 64,
    "unreasoned_skips": 0,
    "skipif_win32_hidden_coverage": 19,
    "skips_hiding_missing_dunder_all": 5,
    "unconditional_skips": 1,
    "fixture_skips_reasoned": 1,
    "time_sleep_in_tests": 2,
    "datetime_now_in_tests": 0
  },
  "runs": [
    {
      "cmd": "uv run pytest kimi-cli/tests/tools -q",
      "run": 1,
      "result": "3 failed, 2008 passed, 19 skipped",
      "collected": 2030,
      "exit_code": 1
    },
    {
      "cmd": "uv run pytest kimi-cli/tests/tools -q --tb=line",
      "run": "D and E",
      "result": "2 failed, 2020 passed, 19 skipped",
      "collected": 2041,
      "exit_code": 1
    },
    {
      "cmd": "uv run pytest kimi-cli/tests/tools --collect-only -q",
      "run": "A/B/C",
      "result": "2041 tests collected",
      "collected": 2041,
      "exit_code": 0
    },
    {
      "cmd": "uv run pytest tests/unit/tools tests/kimix tests/native -q --ignore=tests/test_bash_e2e.py",
      "result": "2 failed, 1073 passed",
      "skipped": 0,
      "exit_code": 1
    }
  ],
  "skips_per_file_measured": {
    "kimi-cli/tests/tools/test_additional_dirs.py": "11 passed, 4 skipped",
    "kimi-cli/tests/tools/test_glob.py": "42 passed, 3 skipped",
    "kimi-cli/tests/tools/test_grep.py": "107 passed, 3 skipped",
    "kimi-cli/tests/tools/test_read_file.py": "2 failed, 99 passed, 4 skipped",
    "kimi-cli/tests/tools/test_tools_integrity_gate.py": "22 passed, 5 skipped",
    "kimi-cli/tests/tools/test_grep_selectors.py": "55 passed, 0 skipped",
    "kimi-cli/tests/core/test_create_llm.py": "48 passed, 1 skipped",
    "kimi-cli/tests/core/test_default_agent.py": "1 skipped",
    "kimi-cli/tests/core/test_subagent_builder.py": "4 skipped",
    "kimi-cli/tests/core/test_agent_cutover.py": "1 skipped",
    "kimi-cli/tests/native/test_loader.py": "17 passed, 0 skipped",
    "kimi-cli/tests/native/test_per_kernel_toggle.py": "9 passed, 0 skipped",
    "kimi-cli/tests/native/test_compat_missing_dll.py": "3 passed, 0 skipped",
    "tests/native/test_loader.py": "17 passed, 0 skipped",
    "tests/native/test_per_kernel_toggle.py": "9 passed, 0 skipped",
    "tests/native/test_compress_equivalence.py": "188 passed, 0 skipped",
    "tests/native/test_behavior_equivalence.py": "114 passed, 0 skipped",
    "tests/unit/tools/test_bash_gate.py": "1 failed, 71 passed"
  },
  "findings": [
    {
      "id": "X06-F1",
      "severity": "high",
      "title": "12 tests perform no verification at all",
      "items": [
        {
          "location": "kimi-cli/tests/tools/test_blackbox.py:51::test_noop_recorder_never_raises",
          "fix": "assert await recorder.record(snapshot, variant=\"replace\", arg={}) is None"
        },
        {
          "location": "kimi-cli/tests/tools/test_fs_cache_invalidation.py:141::test_unregister_missing_is_noop",
          "fix": "from kimi_cli.tools.file import fs_cache; before = list(fs_cache._INVALIDATORS); fs_cache.unregister_invalidator(lambda p: None); assert fs_cache._INVALIDATORS == before"
        },
        {
          "location": "kimi-cli/tests/tools/test_fs_cache_invalidation.py:173::test_delete_without_session_is_safe",
          "fix": "seen=[]; register_invalidator(seen.append); try: invalidate_fs_scan_after_delete(str(p), session=None) finally: unregister_invalidator(seen.append); assert seen == [canonical_snapshot_key(str(p))]"
        },
        {
          "location": "kimi-cli/tests/core/test_history_index.py:386::TestHistoryIndexPhaseC::test_rebuild_fts_with_legacy_mode_is_noop",
          "fix": "assert idx._use_fts is False before the call; after idx.rebuild_fts(): assert idx._conn is None and idx._fts_stale is False"
        },
        {
          "location": "kimi-cli/tests/core/test_plugin.py:208::test_inject_config_noop_when_no_inject",
          "fix": "give the spec a config_file with empty inject, snapshot config.json bytes, call inject_config, assert bytes unchanged"
        },
        {
          "location": "kimi-cli/tests/core/test_session.py:614::test_exception_cleanup_none_session",
          "fix": "dead test (local _latest_created_session=None, inline copy of handler): extract production _cleanup_failed_session(session) and assert that _cleanup_failed_session(None) leaves the sessions dir untouched; delete the inline copy"
        },
        {
          "location": "kimi-cli/tests/core/test_shutdown_background_tasks.py:225::test_shutdown_swallows_manager_exception",
          "fix": "use caplog instead of the unused capsys; after await cli.shutdown_background_tasks(): assert manager.list_tasks.called and assert \"Error during background task shutdown\" in caplog.text"
        },
        {
          "location": "tests/unit/tools/test_taskoutput.py:129::TestTaskOutputDelCleanup::test_del_without_session_does_nothing",
          "fix": "assert getattr(obj, '_session', None) is None; assert obj.__del__() is None"
        },
        {
          "location": "tests/unit/tools/test_taskoutput.py:134::TestTaskOutputDelCleanup::test_del_with_mock_session_no_event_loop",
          "fix": "assert obj.__del__() is None; assert obj._session.mock_calls == []"
        },
        {
          "location": "tests/unit/tools/test_taskoutput.py:140::TestTaskOutputDelCleanup::test_del_during_finalization_noop",
          "fix": "with mock.patch('sys.is_finalizing', return_value=True): obj.__del__(); assert obj._session.mock_calls == []"
        },
        {
          "location": "tests/kimix/test_agent_conversation.py:765::test_agent_inherit_context_reset_skips_when_soul_missing",
          "fix": "await agent._reset_inherited_system_prompt(sub_session); assert sub_session.mock_calls == []"
        },
        {
          "location": "tests/kimix/test_agent_swarm.py:56::test_validate_uniqueness_ok",
          "fix": "assert _validate_uniqueness(['p1', 'p2']) is None"
        }
      ]
    },
    {
      "id": "X06-F2",
      "severity": "high",
      "title": "test_session.py cleanup tests re-implement production logic inline",
      "locations": [
        "kimi-cli/tests/core/test_session.py:628",
        "kimi-cli/tests/core/test_session.py:644",
        "kimi-cli/tests/core/test_session.py:661",
        "kimi-cli/src/kimi_cli/cli/__init__.py:361"
      ],
      "evidence": "_latest_created_session is assigned at cli/__init__.py:408-409 and never read; tests copy the if/delete body instead of calling production code",
      "fix": "extract _cleanup_failed_session(session) into kimi_cli/cli/__init__.py, call it from the except Exception path, and rewrite these three tests to call the helper"
    },
    {
      "id": "X06-F3",
      "severity": "medium",
      "title": "13 win32 skipif tests never run on the dev platform",
      "locations": [
        "kimi-cli/tests/tools/test_additional_dirs.py:73",
        "kimi-cli/tests/tools/test_additional_dirs.py:168",
        "kimi-cli/tests/tools/test_additional_dirs.py:250",
        "kimi-cli/tests/tools/test_additional_dirs.py:263",
        "kimi-cli/tests/tools/test_glob.py:513",
        "kimi-cli/tests/tools/test_glob.py:533",
        "kimi-cli/tests/tools/test_grep.py:1623",
        "kimi-cli/tests/tools/test_grep.py:1644",
        "kimi-cli/tests/tools/test_grep.py:1659",
        "kimi-cli/tests/tools/test_read_file.py:443",
        "kimi-cli/tests/tools/test_read_file.py:461",
        "kimi-cli/tests/tools/test_read_file.py:471",
        "kimi-cli/tests/tools/test_read_file.py:484"
      ],
      "evidence": "-rs output for each file: 4+2+3+4 skipped, reason is a TODO ('[out of work-dir] warning not implemented in ...')",
      "fix": "implement the out-of-work-dir warning and delete the skipif, or convert to @pytest.mark.xfail(sys.platform == 'win32', strict=False, reason='GH-<id>: ...') plus an explicit Windows assertion that the error+message still hold"
    },
    {
      "id": "X06-F4",
      "severity": "medium",
      "title": "integrity gate turns a real gap into 5 skips",
      "locations": ["kimi-cli/tests/tools/test_tools_integrity_gate.py:49"],
      "evidence": "22 passed, 5 skipped; skipped modules: kimi_cli.tools.file.read_media, read, glob, grep_local, write",
      "fix": "pytest.fail(f'{module_name} does not declare __all__') (or assert hasattr(module, '__all__')) after adding __all__ to those five modules; do not skip"
    },
    {
      "id": "X06-F5",
      "severity": "medium",
      "title": "unconditional skip of a core test",
      "locations": ["kimi-cli/tests/core/test_create_llm.py:15"],
      "evidence": "48 passed, 1 skipped; reason names an inline-snapshot incompatibility, so the test never executes anywhere",
      "fix": "replace the inline-snapshot comparison with an explicit assertion on the augmented provider and delete @pytest.mark.skip; otherwise xfail(strict=False, reason='<issue-id>')"
    },
    {
      "id": "X06-F6",
      "severity": "medium",
      "title": "6 Windows skips with a non-informative reason",
      "locations": [
        "kimi-cli/tests/core/test_agent_cutover.py:11",
        "kimi-cli/tests/core/test_default_agent.py:14",
        "kimi-cli/tests/core/test_subagent_builder.py:13",
        "kimi-cli/tests/core/test_subagent_builder.py:38",
        "kimi-cli/tests/core/test_subagent_builder.py:63",
        "kimi-cli/tests/core/test_subagent_builder.py:89"
      ],
      "evidence": "reason='Skipping test on Windows'; measured 1+1+4 skipped on this Windows host",
      "fix": "state the concrete platform behaviour plus a tracking issue in reason=, and make the assertions platform-agnostic so the tests execute on Windows"
    },
    {
      "id": "X06-F7",
      "severity": "high",
      "title": "4 failing tests in the required runs",
      "items": [
        {
          "location": "kimi-cli/tests/tools/test_read_file.py:243",
          "failure": "assert 'absolute path' in \"`../outside_file.txt` does not exist.\"",
          "fix": "emit the absolute-path message for relative out-of-work-dir paths, or assert the documented message and add a dedicated absolute-path test"
        },
        {
          "location": "kimi-cli/tests/tools/test_read_file.py:1379",
          "failure": "assert 'absolute path' in 'Directory for `../outside/*.md` does not exist.'",
          "fix": "same as above; fix together with the 4 skipped [out of work-dir] tests"
        },
        {
          "location": "tests/unit/tools/test_prompt_common.py:177",
          "failure": "stale bash description snapshot (expects the removed 'Prefer `glob`/`grep` over ...' prose); live composition is 'Execute a bash command.For long sessions: ...ALWAYS use native POSIX syntax, even on Windows Safety: ...'",
          "fix": "add the missing separators in src/kimix/tools/file/bash/bash_tool.py ('Execute a bash command. ' + _interactive_scope_text(is_shell=True); self.description += ' ALWAYS use native POSIX syntax, even on Windows.') and regenerate the bash/pwsh snapshot literals from the source constants"
        },
        {
          "location": "tests/unit/tools/test_bash_gate.py::test_probe05_child_env_scrubbed",
          "failure": "fails when the harness exports K04_GATE_API_KEY (1 failed, 71 passed); passes with that var unset",
          "fix": "monkeypatch.delenv('K04_GATE_API_KEY', raising=False) for vars the test does not own, or assert only over the sentinel vars the test injects"
        }
      ]
    },
    {
      "id": "X06-F8",
      "severity": "medium",
      "title": "order/state-dependent test and unstable collection total",
      "locations": ["kimi-cli/tests/tools/test_edit_gate.py::test_probe17_allow_auto_generated_field"],
      "evidence": "run 1 failed it with 2030 collected; runs D/E and the isolation run pass it with 2041 collected; no pytest-randomly, no collection hooks, no nul files",
      "fix": "scope all guard state through monkeypatch fixtures, assert is_auto_generated_file_name('zz_generated.py') before the tool call, re-run the file twice in one session and resolve the 11-item collection delta before trusting the suite total"
    },
    {
      "id": "X06-F9",
      "severity": "low",
      "title": "fixed sleeps instead of waiting on the observable condition",
      "locations": [
        "kimi-cli/tests/core/test_subagent_store.py:91",
        "tests/kimix/test_subagent_anonymous_session.py:276"
      ],
      "fix": "replace time.sleep with a bounded poll on the state the test asserts (e.g. await wait_until(lambda: not child_dir.exists(), timeout=5))"
    },
    {
      "id": "X06-F10",
      "severity": "nit",
      "title": "fixture named test_*",
      "locations": ["kimi-cli/tests/tools/test_glob.py:16"],
      "evidence": "--collect-only | grep -c '::test_files$' -> 0 (not collected, but collides with the test* pattern)",
      "fix": "rename the fixture to glob_tree (definition + all uses)"
    }
  ],
  "unreasoned_skips": [],
  "waivers": [
    "44 tier-1 no-assert functions delegate to asserting helpers (_assert_equivalent x26, _assert_eq x7, _assert_misformatted_autocorrects_to_long x5, _assert_ask_agent_variant_autocorrects x3, _run_both x5, raise AssertionError x1) - verification present",
    "249 occurrences of 'assert x is not None' but none is the sole assertion (section C = 0); 0 'assert True'; 0 'assert isinstance(x, object)'",
    "all 64 skip constructs carry a reason (reason= or positional message); unreasoned skips = 0",
    "11 native-gate pytest.skip calls with positional reasons did not fire on this host (native runtime staged)",
    "2 non-strict xfail marks with reasons in kimi-cli/tests/native/test_grep_engine_parity.py:514,546",
    "kimi-cli/tests/tools/test_grep_selectors.py:194 skip has a reason and did not fire (55 passed)",
    "require_long_paths fixture skip (kimi-cli/tests/conftest.py:166-173) is environment-driven and reasoned",
    "time.time() (44 hits) only builds fixture timestamps, never asserts wall-clock behaviour; no datetime.now usage in scope"
  ],
  "limitations": [
    "The 12 no-assert functions were classified by AST + helper resolution; runtime proof that each test passes with a mutated SUT was not attempted",
    "The run-1 anomaly (2030 vs 2041 collected, probe17 failure) could not be reproduced in 2 further full runs and 3 collect-only runs; the cause is unknown",
    "tests/unit/tools/test_prompt_common.py root cause was verified with a pytest-side probe (.kimix_cache/x06_probe/test_dump_desc.py) that dumps the composed description; pytest's own diff truncation hides the '+' side, so the exact current pwsh/other literals were confirmed only via that probe",
    "Per-file -rs skip counts were measured for 18 representative files; the remaining files' skips were covered only by the AST/grep inventory",
    "No source or test file was modified; no git write command was run",
    "X06 wrote only reviews/tools/gates/X06.txt (appended), reviews/tools/X06-test-hygiene.md and throwaway runners/probes under .kimix_cache/; the K04 files that git status also lists as modified belong to the parallel K04 gate and were not touched by X06"]
}
```
