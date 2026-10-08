# 90 — Consolidated findings (Phase 3 triage)

Inputs: `00-baseline.txt`, `00-registry.txt`, `00-metrics-baseline.json`,
`gates/<ID>.json` (36 mechanical gate runs), `X01`…`X06` sweep reports.

Dedup key = (file, root cause). One finding → one fix package.
Severity: **high** = wrong result / crash / secret leak / gate cannot pass;
**medium** = policy or threshold breach, review-blocking;
**low** = hygiene; **info** = recorded, no action.

| # | sev | axis | finding | evidence (file:line) | fix package |
|---|---|---|---|---|---|
| **F-01** | high | correctness | `_build_image_delivery_limit_error` is **called but never defined**; the "image too large / mipmap failed" path raises `NameError` instead of returning `ToolError` | `kimi-cli/src/kimi_cli/tools/file/read_media.py:427` (F821) | FP-08 |
| **F-02** | high | correctness | annotation `'BackgroundStream'` used but never imported into the module | `src/kimix/tools/file/bash/pwsh_tool.py:1054` (F821) | FP-08 |
| **F-03** | medium | correctness | `__all__` exports `EchoTrimLimit`, which does not exist → `from conflict_detect import *` raises `AttributeError` | `kimi-cli/src/kimi_cli/tools/file/conflict_detect.py:30` (F822) | FP-08 |
| **F-04** | low | correctness | duplicate `import shlex` (F811) plus a group of unused imports | `src/kimix/tools/file/run.py:8,52` | FP-08 |
| **F-05** | high | harness | the ROOT suite cannot be collected at all: `tests/bash/cases.json` is missing and `pytest_generate_tests` reads it at collection time | `tests/test_bash_e2e.py:143`, `tests/conftest.py` | FP-00 |
| **F-06** | high | harness | 38 pre-existing failures in `kimi-cli/tests/native/test_grep_engine_parity.py` after "remove native module grep"; the suite still expects the removed engine | `reviews/tools/00-baseline.txt:2357-2392` | **resolved (FP-10)**: the suite was left behind by `fe10182b` and contradicts the replacement `test_grep_ripgrep_only.py` ("the grep tool must always use ripgrep"). Retired + `test_grep_engine_parity_retired.py` pins the retirement. `kimi-cli/tests/native` = 323 passed, 0 failed.
| **F-07** | medium | harness | 2 failures: base-agent manifests no longer list the single merged todo tool | `kimi-cli/tests/core/test_toolset_todo_redirects.py` | FP-10 |
| **F-08** | medium | harness | 2 failures in relative-path-outside-work-dir assertions | `kimi-cli/tests/tools/test_read_file.py::test_read_with_relative_path_outside_work_dir` | FP-10 |
| **F-09** | medium | library policy | `import json` at module line 1 (only used for a `JSONDecodeError` except arm) | `kimi-cli/src/kimi_cli/tools/__init__.py:1` | FP-05 |
| **F-10** | medium | library policy | `import re` for two compile patterns | `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:5` | FP-05 |
| **F-11** | medium | library policy | `import difflib` for `unified_diff`; also a dead `import xxhash` | `src/kimix/tools/swarm/best_of_n.py:23` (+ `:147`) | FP-05 |
| **F-12** | info | duplication | `fetch_url` implemented twice (C12 `kimi_cli.tools.web.fetch` aiohttp vs K14 `kimix.tools.web.fetch_url` Playwright) | `tools/gate_dup.py` → 2 implementations | **resolved (FP-02)**: they are parallel impls for disjoint runtimes; delegation impossible (kimi_cli has no Playwright and C12 needs a kimi_cli Config/Runtime) and the name is documented at `kimix_api/references/api.md:981`. Waived with a written rationale + pinned by `tests/test_fetch_url_parity.py`. |
| **F-13** | high | security | child processes spawned by `bash`/`pwsh`/`Run`/`python` inherit the **full parent environment**: `ProcessTask.scrub_env` defaults to `False` and none of the 8 shell spawn sites enables it, so `scrub_child_env` never runs | `src/kimix/tools/common.py:1910,1966-1968`; `bash_tool.py:825,882,1217`; `pwsh_tool.py:535,595,914`; `run.py:424` (X03-F1) | FP-11 |
| **F-14** | medium | policy/dead code | 9 tool classes were not reachable from any manifest: `HashRead`, `HashEdit`, `plus`, `compare`, `panic`, `Mkdir`, `Rm`, `FindStr`, `ParserTool` | `reviews/tools/00-registry.txt` | **resolved (FP-03)**: 5 deleted, 2 registered, 2 allowlisted — G-ORPHAN now PASS (0 orphans) |
| **F-15** | medium | dead code | stale `__pycache__` trees hold `.pyc` for modules deleted from the source tree (e.g. `src/kimix/tools/eval/`) | `gates/X05.txt` | FP-06 |
| **F-16** | high | coverage | **no G4 probe suite exists for any of the 36 tools**; every `G4_behavior` gate is red | `gates/<ID>.json` → `probes_present: 0` | FP-07 |
| **F-17** | high | coverage | `Mkdir` / `Rm` (K17/K18) have **no tests at all** | `tools/gen_inventory.py` → K17/K18 test list empty | FP-07 |
| **F-18** | medium | simplicity | every one of the 84 in-scope modules fails `uv run ruff check` (I001 65, E501 101, UP* 12, SIM* 12, F841 11, F401 11, B* 9, N* 4, E741 4, E402 9, …) | `gates/<ID>.txt` §ruff; `ruff check --statistics` | FP-09 |
| **F-19** | medium | simplicity | 90 functions exceed cyclomatic complexity 12; worst `run.py::__call__` = 92, `grep_local.py::_postprocess` = 77, `write.py::__call__` = 64 | `00-metrics-baseline.json` | FP-07 / backlog |
| **F-20** | low | simplicity | 29 functions exceed 120 LOC; worst `pwsh_tool.py::__call__` = 370 | `00-metrics-baseline.json` | backlog |
| **F-21** | info | simplicity | 6 modules ≥ 1200 LOC require a mandatory keep/split decision (`todo/__init__.py` 2452, `grep_local.py` 2355, `common.py` 2342, `providers.py` 2077, `read.py` 1826, `bash_tool.py` 1461) | `00-metrics-baseline.json` | backlog |
| **F-22** | medium | async | `FindStr` walked the filesystem (`os.walk`/`os.listdir`) and scanned files inside `async def __call__` (no `to_thread`) | `src/kimix/tools/file/find_str.py` (X02) | **fixed (FP-12)**: the walk and the per-file scan now go through `anyio.to_thread.run_sync`, pinned structurally by `tests/unit/tools/test_async_offload_gate.py` |
| **F-23** | medium | async | `swarm/best_of_n.py` ran `subprocess`/`shutil.copytree`/`os.walk` on the event loop | `src/kimix/tools/swarm/best_of_n.py` (X02) | **fixed (FP-12)**: `create_worker_workspace`, `_snapshot_files`, `collect_diff`, `cleanup_worker_workspace` and `apply_diff_to_workspace` are all offloaded |
| **F-24** | info | async | temp-folder cleanup retries with a synchronous `time.sleep` between `rmtree` attempts | `src/kimix/tools/common.py` (X02) | **NOT a finding** (corrected): the only callers are the synchronous CLI exit path (`cli_impl/commands.py:419`, in `_cmd_exit`) and `atexit`. No event loop is ever involved, so a blocking retry is correct here |
| **F-25** | high | native parity | the installed `.venv` wheel of `kimix_native` is stale and diverges from `bin/kimix_native` (differing `__init__.py`/`_shell_compat.py`, missing `grep.py`) | `gates/X04.txt` (X04-F1) | FP-10 |
| **F-26** | info | plan drift | plan §1.1 lists `file/_ripgrep_common.py` (absent) and places the `read` unit tests under `kimi-cli/tests/unit/tools/` (that directory does not exist; they are at `tests/unit/tools/`) | `00-inventory.md` | n/a |

## Waived / not a finding

| item | rationale |
|---|---|
| `subagent` implemented in both trees (C01 `AgentTool`, K01 `Agent`) | **intentionally distinct**: `run_in_background` defaults `False` in C01 and `True` in K01; the plan mandates a contrast probe in both gate blocks. Waived in `tools/gate_dup_allowlist.txt`. |
| `kimi_cli.tools.context_prune:context_prune` not in a manifest | registered programmatically at `kimi-cli/src/kimi_cli/soul/kimisoul.py:490`. Allowlisted with that rationale. |
| 101 skipped tests in the CLI suite | pre-existing; counted in `00-baseline.txt`, not introduced by this review. X06 tracks unreasoned skips. |
| `10` vs `9` orphan discrepancy | the raw tool output counts the `ORPHANS:` header line; there are **9** orphan rows plus 1 allowlisted. Corrected in §F-14. |

## Fix-package map

| package | findings | scope |
|---|---|---|
| FP-00 | F-05 | make the root suite collectable without the optional corpus |
| FP-01 | plan-seeded `write.py:214` TODO | resolve or remove the open `TODO: checks:` |
| FP-02 | F-12 | reconcile the duplicate `fetch_url` |
| FP-03 | F-14 | register or delete the 9 orphan tool classes |
| FP-05 | F-09, F-10, F-11 | library-policy compliance |
| FP-06 | F-15 | remove stale `__pycache__` trees |
| FP-07 | F-16, F-17, F-19 | probe suites + complexity reduction |
| FP-08 | F-01, F-02, F-03, F-04 | lint-blocking correctness defects |
| FP-09 | F-18 | `ruff check` clean across both trees |
| FP-10 | F-06, F-07, F-08, F-25 | baseline suite green + native/shim sync |
| FP-11 | F-13 | enable child-env scrubbing at the shell spawn sites |
| FP-12 | F-22, F-23, F-24 | move blocking work off the event loop |

## Additional findings surfaced during Phase 4

| # | sev | axis | finding | evidence | disposition |
|---|---|---|---|---|---|
| **F-27** | low | params | `read_image.region_pct` is validated in `__call__`, not in the pydantic model, so the JSON schema advertised to the model accepts any string; malformed values are only rejected at call time | `kimi-cli/src/kimi_cli/tools/file/read_media.py` (`Params.region_pct: str /\| None`) | **partly fixed** by FP-08 (all malformed values now return a ToolError instead of being silently ignored); moving the check into a model validator is deferred |
| **F-28** | high | correctness | malformed `region_pct` was **silently ignored**: only a 4-part string entered the parser, so `"nonsense"`, `"1,2,3"`, `"1,2,3,4,5"` fell through with `region = None` and the model received the WHOLE image while believing it had cropped | `read_media.py` (pre-FP-08) | **fixed** by FP-08 + probes |
| **F-29** | low | limits | `region_pct` values outside 0–100 are not range-checked (the error text claims they are) | `read_media.py` | open; out-of-range values resolve to out-of-image crops that the downstream crop path rejects |
| **F-30** | medium | harness | `tests/unit/tools/test_prompt_common.py::test_descriptions_unchanged` FAILED: the bash tool's model-facing description did not match the pinned snapshot. Pre-existing drift, never executed before FP-00 because the root suite could not collect | `tests/unit/tools/test_prompt_common.py:177` | **resolved (F-30)**: the test is the spec (its docstring documents the post-Layer-2(b) wire text). `bash_tool.py` now emits the pinned wording - keeping the `glob`/`grep` preference hint and replacing the vague win32-only sentence "ALWAYS use native POSIX syntax, even on Windows" with the precise backslash-conversion and `/dev/null` rules. 10 passed. |
| **F-31** | low | secrets | `write` does not warn when the target path looks like it may hold secrets | `kimi-cli/src/kimi_cli/tools/file/write.py:214` (pre-FP-01 TODO) | **decision recorded**: deliberately not implemented in the write path (it would change tool-visible behaviour); the concern lives in `kimi_cli/utils/export.py::_looks_like_secret_filename` + the `soul/slash.py` export warning. TODO removed (FP-01) |
| **F-32** | low | params | `WriteFile.Params` does not set `extra="forbid"`, so a misspelled parameter is silently dropped | `kimi-cli/src/kimi_cli/tools/file/write.py` | open; now pinned by a probe so a future change is visible |
| **F-33** | medium | gate contract | the plan's verbatim G3 command uses `--cov=<path>` / `--cov=src/kimix/...`, which makes pytest-cov emit `CoverageWarning: module-not-imported` and measure **no row** for the target module. The dotted form (`--cov=kimix.tools.agent`) is the one that works | K01 §G3, K04 §G3 | **corrected** in `tools/review_gates.py` (dotted modules only); the plan text needs fixing |

| **F-38** | info | dead code | `bin/kimix_native/grep.py` (+ the compiled `runtime_py.grep`) is imported by **no** production module after `fe10182b` removed the native grep delegation; only the retired parity suite referenced it | `bin/kimix_native/grep.py`; `grep_local.py` has 0 native symbols | **recorded**: the shim is now dead weight in the wheel. Deleting it is safe but out of the tools-review scope (it is a shipped package member); flagged for the owner |

## Fix-package outcomes

| package | state | evidence |
|---|---|---|
| FP-00 | **done** | root suite: collection ERROR -> 5402 passed / 1 failed / 3 skipped (the 1 failure is F-30, pre-existing and newly surfaced) |
| FP-01 | **done** | TODO removed + explained; 30 new C09 probes |
| FP-02 | **done** | G-DUP green: `fetch_url` waived with a runtime/transport rationale + `tests/test_fetch_url_parity.py` (12 probes) pins both contracts |
| FP-03 | **done** | all 9 orphans resolved: `plus`/`compare`/`panic`/`Mkdir`/`Rm` deleted; `FindStr`/`ParserTool`/`HashRead`/`HashEdit`/`context_prune` allowlisted with written rationales (the initial `agent_worker.json` registration of the first two was reverted on the owner's "no new tool registrations" decision); G-ORPHAN PASS - 27 registered / 5 allowlisted / 0 orphans |
| FP-05 | **done** | G-POLICY green (0 hits / 1 justified waiver) |
| FP-06 | **done** | 24 `__pycache__` / 238 `.pyc` removed (gitignored, so no diff) |
| FP-07 | **partial** | 3 probe suites authored (C06 22, C09 30, C10 28) + K01 20 + K04 72; 31 tools still have no suite |
| FP-08 | **done** | 4 latent defects fixed with probes |
| FP-09 | **open** | `ruff check` still red on 83 of 84 modules |
| FP-10 | **open** | 38 native-grep parity failures + stale venv shim |
| FP-11 | **done** | child-env scrubbing enabled at the shared choke point |
| FP-12 | **open** | blocking IO in `FindStr` / swarm best-of-N / temp cleanup |

## G7 cross-tool gate state at Phase 5

| gate | result |
|---|---|
| G-DUP | **PASS** - `fetch_url` and `subagent` both waived with written rationales; `tests/test_fetch_url_parity.py` pins the fetch_url pair |
| G-ORPHAN | **PASS** - 32 discovered / 29 registered / 3 allowlisted / **0 orphans** |
| G-POLICY | **PASS** - 0 hits, 1 justified waiver |
| G-COMPACT | **PASS** - `tests/test_integration_compaction.py` + `kimi-cli/tests/core/test_kimisoul_context_prune.py` = 12 passed |

## Phase-4 (second pass) findings

| # | sev | axis | finding | evidence | disposition |
|---|---|---|---|---|---|
| **F-34** | medium | correctness | `BackgroundStream.start`'s inner `func` set `v._success = False if result == False else True`. The `== False` is deliberate (a helper returning `None` must count as SUCCESS), but it is an E712 trigger: the mechanical fix `not result` silently flips `None` to failure and broke `tests/unit/tools/test_elapsed_report.py` + `tests/unit/tools/test_taskmanager.py` (9 tests). Restored verbatim with `# noqa: E712` and an explanatory comment | `src/kimix/tools/background/utils.py:238` | **fixed**; the tests are the guard |
| **F-35** | medium | simplicity | 101 `E501` line-too-long violations remain in `kimi-cli/src/kimi_cli/tools` (the kimi-cli ruff config does not ignore E501, unlike the root config). ~117 of the 165 flagged lines across both trees contain string literals / long f-strings, so a mechanical wrap risks changing model-facing text | `uv run ruff check --statistics kimi-cli/src/kimi_cli/tools` | **open, specified**: either wrap deliberately (6 sites are pure code) or have the owner raise `line-length`/add a per-file ignore. Recorded rather than mass-edited because the plan's §11 non-goals forbid reformatting-only changes |
| **F-36** | low | simplicity | 5 tool classes use a lowercase class name (`context_prune`, `retrieve`, `fetch_url` x2, `compact`, `python`) where the rest of the codebase uses `CapWords` + a lowercase alias (`TodoList` / `todo_list`). Renaming would break manifests, so each carries a local `# noqa: N801` | `kimi_cli/tools/context_prune.py:81`, `memory/__init__.py:37`, `web/fetch.py:101`, `kimix/tools/context/__init__.py:53`, `py/__init__.py:96`, `web/fetch_url.py:23` | **recorded**: aligning them with the `TodoList`/`todo_list` convention is a suggested follow-up |
| **F-37** | low | simplicity | 8 module-level imports deliberately follow code (`common.py`, `background/utils.py`, the shell re-export shims). Each now carries `# noqa: E402 -- deliberately a late import` instead of being moved (moving them changes import-time behaviour) | see the `noqa` sites | **fixed (documented)** |

| **F-39** | high | correctness | `ParserTool.__init__(self, session: Any = None)` - `Any` is never an injectable dependency, so the moment the class was registered (FP-03) every session died with `ValueError: Tool dependency not found: Any` (`tests/test_session_mcp_deferred.py`). The parameter was documented as "currently unused" and its `self._session` was never read | `src/kimix/tools/parser/__init__.py:114` (pre-fix) | **fixed**: the unused `__init__` override is removed (the base `CallableTool2.__init__` is used), and `tests/unit/tools/test_tool_registry_instantiation_gate.py` now pins - for all 26 registered tool classes - that the module imports, the class resolves, `get_type_hints(__init__)` succeeds, and no parameter is annotated `Any` |

| **F-40** | low | limits | `WritePlan` opens the plan file in text mode, so on Windows the file gets CRLF newlines; the model then reads back CRLF | `src/kimix/tools/note/__init__.py` | **recorded**: `read_text()` normalises on read, so the visible content is unaffected; pinned by a probe that documents it |
| **F-41** | low | limits | `EditPlan._normalize_line_endings` folds CRLF -> LF but leaves a lone CR alone (the plan's K12 probe 15 says "`//r//n` -> `//n`", which is what it does) | `src/kimix/tools/note/__init__.py` | **recorded as intended**: pinned by a probe |
| **F-42** | low | error contract | `EditPlan`'s no-match `ToolError` says "The old string was not found in the plan file." without naming the target string, unlike the plan's K12 probe 16 which asks the missing target to be named | `src/kimix/tools/note/__init__.py` | **open**: pinned by a probe asserting today's wording so the gap is visible |

| **F-43** | low | simplicity | `EditPlan._find_similar` ended with `if target_line_count == 1 and lines:` re-running the *identical* `process.extractOne(norm_target, lines, ...)` lookup that the function's first block already performed, so the branch could never return anything new — pure dead code that pushed the method to complexity 13 | `src/kimix/tools/note/__init__.py` | **fixed (G1)**: dead branch removed and the window search extracted to `_best_fuzzy_match`; `_find_similar` 13 -> 5, `_read_tail` 15 -> 7 (byte-budget trim + message assembly extracted). `note/__init__.py` max complexity 15 -> 12, so **K10/K11/K12 now pass G1** (ruff + C901 + metrics all green) |

| **F-44** | medium | gate contract | `ruff --select C901` and `tools/review_metrics.py` disagree on nested functions: ruff counts a nested `def`'s branches toward the ENCLOSING function's complexity, `review_metrics` prunes them. `find_str.py` measured `cx_max=7` (metrics) while ruff reported `__call__ is too complex (17 > 12)`. Both are G1 sub-checks, so the stricten one wins | `src/kimix/tools/file/find_str.py` (K16 gate) | **fixed at the source**: the two closures were hoisted to module level (`_find_files`, `_find_in_file`), which is what ruff was really asking for. Recorded here because the plan's G1 runs *both* tools and their conventions must be understood together |

| **F-45** | low | gate contract | `ruff --select C901` does NOT count boolean operators as decision points while `tools/review_metrics.py` does (each `or`/`and` beyond the first counts one). `swarm/__init__.py::_validate` therefore measures 19 in the metrics tool and passes C901 | `src/kimix/tools/swarm/__init__.py` (K13 gate) | **recorded**: both are G1 sub-checks, so the stricter reading (metrics) governs; aligning the two conventions is a follow-up |
| **F-46** | medium | gate contract | `tools/review_gates.py` runs `pytest --cov ... --cov-report=term-missing` but does NOT pass `--cov-fail-under=<FLOOR>`, so `cov=pass` in `gates/<ID>.json` means "the coverage run succeeded", not "the floor was met". The per-tool `floor`/`actual` comparison lives in the reports' §2.2 block, not in the harness | `tools/review_gates.py` | **recorded**: passing a per-tool `--cov-fail-under` is a one-line follow-up; until then G3 must be read from the report, not from `cov=pass` |

| **F-47** | low | simplicity | `AgentSwarmParams._validate` (default branch) ended with `if uses_template and "{{item}}" not in self.prompt_template: raise ...`, which is unreachable: `uses_template` is only truthy when the placeholder IS present. It also duplicated the template/prefix XOR check verbatim in both branches | `src/kimix/tools/swarm/__init__.py` | **fixed (G1)**: the XOR check is now `_require_exactly_one_prompt_source()` and the dead raise is removed; `_validate` 19 -> 4, module max complexity 19 -> 12, so **K13 now passes G1** |

## FP-09 outcome

`uv run ruff check --statistics kimi-cli/src/kimi_cli/tools src/kimix/tools`:
**263 findings -> 101**, and all 101 are `E501` line-too-long (F-35). Every other
rule category is now clean:

```
before:  E501 101  I001 65  F401 11  F841 11  UP* 12  SIM* 12  B* 9  E4xx 14  N* 4  ...
after:   E501 101
```

Automated in this pass: 113 safe fixes (import order, pyupgrade, f-strings,
`SIM114`, unused-import removal) plus 44 unsafe-but-reviewed fixes
(`contextlib.suppress`, ternary collapse, `zip(strict=)`, `B007`); the remainder
was hand-fixed with per-site reasons for the 5 `N801` classes and the 8 `E402`
late imports. The `E712` autofix was REVERTED (F-34) after it was caught by the
test suite.
