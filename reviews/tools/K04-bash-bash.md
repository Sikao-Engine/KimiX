# K04 — `bash` (Bash) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | `reviews/tools/gates/K04.txt` §G0 (6/6 source files + 6/6 test files cited below; registration `00-registry.txt:44`) |
| G1 simplicity | **fail** | ruff 5 errors (`I001`×3, `E402`×2); `ruff --select C901` 2 errors (`__call__` 36, `detect_self_kill` 46); metrics `--max-fn-loc 120 --max-complexity 12` exit 1 (`loc=1461`, `longest_fn=325`, `cx_max=52`, `max_nest=6`); vulture 1 hit (waived 1) |
| G2 risk | **fail** | 13/13 items answered, `open_high = 1` (child-env scrubbing, owned by X03-F1/FP-11), `open_medium = 3` |
| G3 coverage | pass (with finding) | verbatim gate exit 0; but the verbatim `--cov` target never measures `bash_tool.py` (module-not-imported) → corrected target: **bash_tool.py 82.0 %** (Stmts 590, Miss 82), TOTAL 87.39 %, floor 80, waived_lines 82 / budget 100 |
| G4 behavior | **fail at the pinned SHA** | `tests/unit/tools/test_bash_gate.py`, 45 probe functions / 72 collected — 1 red (`test_probe05_child_env_scrubbed`); all 72 green *only* with the uncommitted FP-11 edit (see §6) |
| G5 regression | pass | ROOT suite baseline-identical (collection error, `tests/bash/cases.json` missing); CLI suite 43 failed / 5241 passed, failure set byte-identical to `00-baseline.txt` (NEW = ∅); syntax_check clean; git_diff only the new probe file |
| G6 independent rerun | pending (orchestrator) | numbers above are the ones to compare; see §8 for the HEAD movement that G6 must account for |

## 1. Scope

Source files (all read end-to-end; 2 537 LOC total):

| file | LOC | read |
|---|---|---|
| `src/kimix/tools/file/bash/bash_tool.py` | 1461 | windows at 1-424, 424-873, 873-1273, 1273-1461 |
| `src/kimix/tools/file/bash/safety.py` | 665 | windows at 1-401, 400-665 |
| `src/kimix/tools/file/bash/shell_common.py` | 159 | full |
| `src/kimix/tools/file/bash/bash_fix.py` | 135 | full |
| `src/kimix/tools/file/bash/output_enhance.py` | 106 | full |
| `src/kimix/tools/file/bash/__init__.py` | 11 | full |

Supplementary in-scope reads (referenced by the tool, needed for the risk audit):
`src/kimix/tools/common.py` (`ProcessTask` 1900-2260, `_maybe_rewrite_shell_command_with_rtk` 1521-1590,
`_token_filter_output` 1593-1704, `_env_with_rg_bin_path` 1774-1797, `OUTPUT_LIMIT` 508),
`src/kimix/tools/security.py` (1-91), `src/kimix/tools/prompt_common.py` (1-134),
`src/kimix/tools/file/bash/process_pwsh.py` (1-94).

Tests in scope: `tests/test_bash.py` (6643), `tests/test_bash_e2e.py` (489), `tests/test_shell_common.py` (184),
`tests/test_shell_safety.py` (437), `tests/test_output_enhance.py` (304),
`tests/native/test_shell_security_equivalence.py` (364).
Test-function inventory for `tests/test_bash.py` was extracted mechanically
(`^\s*(class |async def |def )` → 599 definitions, 491 test functions).

Registration: **registered** — `reviews/tools/00-registry.txt:44`
`registered kimix.tools.file.bash.bash_tool:Bash name='bash' src/kimix/tools/file/bash/bash_tool.py`.
Not an ORPHAN. `__init__.py:3-11` re-exports `Bash`/`BashParams` (and `Powershell`).

Probe suite created by this review: `tests/unit/tools/test_bash_gate.py` (1077 LOC, 45 probe functions).

## 2. Behaviour map

| # | entry point | branches | notes |
|---|---|---|---|
| 1 | `find_bash` (`bash_tool.py:417`) | `lru_cache`; 3 platforms | win32 → `_find_git_bash_windows`; darwin → homebrew/macports → git-bash → system → PATH |
| 2 | `_find_git_bash_windows` (`:333`) | 6 (env override, `where.exe`, `--exec-path`, 2 hard-coded dirs, PATH, WindowsApps stub) | each candidate must pass `_bash_runs` |
| 3 | `_bash_runs` (`:187`) | 2 (`OSError`, timeout) → False | probe runs external programs |
| 4 | `_is_git_bash_install` (`:275`) | 5 (shape, `usr/bin`, marker isfile) | MSYS2 rejected, drive-anchored marker |
| 5 | `_with_msystem_neutralized` (`:315`) | 2 (platform, git-bash) | `export MSYSTEM=; ` prefix |
| 6 | `_configured_shell` (`:453`) | 5 (OSError/ValueError/AttributeError, non-str, bash, pwsh) | reads `agent.shell` |
| 7 | `_should_enable_bash` / `_should_enable_powershell` (`:478`/`:509`) | 5 / 4 | config wins over platform heuristics |
| 8 | `_bash_subprocess_env` (`:147`) | 2 (win32) | `_env_with_rg_bin_path()` + MSYS opt-outs |
| 9 | `_prepare_bash_cmd` / `_process_unquoted` (`:580`/`:557`) | 2 + native-vs-shim | unquoted `\` → `/` on Windows only |
| 10 | `_encode_startup_script` / `_payload_eval_command` / `_bash_argv_env` (`:102`/`:118`/`:125`) | 2 (≤6000 vs payload) | gzip+pybase64 → `KIMIX_BASH_PAYLOAD` |
| 11 | `BashParams` (`:603`) | alias `command`; 6 fields; `mode` before-validator; `cmd` after-validator | `populate_by_name`; ranges 1..900 / ≥3 |
| 12 | `Bash.__init__` (`:646`) | 3 `SkipThisTool`, win32 description, forbidden-keyword dedup, 3 config gates | PID hint appended to the description |
| 13 | `Bash.__call__` (`:740`) | ~30 decision points, 50 branches (cx 36 ruff / 51 metrics) | hardline → self-kill → forbidden → task_id → send → empty → pattern → win32 refresh → interactive → one-shot |
| 14 | `_hardline_blocked` / `_self_kill_blocked` (`:692`/`:714`) | 3 / 3 | config-gated, return `ToolError(brief=...)` |
| 15 | `_forbidden_error` (`:1066`) | 3 | normalized substring match on the *displayed* command |
| 16 | `_prepare_command` (`:1083`) | 3 (unsupported, forbidden, ok) | delegates to `shell_common.inspect_bash_command` |
| 17 | `_continuation_may_be_incomplete` (`:1105`) | 5 (empty, odd trailing `\`, bash `-n` rc, 3 stderr markers, OSError) | fail-closed |
| 18 | `_compile_pattern` (`:1147`) | 3 | `ToolError(brief="Invalid pattern")` |
| 19 | `_stop_after_timeout` (`:1159`) | 2 (partial output) | `stop()` + `remove_task_id` |
| 20 | `_execute_background` (`:1187`) | 4 | send path; optional `wait_for_output` |
| 21 | `_continue_session` (`:1253`) | 6 (not found ×2, bad pattern, unsafe fragment, input failure, ok) | sends `cmd + "\n"`; no rtk/fix rewriting |
| 22 | `_process_output` (`:1329`) | 5 (redact, rtk-original, token filter, >65536 summarize+save, export) | populates `output_truncated` |
| 23 | `_format_session_result` (`:1369`) | 4 | status → exit code → meaning; elapsed only when completed |
| 24 | `_format_background_output` (`:1421`) | 4 | `job_output` formatting parity |
| 25 | `safety.check_hardline_blocked` / `detect_hardline_command` / `command_detection_variants` (`safety.py:100`/`90`/`59`) | 2 / native-vs-shim / pure-Python only | deobfuscation variants (≤3) |
| 26 | `safety.detect_self_kill` (`:457`) | 5 kill families × PID/loop-var/name/pattern | cx 52 (ruff 46), 51 branches |
| 27 | `safety.self_kill_hint` (`:642`) | 3 (safe, pid/image cache, variant loop) | wraps `detect_self_kill` |
| 28 | `safety.foreground_background_guidance` (`:117`) | 3 | hint for dev servers/watchers |
| 29 | `safety.validate_workdir` (re-export `:114`) | - | moved to `kimix.tools.security` |
| 30 | `output_enhance.interpret_exit_code` / `is_expected_exit` / `annotate_failure` (`:54`/`:73`/`:92`) | 3 / 3 / 2 | SIGPIPE rule evaluated before the native fast path; `redact_sensitive_output` re-exported `:106` |
| 31 | `shell_common.bash_argv` / `bash_file_argv` / `inspect_bash_command` / `prepare_bash_command` / `pwsh_argv` / `pwsh_file_argv` / `pwsh_executable` / `wrap_pwsh_command` (`:72`/`:91`/`:49`/`:61`/`:122`/`:153`/`:114`/`:100`) | 2-4 each | single home of the `(argv, env)` machinery; `PWSH_ONESHOT_FLAGS` `:30` |
| 32 | `bash_fix.fix_bash_command` (`:113`) | 3 (non-win32/empty, shim result) | pure re-export of the shim scanner; `BashFix` dataclass |

## 3. S1 Simplicity (G1 detail)

verdict: **fail** (3 hard breaches + lint).

| finding | file:line | evidence | change | waiver? |
|---|---|---|---|---|
| `bash_tool.py` is 1461 LOC vs the planned ≈700 (2.1×) | `bash_tool.py:1-1461` | `review_metrics` `loc=1461` (baseline `00-metrics-baseline.json` identical) | split decision below | no |
| `__call__` 325 LOC / cx 51 (ruff 36) — hard cap 200 | `bash_tool.py:740-1064` | `longest_fn={'name': '__call__','loc': 325}`; `ruff C901 36 > 12` | extract `_run_interactive_start`, `_run_one_shot`, `_finish_one_shot` | no |
| `safety.detect_self_kill` cx 46-52 (hard fail >15) | `safety.py:457-629` | `ruff C901 46 > 12`; `module_complexity=52` | one helper per kill family (`_posix_kill_hits`, `_taskkill_hits`, `_pstop_hits`, `_pkill_hits`, `_wmic_hits`) | no |
| `max_nesting_depth 6 > 5` | `safety.py:341-364` (`_loop_pid_sources`) | `max_nest=6` in the metrics JSON | early-continue guards in the inner loops | no |
| ruff lint failures | `output_enhance.py:27`, `safety.py:24`, `shell_common.py:19` (`I001`); `output_enhance.py:106`, `safety.py:114` (`E402`) | `ruff check` exit 1, `Found 5 errors` | sort the `native_loader` imports / move the compatibility re-exports under a `# noqa: E402`-carrying module tail or a dedicated shim module | no |
| unreachable branch: "Empty command" `ToolError` | `bash_tool.py:769-774` | `mode=="send"` returns at `:766`; `mode=="execute"` with empty `cmd` is rejected by `shell_cmd_required_validator` (`prompt_common.py:124-127`); line `770` is in the coverage `Missing` column; probe 19 asserts both halves | delete the branch | no |
| magic numbers without names | `bash_tool.py:1358` (`65536`), `:900`/`:1314` (`float(params.timeout)`), `safety.py:230`/`:237` (`range(64)` ppid depth), `:280`/`:411`/`:416` (`3` = min name length) | `grep` §G2(f,i) in the evidence file | hoist to named constants (`_SUMMARY_THRESHOLD_CHARS`, `_MAX_ANCESTOR_DEPTH`, `_MIN_IMAGE_NAME_LEN`) | no |
| hot-path function-local imports on every call | `bash_tool.py:999`, `:1092`, `:1176`, `:1255`, `:1313` (`remove_task_id`, `shell_common`, `get_all_tasks`, `DEFAULT_INACTIVITY_TIMEOUT`) | `grep '^    from '` §G1 in the evidence file | module-level imports (cycle-safe per the module docstring of `shell_common.py:13-16`) | partial — justified only if the cycle is proven |
| `_encode_startup_script` imports `gzip`+`pybase64` inside the function | `bash_tool.py:111-115` | same grep | module-level (pybase64 is a declared dep) | no |
| vulture `unused variable 'cls'` | `bash_tool.py:629` | `vulture --min-confidence 90` (stable across 3 runs) | **waived**: `cls` is mandatory in the pydantic `@model_validator(mode="before") @classmethod` idiom (`prompt_common.py:95`); removing it breaks the validator | yes (1) |
| vulture `unused import 'BackgroundStream'` (90 %, boundary) | `bash_tool.py:83` | observed in one earlier run of the same command | **waived**: `TYPE_CHECKING` import used by the runtime annotation at `:1375`; safe because `requires-python >=3.14` (PEP 649) | yes (boundary, not counted) |
| duplicated logic vs sibling tool | `bash_tool.py:1187-1251` vs `pwsh_tool.py` (K05 scope) | the `_execute_background` / `_continue_session` / `_process_output` / `_format_background_output` quartet is near-identical to the pwsh tool's | extract the shared session plumbing into `shell_common` (same owner rationale as `PWSH_ONESHOT_FLAGS`) | no |

long functions:

| function | file | LOC | decision |
|---|---|---|---|
| `__call__` | `bash_tool.py:740` | 325 | **split** (mandatory: above the 200 hard cap) |
| `detect_self_kill` | `safety.py:457` | 173 | **split** (cx/monolith; keep the public signature) |
| `_continue_session` | `bash_tool.py:1253` | 75 | keep |
| `_execute_background` | `bash_tool.py:1187` | 65 | keep |
| `_find_git_bash_windows` | `bash_tool.py:333` | 51 | keep |
| `_format_session_result` | `bash_tool.py:1369` | 51 | keep |

module LOC:

| module | LOC | decision |
|---|---|---|
| `bash_tool.py` | 1461 | **split (plan)**: keep discovery helpers (`:102-534`, ≈430 LOC) in a `bash_discovery.py`; the tool class keeps params + `__call__` + session plumbing; target ≈700 LOC as planned |
| `safety.py` | 665 | keep the file, split the function (cx, not LOC) |
| `shell_common.py` | 159 | keep (single home of the launcher machinery — correct deduplication) |
| `bash_fix.py` / `output_enhance.py` | 135 / 106 | keep (thin delegators to the native shim) |

## 4. S2 Potential issues (G2 detail)

| # | item (a–m) | severity | file:line | evidence | impact | fix | waiver |
|---|---|---|---|---|---|---|---|
| 1 | **(a) error contract** | clean | `bash_tool.py:692-738`, `:1066-1100`, `:1147-1185`, `:1253-1327` | every failure path returns `ToolError`; probes 7/18 assert `brief` for hardline, self-kill, forbidden, invalid pattern, task-not-found, unsupported, send-failure, failed-command, cancelled; `ValidationError` is raised by pydantic *before* the tool is called (by design) | - | - | - |
| 2 | **(b) async** | medium | `bash_tool.py:1124` (`subprocess.run(..., timeout=2)`), `:784` (`refresh_env_from_registry()`), `:197/:212/:234` (`subprocess.run` in `__init__`/discovery) | `grep subprocess.run` §G2; `_continuation_may_be_incomplete` is awaited-from `async def __call__` | a forbidden-rule-configured session blocks the event loop up to 2 s per call; a registry refresh walks the whole environment synchronously | run both through `asyncio.to_thread` (`ProcessTask` already spawns a worker thread for the child) | no |
| 3 | **(c) resources** | medium | `bash_tool.py:1159-1185`, `common.py:2213-2222` | probe 3 measures the kill: after the `ToolError("Timeout")` is returned the tick file keeps growing for ≈2 s before freezing; `_stop_function` calls `kill_child_tree(pid)` **without** `force=True` (`common.py:2220`) and escalates only at `common.py:2208`, after the reader thread notices `_stop_event` | a transient orphan window; on a child that ignores `taskkill /T` (no `/F`) the tree survives until the escalation path runs | pass `force=True` (or escalate after a bounded wait) in `_stop_function` | no |
| 4 | **(d) subprocess** | clean | `bash_tool.py:197`, `:212`, `:234`, `:1124`; `common.py:1979-2005` | all launches use argv lists (no `shell=True`), `capture_output=True`, explicit `timeout` on every probe; stdin is queued and written with `data.encode("utf-8", errors="replace")` (`common.py:2103`); `CREATE_NEW_PROCESS_GROUP` on Windows, `start_new_session=True` on POSIX; `taskkill /PID /T` on Windows / `killpg` on POSIX | - | - | - |
| 5 | **(e) path safety** | low (documented) | `bash_tool.py:580-600` (`_prepare_bash_cmd`) | unquoted `\x` is rewritten to `/x` (e.g. `echo a\nb` → `a/nb`), diverging from POSIX bash where `\n` escapes to `nb`; quoted text, escapes before metacharacters, `$'…'`, and single-segment relative paths are preserved (probe 9/10) | Windows-only cosmetic divergence on non-path backslashes; documented in the docstring | none (keep), or restrict the rewrite to `\` followed by `[A-Za-z0-9_.]` after a path-ish token | yes (documented behaviour) |
| 6 | **(f) injection** | low | `bash_tool.py:1147-1157`, `safety.py:423-443` | the tool *is* a shell (no `shell=True` misuse); `_compile_pattern` accepts an arbitrary model-supplied regex and `_pattern_kill_hit` runs `re.search(pattern, haystack)` over agent image names/cmdline; `wait_for_pattern` is searched against the whole output stream | a catastrophic user regex (ReDoS) can spin a CPU core; the haystacks are short, and the output search runs in the stream reader thread | bound compiled patterns (`regex` timeout) or document the risk | no (low) |
| 7 | **(g) secrets** | **high** | `bash_tool.py:825`, `:882`, `:1217`; `common.py:1910` (pinned SHA) | `ProcessTask.scrub_env` defaults to `False` and no bash spawn site passes it; probe 5 runs `RTK_DISABLED=1 env` + `printenv K04_GATE_API_KEY` **through the tool** and the secret value appears in the child output | every `*_KEY`/`*_TOKEN`/`*_SECRET` in the agent environment reaches the child shell and every grandchild script; inconsistent with the Python tool, which scrubs by default (`py/__init__.py:354`) | already owned by **X03-F1 / 90-findings F-13 → FP-11**: scrub at the spawn sites (or centrally in `_env_with_rg_bin_path`) with the existing `env_passthrough` opt-out. An uncommitted concurrent edit to `common.py` does exactly this (see §6/§8) | no |
| 8 | **(h) network** | clean | – | no network/URL/SSRF surface in the six in-scope modules (`grep` shows no `socket`/`urllib`/`requests`/`http` use) | - | - | - |
| 9 | **(i) limits** | medium | `bash_tool.py:1358-1367`, `common.py:835`, `:2103`, `:2017` | (i1) `>65536` chars → save+summarize and `output_truncated=True` (probe 04b asserts); (i2) the `OUTPUT_LIMIT=16384` export path replaces the text with a pointer but leaves `output_truncated=False` (probe 04 asserts the flip and the stale flag — reported as finding K04-F6); (i3) the `65536` literal is unnamed while `16384` has `OUTPUT_LIMIT`; (i4) UTF-8 boundaries are safe (`errors="replace"` incremental decoder; probes 13/21c); (i5) `max_lines` fold markers are asserted by probe 21b | a consumer trusting `output_truncated` concludes the full text is present when it is only a pointer to a temp file | set `output_truncated=True` on the export path in `_process_output`; name the 65536 constant | no |
| 10 | **(j) state** | low | `safety.py:140-142`, `:211-244`, `:260-294`; `bash_tool.py:417` | three module-level caches (`_AGENT_PIDS_CACHE`, `_AGENT_IMAGE_NAMES_CACHE`, `_AGENT_CMDLINE_CACHE`) memoized "for process life", plus `find_bash` `lru_cache(maxsize=1)`; no locks, no invalidation hook | a re-parented/restarted agent launcher keeps a stale ancestor-PID set, so the self-kill guard can miss the new parent (or block a recycled PID); `find_bash` caches the environment-dependent discovery for the whole process | add an optional `refresh=True`/invalidation entry point, or re-walk when `os.getppid()` changes | no (low) |
| 11 | **(k) params** | clean | `bash_tool.py:603-633`; `prompt_common.py:48-134` | probe 16/16b/17/17b/17c assert: alias `command`↔`cmd` (`model_fields["cmd"].alias`), `populate_by_name`, the three `mode` aliases (`interactive=True`→`interactive`, `run`→`execute`, `background`→`send`), `timeout` 1/900 accepted and 0/-1/901/1000 rejected, `max_lines` 3 accepted and 0/2/-5 rejected, empty `cmd` rejected for `execute`/`task_id` and allowed for `send`, and the JSON schema (`command` advertised, enum, `default=30`, `minimum/maximum`, `anyOf` null+`minimum:3`) | - | - | - |
| 12 | **(l) native parity** | clean (tested) | `output_enhance.py:66-70`, `:85-88`, `:101-103`; `safety.py:107`, `:126`; `bash_tool.py:573`; `bash_fix.py:120-135` | every native fast path has a pure-Python fallback resolved lazily (`_compat_tools()`), and `tests/native/test_shell_security_equivalence.py:92/140/196/237/282/314/349` asserts bit-identical results for redaction, env scrubbing, hardline detection, guidance, exit codes, failure hints and `bounded_append`; the SIGPIPE rule is evaluated *before* the native call (`output_enhance.py:66`) with the documented reason and is covered by `test_output_enhance.py::TestInterpretExitCode::test_sigpipe_in_pipeline_meaning` | - | note only: `command_detection_variants` (`safety.py:72`) always takes the pure-Python path while `check_hardline_blocked` takes the native one — asymmetric but equivalence-tested | - |
| 13 | **(m) library policy** | info (X01 owns) | `tests/test_bash_e2e.py:66` (`import json`), `:70` (`from difflib import unified_diff`), `:74`; `bash_tool.py:15` (orjson ✓), `:16` (regex ✓), `:113` (pybase64 ✓), `:111` (`gzip`, stdlib but not on the policy list) | AST/env evidence in §G2 of the gate file; the six source modules use `orjson`/`regex`/`pybase64` correctly | the two test-module hits are in a *test* corpus loader (out of the X01 src scope) | route to X01: `orjson` for the corpus reader, `rapidfuzz` for the diff | yes (report-only) |

open_high = 1 (row 7, owned by X03-F1/FP-11) · open_medium = 3 (rows 2, 3, 9) · waived = 2 (rows 5, 13).

## 5. S3 Coverage (G3 detail)

| behaviour | asserting test | status |
|---|---|---|
| `find_bash` resolution + smoke probe | `test_bash.py::TestFindBash::test_returns_path_on_this_system`, `::test_returns_basename_bash`; `TestFindGitBashWindows::test_*` (8) | covered |
| `_bash_subprocess_env` (MSYS opt-outs, user override, POSIX) | `test_bash.py::TestBashSubprocessEnv::test_win32_sets_msys_opt_out`, `::test_respects_user_overrides`, `::test_posix_untouched`, `::test_delegates_to_rg_bin_path` | covered |
| shell enable/disable matrix | `test_bash.py::TestWindowsShellExclusion::*` (5), `::TestConfiguredShellConfigRead::*` (9), `::TestConfiguredShellSelection::*` (7) | covered |
| `bash_fix` rewrites (paths, `cd /d`, `nul`, `/tmp`, wrappers, fallbacks, unsupported, heredoc/case/array scanners) | `test_bash.py::TestBashFix*::*` (~120), `tests/test_bash_e2e.py::TestCorpusLoading::*`, `tests/test_shell_common.py::TestPrepareBashCommand::*` | covered (host-gated subsets) |
| `_prepare_bash_cmd` / `_process_unquoted` (≈60 cases) | `test_bash.py::TestPrepareBashCmd::*`, `::TestBashBackslashPaths::*` (6) | covered |
| `_is_git_bash_install` / `_with_msystem_neutralized` | `test_bash.py::TestIsGitBashInstall::*` (6), `::TestMsystemNeutralizedCommand::*` (3), `::TestMSystemNeutralizationRealBash::*` | covered |
| `Bash.__call__` execute happy path / exit codes / pipefail | `test_bash.py::TestBashCall::test_echo_hello`, `::test_true_command`, `::test_false_command`, `::test_pipefail_surfaces_real_exit_code`, `::TestComplexCommands::*` | covered |
| expected non-zero exit (grep/SIGPIPE) | `test_bash.py::TestBashCall::test_grep_no_match_reports_success`, `::test_sigpipe_truncation_reports_success` | covered |
| hardline floor + obfuscation variants | `test_bash.py::TestShellSafetyWiring::test_hardline_block_returns_error_before_process_task`, `::test_hardline_block_obfuscated_spelling`, `::test_hardline_skipped_when_config_disabled`; `tests/test_shell_safety.py::TestHardlineBlocked::*` | covered |
| self-kill guard (own PID, image, loop var, config off) | `test_bash.py::TestShellSafetyWiring::test_self_kill_guard_*` (5); `tests/test_shell_safety.py::TestDetectSelfKill::*`, `::TestSelfKillHint::*` | covered |
| forbidden-command policy (source/generated/rtk) | `test_bash.py::TestBashFixToolIntegration::test_forbidden_*` (6) | covered |
| interactive start / continuation / unsafe fragment | `test_bash.py::TestBashInteractiveArgumentBuilding::*`, `::TestBashInteractiveIntegration::*`, `::TestBashFixToolIntegration::test_existing_interactive_session_input_*` | covered |
| `mode="send"` + `wait_for_pattern` (+ completion, invalid pattern) | `test_bash.py::TestBashBackgroundSendWaitForPattern::*` (4), `::TestBashBackgroundSendWaitForPatternIntegration::*` | covered |
| timeout shapes (guidance / silent hand-off / kill) | `test_bash.py::TestShellSafetyWiring::test_timeout_branch_*` (3), `::TestBashInactivityTimeout::*` (2), `::TestBashCall::test_timeout` | covered |
| long-output pipeline (dedup, fold, export, summarize) | `test_bash.py::TestBashOriginalSavedSuffix::*` (11), `::TestPowershellOriginalSavedSuffix::*` | covered |
| secret redaction wiring | `test_bash.py::TestShellSafetyWiring::test_redaction_applied_in_process_output`, `::test_redaction_disabled_by_config`; `tests/test_output_enhance.py::TestRedactSensitiveOutput::*` (14) | covered |
| `output_enhance` meanings / hints / guidance | `tests/test_output_enhance.py::TestInterpretExitCode::*`, `::TestIsExpectedExit::*`, `::TestAnnotateFailure::*`, `::TestForegroundBackgroundGuidance::*` | covered |
| native↔Python equivalence | `tests/native/test_shell_security_equivalence.py::test_*_equivalence` (7) | covered |
| **foreground `execute` + `wait_for_pattern` that never matches → `_stop_after_timeout`** | none | **GAP** — missing lines 899-931 |
| **`CancelledError` during the foreground one-shot** | none in the G3 file set (probe 18c covers it, but probes are not part of the G3 command) | **GAP** — missing lines 941-951 |
| **oversized startup script → `KIMIX_BASH_PAYLOAD`** | none | **GAP** — missing lines 136-137 |
| **macOS bash discovery (`_git_bash_for_macos`, `_bash_candidates_macos`)** | none (Windows host) | **GAP** — missing 432-450 |
| **`_find_git_bash_windows` fallback branches (exec-path miss, hard-coded dirs miss, PATH)** | partial (`TestFindGitBashWindows` covers the injectable halves) | **GAP** — missing 366-375, 388-400, 405, 414 |
| **POSIX `_posix_ppid` / ancestor walk** | none (Windows host) | **GAP** — missing `safety.py:154-162`, 222-229, 237-242 |
| **`_name_kill_hit` wildcard + `_pattern_kill_hit` regex-error fallback** | none | **GAP** — missing `safety.py:412-416`, 436-442 |
| **`is_expected_exit` when the native kernel lacks the symbol** | none | **GAP** — missing `output_enhance.py:89` |

summary: behaviours mapped **32**, covered **24**, gaps **8** (all with a proposed test below),
plus the 82 uncovered statements/branches listed in `Missing` for `bash_tool.py` (waived table below).

waived lines + budget (corrected G3 run, `bash_tool.py`: Stmts 590 / Miss 82 / Branch 260 / BrPart 41 → **82.0 %**):

| waived lines | what they are | why waived |
|---|---|---|
| `136-137` | `_bash_argv_env` payload branch | ≥6000-char command path; **proposed test** `tests/unit/tools/test_bash_gate.py::test_argv_payload_fallback_env_carries_script` (assert `KIMIX_BASH_PAYLOAD` in env iff `len(script) > 6000`) |
| `218-221`, `241-249` | `_where_git_executables` / `_git_exec_path` failure paths | host-specific `where.exe`/`git --exec-path` errors; **proposed test** `tests/test_bash.py::TestFindGitBashWindows::test_git_exec_path_failures_degrade` |
| `366-375`, `388-400`, `405`, `414` | `_find_git_bash_windows` fallback ladder | **proposed test** `tests/unit/tools/test_bash_gate.py::test_git_bash_resolution_ladder` (monkeypatched `_bash_runs`/`shutil.which`, assert the returned path per rung) |
| `432-450` | macOS discovery | **proposed test** `tests/unit/tools/test_bash_gate.py::test_macos_bash_discovery` (patched `sys.platform`/`shutil.which`/`Path.is_file`) |
| `494`, `575`, `653`, `667`, `792`, `806`, `809`, `812`, `875`, `878`, `1042`, `1117`, `1201`, `1207`, `1210`, `1213`, `1279`, `1304->1306`, `1394`, `1451`, `1453`, `1456`, `1459->1461` | defensive/config-gated single branches (early returns, second forbid/hardline/self-kill re-checks on the rtk-rewritten command, empty-branch tails) | low value individually; **proposed test** `tests/unit/tools/test_bash_gate.py::test_error_branch_matrix` (parametrized over the 9 `ToolError` briefs) |
| `770` | unreachable "Empty command" return | **DELETE the branch** (finding K04-F4) rather than cover it |
| `823-824`, `827->833`, `884->891`, `1179->1181`, `1219->1226` | interactive-without-cmd and stream-`None` paths | **proposed test** `tests/unit/tools/test_bash_gate.py::test_interactive_without_cmd_stream_none` |
| `899-931` | foreground `wait_for_pattern` timeout | **proposed test** `tests/unit/tools/test_bash_gate.py::test_execute_wait_pattern_timeout_kills` |
| `941-951` | `CancelledError` | **proposed test** already written: `test_probe18c` (cancellation half) — move/duplicate it into the G3 file set |
| `1133-1135` | `_continuation_may_be_incomplete` OSError fail-closed | **proposed test** `tests/unit/tools/test_bash_gate.py::test_continuation_parse_oserror_fails_closed` |

`waived_lines = 82`, `waiver_budget = 100` (the plan's §4.4 K04 block declares no per-tool budget;
the reviewer sets 100 and records it here — every line above has a proposed test or a delete plan).

Additional G3 methodology finding (reported as K04-F12): the verbatim gate target
`--cov=src/kimix/tools/file/bash/bash_tool.py` is never resolved by coverage
(`CoverageWarning: Module … was never imported`), so **the largest in-scope module is absent from
the table** and the printed TOTAL is unstable between runs of the same command (436 → 1042
statements, 90.63 % → 89.01 %). Hypothesis (not proven): the instability comes from concurrent
pytest-cov runs sharing the same `.coverage` file in this working tree. Use the dotted form
`--cov=kimix…bash_tool` (82.0 % measured).

## 6. G4 Behavioural probes

`tests/unit/tools/test_bash_gate.py` — 45 probe functions, 72 collected cases (parametrized).

| probe | assertion | result |
|---|---|---|
| 01 interactive → `task_id` → stdin continuation | `ToolOk`, `brief`, marker echoed by the continued shell, `exit` then the worker thread settles (bounded poll) | pass |
| 01b `interactive=True` alias with empty `cmd` | `ToolOk`, `task_id` starts with `bash` | pass |
| 02 `wait_for_pattern` blocks then matches | `wait_matched: true` **and** elapsed ≥0.9 s for `sleep 1; echo …` | pass |
| 02b pattern timeout | `status: running` + job still registered | pass |
| 02c settled job | `status: completed` (mocked `alive=False`) | pass |
| 02d matched job settles and stays retrievable | worker thread gone, `task_id` still in the registry | pass |
| 03 timeout kills the process | `ToolError("Timeout")`, registry empty, tick file frozen (two equal samples) and `DONE` never written | pass |
| 04 long-output promotion boundary | `OUTPUT_LIMIT == 16384`; below → verbatim text; above → `[Output too large, … .txt]` pointer + `output_truncated is False` (characterizing K04-F6) | pass |
| 04b >65536 summarize | one summarize call, `[PROBE04 SUMMARY]`, `output_truncated: true`, original path non-None | pass |
| **05 child env scrubbed** | `K04_GATE_API_KEY`/`_ACCESS_TOKEN`/`_CLIENT_SECRET` absent from `RTK_DISABLED=1 env` **and** `printenv <name>` must fail | **FAIL at the pinned SHA** (secret printed); green only with the uncommitted FP-11 edit |
| 05b plain variables survive | `printenv K04_GATE_PLAIN_VAR` → `ToolOk` + value | pass |
| 06 redaction of an echoed token | `ghp_…` absent, `[REDACTED]` present, non-secret marker untouched | pass |
| 07 dangerous command briefs | hardline `brief` + self-kill `brief`, `ProcessTask` never constructed | pass |
| 07b obfuscated `r\m -rf /` | still `Blocked (hardline)` | pass |
| 08 rtk wrap (4 params) | `git status`→`rtk git status`, `ls -la`→wrapped, `find … '*.py'` **not** wrapped, `read var` not wrapped | pass |
| 08b rtk unavailable | command unchanged | pass |
| 09 documented `bash_fix` rewrites (10 params) | exact `src`→`dest` for drive paths, `cd /d`, `nul`/`NUL`, `/c/x`, wrapper unwrap, preserved quoted/single-segment paths, empty | pass |
| 09b fallback + unsupported | `replacements == ("rev",)`, `rev() {`/`export -f rev` present; `journalctl` → `unsupported == ("journalctl",)` + reason, text unchanged | pass |
| 09c `/tmp` mount | equals the host temp dir, `path_changes == ("/tmp/x",)` | pass |
| 10 quoting/escaping round-trip (9 params) | quotes, `$'…'`, embedded quotes, backslash escapes, newlines, unicode — byte-identical | pass |
| 10b/10c unquoted backslash | win32 rewrites unquoted, preserves single-quoted; linux no-op | pass |
| 11 POSIX vs Windows (6 params) | win32 rewrites, linux/darwin byte-for-byte no-op (both branches forced, documented in the docstring) | pass |
| 11b MSYSTEM neutralization | win32+Git Bash prefixed, win32+MSYS2 untouched, linux untouched | pass |
| 11c `_prepare_bash_cmd` / `_is_git_bash_install` | win32-only rewrite; `usr/bin` detection; `None` → False | pass |
| 12 exit codes | `exit 7`→`exit_code: 7`, `exit 0`, unknown command→`exit_code: 127` + "command was not found" | pass |
| 13 binary/non-UTF-8 | 3× `head -c 64 /dev/urandom` and `printf 'a\377\376b\n'` → no crash, `str` output, `exit_code: 0` | pass |
| 14 very long single line | `"b"×16385` → `b ×16385 [+16384 chars elided]`, `truncated is True`, original saved | pass |
| 14b short line untouched | verbatim, `truncated is False`, no paths | pass |
| 15 shared launcher arguments | `PWSH_ONESHOT_FLAGS`, `bash -l -c` / `bash -c` / `bash -l <file>`, pwsh `-NoP -NonI -Exec Bypass -NoL -Command` + try/catch wrapper, PS5.1 downgrade reuses the flags | pass |
| 16 params + aliases | `command`↔`cmd`, all 6 fields, 3 mode aliases, `model_fields["cmd"].alias` | pass |
| 16b JSON-schema round trip | `command` advertised, enum, `default=30`, min 1/max 900, `max_lines` anyOf null+`minimum:3` | pass |
| 17/17b/17c boundaries | `timeout` 1/900 ok, 0/-1/901/1000 rejected; `max_lines` 3 ok, 0/2/-5 rejected; empty `cmd` rejected for `execute`/`task_id`, allowed for `send` | pass |
| 18/18b/18c/18d error briefs | forbidden, invalid pattern, task-not-found, unsupported, send-failure, failed-command, cancelled, expected-non-zero-as-success | pass |
| 19 unreachable empty-command branch | `ValidationError` for execute+empty **and** `send`+empty → `ToolOk` (the branch is dead) | pass |
| 20 timeout shapes | guidance / silent hand-off (not killed) / stalled (killed, registry empty) | pass |
| 21/21b/21c | timeout forwarded to the monitor, `max_lines` fold forwarded, `errors="replace"` decoder | pass |

summary: required **15**, present **45** functions / **72** cases.
params-field coverage: `cmd`/`command`, `mode`, `timeout`, `task_id`, `wait_for_pattern`, `max_lines` — all exercised.
alias coverage: `command`, `interactive=True`, `run`, `background` — all exercised.
boundary coverage: `timeout` 1/900 (at) + 0/901 (out), `max_lines` 3 (at) + 2 (out), empty input, 16384/16385 and 65536/65537 output thresholds, 16385-char single line.
side effects: the timeout probe uses `tmp_path` and asserts the exact frozen tick count; the JSON-schema probe asserts no filesystem state.
**G4 result: fail** — one probe (`05`) is red at the pinned SHA; it is the true signal for X03-F1/FP-11
(faking it by asserting `scrub_child_env()` in isolation would hide the end-to-end leak).

## 7. G5/G6 Evidence

| command | result |
|---|---|
| `uv run pytest tests -q` | exit 2, `1 error during collection` (`ValueError: cannot read corpus file … tests\bash\cases.json`) — **byte-identical to `reviews/tools/00-baseline.txt:27-33`** |
| `uv run pytest kimi-cli/tests -q` | exit 1, `43 failed, 5241 passed, 105 skipped, 1 xfailed, 1 xpassed`; set-difference of the `FAILED` lines vs the baseline = **NEW ∅ / GONE ∅** (baseline 43 failed / 5155 passed / 101 skipped; the extra passes come from sibling units' new test files) |
| `uv run pytest tests/test_bash.py tests/test_shell_common.py tests/test_shell_safety.py tests/test_output_enhance.py tests/native/test_shell_security_equivalence.py -q` | 1603 passed, 1 skipped (all skips are host-gated: `-rs` lists the platform skips only) |
| `uv run pytest tests/test_bash_e2e.py -q` (G3 additional) | exit 2 — **pre-existing baseline failure**, the default corpus `tests/bash/cases.json` does not exist anywhere in the repo (verified with `find`); the plan's "run with the default corpus" gate cannot be green without that file |
| `uv run tools/syntax_check.py tests/unit/tools/test_bash_gate.py` | Syntax OK |
| `uv run tools/git_diff.py tests/unit/tools/test_bash_gate.py` | UNTRACKED/new file, diff = exactly the probe suite (`@@ -0,0 +1,1077 @@`) |
| `uv run ruff check tests/unit/tools/test_bash_gate.py` | All checks passed |
| `pyproject.toml` | untouched (`git status --porcelain -- pyproject.toml` empty → no `uv sync` needed) |

G6 (orchestrator): re-run the same commands and compare `complexity_max=52`, `longest_fn_loc=325`,
`actual coverage=82.0` (module `kimix.tools.file.bash.bash_tool`), `probe count=45/72`, the two suite
results, and `git_sha`; **note the HEAD movement below** and the uncommitted `common.py` edit.

## 8. Git provenance

- `git rev-parse HEAD` at review time — **`52192b7c3a1baa66c96e0015d2868f3b106fc043`** = line 1 of `reviews/tools/gates/K04.txt`.
- `git status --porcelain` at review start — **not empty**: the tree already carried sibling units' artifacts
  (`?? .c08tmp/`, `?? reviews/tools/gates/C03.txt`, `C08.txt`, `C09.txt`, `C10.txt`, `?? reviews/tools/gates/X01.txt`)
  → `worktree_clean_at_start = false` (parallel wave; nothing of mine existed yet).
- **HEAD moved during the review**: `52192b7c → 9ce4db12` via four orchestrator commits
  (`72257564` gate harness, `37850165` fix(FP-08), `7aa84025` review(P2+P3) 36 reports, `9ce4db12` fix(FP-05)).
  The **only** in-scope source change in that range is FP-08's dequoting of the
  `'BackgroundStream' | None` annotation (`bash_tool.py:1375`, plus two lines in the out-of-scope
  `pwsh_tool.py`): no LOC, complexity or behaviour change, so every citation and every metric above remains
  valid (the metric baseline `00-metrics-baseline.json` matches the current numbers exactly).
- **Concurrent uncommitted edit (not mine)**: `src/kimix/tools/common.py` (mtime 12:01:46) adds
  `scrub_child_env(dict(base))` in `_env_with_rg_bin_path` and flips `ProcessTask.scrub_env` to `True`.
  This is the FP-11 fix. It flips probe 05 from red to green: at the pinned SHA (and at HEAD, which still
  has `scrub_env: bool = False`) probe 05 is red; with the working-tree edit the whole suite is green
  (`72 passed`). Both runs are in the evidence file. **G4 is therefore reported as `fail` at `git_sha`.**
- **Evidence-file integrity**: the parent gate harness wrote its own K04 sweep log into
  `reviews/tools/gates/K04.txt` at 12:04:29 (its `_harness.log` records `K04: ruff=fail c901=fail
  metrics=fail cx_max=52 longest=325 pytest=fail cov=fail`). It displaced the review-start status block;
  this review re-ordered the file so that lines 1-2 satisfy the plan again and preserved the harness log
  verbatim in the APPENDIX at the end.
- commit SHA (filled in by the orchestrator after acceptance): …
- tag: `tools-review/p2-w<N>`
- `git diff --stat tools-review/p0-base HEAD -- <in-scope files>`:
  `src/kimix/tools/file/bash/bash_tool.py | 2 +-` and `src/kimix/tools/file/bash/pwsh_tool.py | 3 ++-`
  (the FP-08 annotation change); **no in-scope test file changed since the base tag**.

## 9. Duplication / native-parity / policy notes

- **Duplication**: `bash_tool.py` and `pwsh_tool.py` (K05) duplicate the whole session plumbing
  (`_execute_background`, `_continue_session`, `_process_output`, `_format_session_result`,
  `_format_background_output`) — ~300 LOC of near-identical code in the two files. `shell_common.py`
  already owns the launcher/fixer machinery, so that is the natural home; the two tools must keep
  distinct `_process_output` config gates (`redact_secrets` is read per tool).
- **Native parity**: `bash_fix.py`, `process_pwsh.py` and `output_enhance.py`/`safety.py` are thin
  dispatch shells over `bin/kimix_native/*` with pure-Python fallbacks; `bash_tool.py` keeps its own
  `_shell` handle for `_process_unquoted` (`:540-575`). All divergences are pinned by
  `tests/native/test_shell_security_equivalence.py`; the one hand-ordered rule (SIGPIPE) is documented
  and tested. No file in `bin/kimix_native/` is in this unit's scope.
- **Library policy (X01 owns)**: the six source modules comply (`orjson`, `regex`, `pybase64`).
  `tests/test_bash_e2e.py:66/70` use stdlib `json` + `difflib` — report-only, test-scope.
- **`max_lines` fold / `output_enhance` naming**: the plan's probe 14 says "folded per `output_enhance`
  rules", but `output_enhance.py` contains no folding; the single-line fold
  (`b ×16385 [+16384 chars elided]`) is produced by `kimi_cli`'s micro-compress stage inside
  `_token_filter_output` (`common.py:1669-1694`). Probe 14 pins the real behaviour; the plan wording is
  inaccurate (hypothesis: the plan assumed the folding lived in `output_enhance`).

## 10. Verdict + JSON

**verdict: major.** Three hard G1 breaches (`loc 1461`, `__call__ 325`, `cx 52`), five ruff errors,
one open high G2 item (child-env scrubbing at the pinned SHA — owned by X03-F1/FP-11) and one red G4
probe for the same reason. Everything else is green: coverage 82.0 % for `bash_tool.py` (floor 80),
1603 in-scope tests passing, both suites baseline-identical, no regression, no ORPHAN, no
native-parity divergence. `needs_fix_phase = true` (split `__call__`, split `detect_self_kill`, delete
the dead branch, sort imports, renaming the `65536` constant, and let FP-11 land the env scrub).

```json
{
  "id": "K04",
  "tool": "bash",
  "files": [
    "src/kimix/tools/file/bash/bash_tool.py",
    "src/kimix/tools/file/bash/safety.py",
    "src/kimix/tools/file/bash/shell_common.py",
    "src/kimix/tools/file/bash/bash_fix.py",
    "src/kimix/tools/file/bash/output_enhance.py",
    "src/kimix/tools/file/bash/__init__.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {"ruff": "fail", "complexity_max": 52, "longest_fn_loc": 325,
                      "loc_regression": "none", "vulture_hits": 1, "vulture_waived": 1},
    "G2_risk": {"items_answered": 13, "open_high": 1, "open_medium": 3, "waived_medium": 2},
    "G3_coverage": {"module": "kimix.tools.file.bash.bash_tool", "floor": 80,
                    "actual": 82.0, "waived_lines": 82, "waiver_budget": 100},
    "G4_behavior": {"probe_file": "tests/unit/tools/test_bash_gate.py",
                    "probes_required": 15, "probes_present": 45, "result": "fail"},
    "G5_regression": {"root_suite": "pass", "cli_suite": "pass",
                      "syntax_check": "pass", "git_diff": "pass"},
    "G6_independent_rerun": "pending"
  },
  "findings": [
    {"severity": "high", "axis": "risk",
     "title": "bash children inherit the full agent env (no scrub_child_env) — probe 5 red",
     "file": "src/kimix/tools/file/bash/bash_tool.py:825",
     "evidence": "ProcessTask.scrub_env defaults False (common.py:1910 at the pinned SHA, also at HEAD) and none of bash_tool.py:825/:882/:1217 passes it; probe test_probe05_child_env_scrubbed runs `RTK_DISABLED=1 env` and `printenv K04_GATE_API_KEY` through the tool and the value is printed",
     "impact": "every *_KEY/*_TOKEN/*_SECRET in the agent environment is readable by the child shell and every grandchild script; inconsistent with the Python tool which scrubs by default",
     "fix": "already owned by X03-F1 / 90-findings F-13 -> FP-11: scrub at the spawn sites or centrally in _env_with_rg_bin_path, with the existing env_passthrough opt-out",
     "test_to_add": "tests/unit/tools/test_bash_gate.py::test_probe05_child_env_scrubbed"},
    {"severity": "high", "axis": "simplicity",
     "title": "bash_tool.py 1461 LOC (2.1x the planned 700) with a 325-LOC __call__",
     "file": "src/kimix/tools/file/bash/bash_tool.py:740",
     "evidence": "review_metrics loc=1461, longest_fn=__call__ loc=325 cx=51; plan 4.4 K04 G1 requires ~700 LOC and max_fn_loc<=120 (hard cap 200)",
     "impact": "hard G1 fail; a 325-line async entry point with 50 branches is the top change-risk surface in the tool and cannot be reviewed or tested exhaustively",
     "fix": "split (plan): move discovery helpers (lines 102-534) to bash_discovery.py and extract _run_interactive_start/_run_one_shot/_finish_one_shot from __call__",
     "test_to_add": "tests/test_bash.py::TestBashCall (existing) plus a per-extracted-function probe"},
    {"severity": "high", "axis": "simplicity",
     "title": "safety.detect_self_kill complexity 46 (ruff) / 52 (metrics) vs the <=12 budget",
     "file": "src/kimix/tools/file/bash/safety.py:457",
     "evidence": "ruff C901 46 > 12; review_metrics module_complexity=52, branches=51, loc=173",
     "impact": "hard G1 fail; the self-kill guard is a security control whose 5 kill-family branches are interleaved, so a new family can silently shadow another",
     "fix": "extract one predicate per family (_posix_kill_hits, _taskkill_hits, _stop_process_hits, _pkill_hits, _wmic_hits) and keep detect_self_kill as a thin dispatcher",
     "test_to_add": "tests/test_shell_safety.py::TestDetectSelfKill (existing) per extracted predicate"},
    {"severity": "medium", "axis": "simplicity",
     "title": "unreachable \"Empty command\" ToolError branch (dead code)",
     "file": "src/kimix/tools/file/bash/bash_tool.py:769",
     "evidence": "mode==\"send\" returns at :766 and shell_cmd_required_validator (prompt_common.py:124-127) rejects execute+empty; line 770 is in the coverage Missing column; probe 19 asserts both halves",
     "impact": "a reader believes empty input is validated at the tool layer; the real rejection is a pydantic ValidationError, so the ToolError contract differs from what the code implies",
     "fix": "delete lines 769-774",
     "test_to_add": "tests/unit/tools/test_bash_gate.py::test_probe19_empty_command_branch_is_unreachable"},
    {"severity": "medium", "axis": "risk",
     "title": "blocking subprocess.run / registry refresh inside async __call__",
     "file": "src/kimix/tools/file/bash/bash_tool.py:1124",
     "evidence": "_continuation_may_be_incomplete runs subprocess.run([bash,'-n','-c',cmd], timeout=2) from the awaited __call__ path (also :784 refresh_env_from_registry())",
     "impact": "up to 2 s of event-loop stall per call whenever forbidden_commands are configured; other tools' streams (job_output SSE readers) freeze meanwhile",
     "fix": "wrap both in asyncio.to_thread",
     "test_to_add": "tests/unit/tools/test_bash_gate.py::test_continuation_parse_does_not_block_loop"},
    {"severity": "medium", "axis": "risk",
     "title": "long-output export replaces the text but reports output_truncated=false; 65536 is an unnamed literal",
     "file": "src/kimix/tools/file/bash/bash_tool.py:1358",
     "evidence": "probe 04: 16384 chars -> verbatim; 16385 -> \"[Output too large, exported to file: …]\" with output_truncated False; common.py:835 exports above OUTPUT_LIMIT while _process_output only sets the flag for the filter/rtk/summarize stages",
     "impact": "a consumer trusting output_truncated concludes the full text was returned when it received a pointer; the 65536 summarize threshold is a magic number distinct from OUTPUT_LIMIT",
     "fix": "set output_truncated=True when the export path fires; hoist 65536 into a named constant",
     "test_to_add": "tests/unit/tools/test_bash_gate.py::test_probe04_long_output_promotion_threshold_boundary"},
    {"severity": "medium", "axis": "risk",
     "title": "timeout kill reports success ~2 s before the process tree is actually dead",
     "file": "src/kimix/tools/file/bash/bash_tool.py:1159",
     "evidence": "probe 03: after ToolError('Timeout') the tick file keeps growing for ~2 s before freezing (12 -> 18 -> 23); common.py:2220 kill_child_tree(pid) without force=True, escalation only at common.py:2208",
     "impact": "a 'killed' command can still write files / hold ports for a couple of seconds after the tool answered, so an immediate retry races the orphan",
     "fix": "call kill_child_tree(pid, force=True) in _stop_function (or escalate after a bounded wait)",
     "test_to_add": "tests/unit/tools/test_bash_gate.py::test_probe03_timeout_kills_the_process"},
    {"severity": "low", "axis": "simplicity",
     "title": "ruff lint failures + hot-path function-local imports in the bash tool family",
     "file": "src/kimix/tools/file/bash/output_enhance.py:27",
     "evidence": "ruff check exit 1 (I001 x3, E402 x2); grep shows function-local imports at bash_tool.py:999/:1092/:1176/:1255/:1313 and gzip/pybase64 at :111-113",
     "impact": "lint gate red; per-call import lookups on the hot path",
     "fix": "sort the native_loader imports, move the compatibility re-exports behind an explicit module tail, hoist the hot-path imports",
     "test_to_add": "n/a (static lint)"},
    {"severity": "low", "axis": "risk",
     "title": "mode='send' with an empty cmd still spawns a bash process",
     "file": "src/kimix/tools/file/bash/bash_tool.py:1187",
     "evidence": "probe 19: BashParams(cmd='', mode='send') passes validation and _execute_background starts ProcessTask with an empty -c payload, returning a task_id",
     "impact": "a model typo creates a useless background task and consumes a task-id slot",
     "fix": "reject empty cmd for send as well (or short-circuit before ProcessTask.start)",
     "test_to_add": "tests/unit/tools/test_bash_gate.py::test_probe19_empty_command_branch_is_unreachable"},
    {"severity": "low", "axis": "risk",
     "title": "agent PID/image/command-line caches are memoized for process life",
     "file": "src/kimix/tools/file/bash/safety.py:140",
     "evidence": "_AGENT_PIDS_CACHE/_AGENT_IMAGE_NAMES_CACHE/_AGENT_CMDLINE_CACHE have no invalidation hook (safety.py:211-294)",
     "impact": "if the launcher re-parents (restart, wrapper upgrade) the guard compares against a stale ancestor set and can let a self-kill through",
     "fix": "re-walk when os.getppid() changes, or expose an invalidate() used by tests",
     "test_to_add": "tests/test_shell_safety.py::TestSelfKillHint::test_cache_invalidated_on_ppid_change"},
    {"severity": "info", "axis": "cross-cutting",
     "title": "G3 verbatim --cov target never measures bash_tool.py; TOTAL unstable",
     "file": "src/kimix/tools/file/bash/bash_tool.py",
     "evidence": "coverage warns 'Module src/kimix/tools/file/bash/bash_tool.py was never imported'; the module is absent from both verbatim runs and TOTAL differs (436 stmts/90.63% vs 1042 stmts/89.01%) for the same command",
     "impact": "the coverage gate silently does not measure the largest in-scope module, so a green G3 says nothing about bash_tool.py",
     "fix": "use --cov=kimix.tools.file.bash.bash_tool (82.0% measured) in the K04 gate block",
     "test_to_add": "n/a (gate-command fix)"},
    {"severity": "info", "axis": "cross-cutting",
     "title": "library-policy hits in the bash tool test corpus loader (X01 owns)",
     "file": "tests/test_bash_e2e.py:66",
     "evidence": "import json (:66) and from difflib import unified_diff (:70) in the e2e corpus harness",
     "impact": "violates the documented third-party-first policy if tests are in scope for G-POLICY",
     "fix": "orjson for the corpus reader, rapidfuzz for the failure diff (X01 owns the finding)",
     "test_to_add": "tests/test_bash_e2e.py::TestCorpusLoading (existing, re-run after the swap)"}
  ],
  "coverage": {
    "behaviors_mapped": 32,
    "covered": 24,
    "gaps": 8,
    "gap_list": [
      "foreground execute + wait_for_pattern that never matches -> _stop_after_timeout (bash_tool.py:899-931)",
      "CancelledError during the foreground one-shot (bash_tool.py:941-951; only the G4 probe covers it)",
      "oversized startup script -> KIMIX_BASH_PAYLOAD (bash_tool.py:136-137)",
      "macOS bash discovery (bash_tool.py:432-450)",
      "_find_git_bash_windows fallback ladder (bash_tool.py:366-375, 388-400, 405, 414)",
      "POSIX _posix_ppid ancestor walk (safety.py:154-162, 222-229, 237-242)",
      "_name_kill_hit wildcard + _pattern_kill_hit regex-error fallback (safety.py:412-416, 436-442)",
      "is_expected_exit when the native kernel lacks the symbol (output_enhance.py:89)"
    ]
  },
  "gate_evidence": "reviews/tools/gates/K04.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "worktree_clean_at_start": false,
  "needs_fix_phase": true,
  "duplication_notes": [
    "bash_tool.py vs pwsh_tool.py duplicate ~300 LOC of session plumbing (_execute_background, _continue_session, _process_output, _format_session_result, _format_background_output); shell_common.py is the natural shared home",
    "shell_common.py is a correct de-duplication (single home of the (argv, env) machinery + PWSH_ONESHOT_FLAGS)",
    "bash_fix.py / process_pwsh.py / output_enhance.py / safety.py correctly delegate to one canonical implementation in bin/kimix_native (no divergent copies in src)",
    "G3 gate command binds --cov to a path target that coverage never imports (methodology duplication of coverage config; see finding K04-F11)"
  ]
}
```
