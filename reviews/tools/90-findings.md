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
| **F-06** | high | harness | 38 pre-existing failures in `kimi-cli/tests/native/test_grep_engine_parity.py` after "remove native module grep"; the suite still expects the removed engine | `reviews/tools/00-baseline.txt:2357-2392` | FP-10 |
| **F-07** | medium | harness | 2 failures: base-agent manifests no longer list the single merged todo tool | `kimi-cli/tests/core/test_toolset_todo_redirects.py` | FP-10 |
| **F-08** | medium | harness | 2 failures in relative-path-outside-work-dir assertions | `kimi-cli/tests/tools/test_read_file.py::test_read_with_relative_path_outside_work_dir` | FP-10 |
| **F-09** | medium | library policy | `import json` at module line 1 (only used for a `JSONDecodeError` except arm) | `kimi-cli/src/kimi_cli/tools/__init__.py:1` | FP-05 |
| **F-10** | medium | library policy | `import re` for two compile patterns | `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:5` | FP-05 |
| **F-11** | medium | library policy | `import difflib` for `unified_diff`; also a dead `import xxhash` | `src/kimix/tools/swarm/best_of_n.py:23` (+ `:147`) | FP-05 |
| **F-12** | high | duplication | `fetch_url` implemented twice (C12 `kimi_cli.tools.web.fetch` vs K14 `kimix.tools.web.fetch_url`) with divergent behaviour | `tools/gate_dup.py` → 2 implementations | FP-02 |
| **F-13** | high | security | child processes spawned by `bash`/`pwsh`/`Run`/`python` inherit the **full parent environment**: `ProcessTask.scrub_env` defaults to `False` and none of the 8 shell spawn sites enables it, so `scrub_child_env` never runs | `src/kimix/tools/common.py:1910,1966-1968`; `bash_tool.py:825,882,1217`; `pwsh_tool.py:535,595,914`; `run.py:424` (X03-F1) | FP-11 |
| **F-14** | medium | policy/dead code | 9 tool classes are not reachable from any manifest: `HashRead`, `HashEdit`, `plus`, `compare`, `panic`, `Mkdir`, `Rm`, `FindStr`, `ParserTool` | `reviews/tools/00-registry.txt` | FP-03 |
| **F-15** | medium | dead code | stale `__pycache__` trees hold `.pyc` for modules deleted from the source tree (e.g. `src/kimix/tools/eval/`) | `gates/X05.txt` | FP-06 |
| **F-16** | high | coverage | **no G4 probe suite exists for any of the 36 tools**; every `G4_behavior` gate is red | `gates/<ID>.json` → `probes_present: 0` | FP-07 |
| **F-17** | high | coverage | `Mkdir` / `Rm` (K17/K18) have **no tests at all** | `tools/gen_inventory.py` → K17/K18 test list empty | FP-07 |
| **F-18** | medium | simplicity | every one of the 84 in-scope modules fails `uv run ruff check` (I001 65, E501 101, UP* 12, SIM* 12, F841 11, F401 11, B* 9, N* 4, E741 4, E402 9, …) | `gates/<ID>.txt` §ruff; `ruff check --statistics` | FP-09 |
| **F-19** | medium | simplicity | 90 functions exceed cyclomatic complexity 12; worst `run.py::__call__` = 92, `grep_local.py::_postprocess` = 77, `write.py::__call__` = 64 | `00-metrics-baseline.json` | FP-07 / backlog |
| **F-20** | low | simplicity | 29 functions exceed 120 LOC; worst `pwsh_tool.py::__call__` = 370 | `00-metrics-baseline.json` | backlog |
| **F-21** | info | simplicity | 6 modules ≥ 1200 LOC require a mandatory keep/split decision (`todo/__init__.py` 2452, `grep_local.py` 2355, `common.py` 2342, `providers.py` 2077, `read.py` 1826, `bash_tool.py` 1461) | `00-metrics-baseline.json` | backlog |
| **F-22** | medium | async | `FindStr` walks the filesystem synchronously inside `async def __call__` (no `to_thread`) | `src/kimix/tools/file/find_str.py:32,78,87,98` (X02) | FP-12 |
| **F-23** | medium | async | `swarm/best_of_n.py` runs `subprocess.run`/`shutil.copytree` on the event loop | `src/kimix/tools/swarm/best_of_n.py:87,114,124,132,168,177,225,233,250` (X02) | FP-12 |
| **F-24** | low | async | temp-folder cleanup retries with a synchronous `time.sleep` between `rmtree` attempts | `src/kimix/tools/common.py:570,577` (X02) | FP-12 |
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
