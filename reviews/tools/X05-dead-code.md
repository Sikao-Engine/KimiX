# X05 — dead-code

## 0. Method

Frozen commit: `52192b7c3a1baa66c96e0015d2868f3b106fc043` (evidence-file lines 1–6:
`git rev-parse HEAD` plus the `git status --porcelain` header of the next block; the only
untracked entries are this sweep's own gate logs `?? reviews/tools/gates/X01.txt … X06.txt`,
so the worktree is otherwise clean).

Scope: the two tool source trees — `kimi-cli/src/kimi_cli/tools/**` (C-tree) and
`src/kimix/tools/**` (K-tree) — plus the shims that wrap them, the bytecode caches under
`kimi-cli/src` + `src`, and the reference/redirect tables that name these tools.

Evidence base: `reviews/tools/gates/X05.txt` (783 lines) is the **sole** source for this
report — no command was re-run for it, and no source outside the raw outputs in that file was
read. Commands recorded there (each preceded by a `=== <command> ===` marker):

1. `uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools`
   (L9–64): 37 discovered tool classes · 27 registered · **1 allowlisted** · **9 ORPHAN**,
   `G-ORPHAN: FAIL` (L21). Raw rows L26–62.
2. `uv run tools/gate_dup.py` (L66–81): 35 distinct names · 2 duplicated → `G-DUP: FAIL`
   (L68); the only unwaived duplicate is `fetch_url`.
3. Module-orphan reachability `uv run python .kimix_cache/tmp_30108/mod_orphan.py`
   (L83–191): AST import index (absolute + relative `Import`/`ImportFrom`, including
   `from pkg import submodule`) over `kimi-cli/src`, `src`, `kimi-cli/tests`, `tests` for all
   **84** `.py` modules under both tool trees; prints imported?/registered? per module, the
   dead-module set (L171–172) and per-candidate import sites (L174–190).
4. Deprecated-shim importers (L192–237): `kimix.tools.check_fmt` and
   `kimi_cli.tools.file.replace`.
5. `find kimi-cli/src src -name '__pycache__' -type d` (L238–309) plus
   `uv run python .kimix_cache/tmp_30108/pyc_orphans.py` (L311–582) — FP-06.
6. `uv run --with vulture vulture kimi-cli/src/kimi_cli/tools src/kimix/tools
   --min-confidence 90 --sort-by-size` (L583–589).
7. `grep -rn "TODO\|FIXME\|XXX" kimi-cli/src/kimi_cli/tools src/kimix/tools` (L590–616) and a
   strict `\b(TODO|FIXME|XXX)\b` re-grep (L617–619).
8. Orphan-class reference/instantiation grep for all nine classes (L620–780) and the manifest
   reference check (`kimi-cli/src/kimi_cli/agents`, `src/kimix/agent_*.json`, L782–783).

Dimensions checked: (a) registered-orphan inventory (G-ORPHAN); (b) model-facing name
duplication (G-DUP); (c) module reachability = imported anywhere vs. registered anywhere;
(d) deprecated shims and their remaining importers; (e) stale bytecode for deleted sources
(FP-06); (f) ≥90 %-confidence unused symbols (vulture); (g) open TODO/FIXME/XXX markers;
(h) the redirect/taxonomy tables that promise names no manifest registers. Each maps to a
finding in §1 or an explicit row in §2.

Limitations stated up front. (1) Reachability is **static** (AST + path table): a module
reached only through a dynamic `importlib`/string lookup would be reported as un-imported;
none of the nine orphan classes or the one dead module has a string-form consumer in the
grep evidence, but that is a grep, not a runtime probe. (2) “Tested” below means an import or
instantiation site exists under `tests/` or `kimi-cli/tests/`; the pass/fail status of those
tests was **not** collected here (X04/FP-10 owns the red suite). (3) The K16/K17/K18
classifications are recommendations for the plan's *decision gates*; they still require the
orchestrator's recorded sign-off. (4) `__pycache__` contents are build artifacts, so the 193
orphaned `.pyc` count is a hygiene signal, not a source-level defect.

## 1. Findings

| severity | title | file:line | evidence | impact | fix | test_to_add |
|---|---|---|---|---|---|---|
| high | G-ORPHAN red — 9 tool classes exist but no agent manifest registers them (`G-ORPHAN: FAIL`) | `kimi-cli/src/kimi_cli/tools/file/hash_line.py:677`, `:872`; `kimi-cli/src/kimi_cli/tools/test.py:13`, `:28`, `:47`; `src/kimix/tools/file/__init__.py:13`, `:38`; `src/kimix/tools/file/find_str.py:27`; `src/kimix/tools/parser/__init__.py:105` | Registry run: `# discovered tool classes: 37` / `# registered in a manifest: 27` / `# allowlisted (rationale recorded): 1` / `# ORPHANS: 9` / `G-ORPHAN: FAIL` (X05 L21–25). The 9 rows are listed at L26–62; the single allowlisted row is `kimi_cli.tools.context_prune:context_prune` (wired programmatically at `kimi-cli/src/kimi_cli/soul/kimisoul.py:490`, rationale in `tools/tool_registry_allowlist.txt`). Manifest re-check (X05 L782–783) exits 1: none of the nine names appears in `kimi-cli/src/kimi_cli/agents/**/*.yaml` or `src/kimix/agent_*.json`. Class-reference grep (X05 L620–780) shows **zero instantiation sites** for all nine outside their own definitions; only `HashEdit`/`HashRead` have test instantiations (`kimi-cli/tests/conftest.py:361-374`, `test_hash_line.py`, `test_plan25_integration.py`) and `FindStr`/`ParserTool` have test instantiations (`tests/native/test_behavior_equivalence.py:109-124`, `tests/test_parsers.py:539-618`). | Nine model-facing tool classes are unreachable from every shipped agent: a model can never be told to call `HashRead`, `HashEdit`, `Mkdir`, `Rm`, `FindStr` or `ParserTool`, so the implementations (and, for the C-tree, their tests) are maintenance weight with no production path. Four of them (`Mkdir`, `Rm`, `FindStr`, `ParserTool`) are simultaneously named as *canonical* redirect targets in `kimi-cli/src/kosong/tooling/__init__.py:1848-1864` (`MakeDir→Mkdir`, `Delete→Rm`, `FindString→FindStr`, `Parse→ParserTool`, `ReadHash→HashRead`, `EditHash→HashEdit`), so the redirect table promises names the toolset cannot resolve — a hallucinated `MakeDir` redirects to a non-existent `Mkdir`. | Apply the decision list in the table below (4 register / 5 delete); after the decision, re-run `review_tool_registry.py` and require exit 0. Deletions must also drop the matching `kosong/tooling` redirect entries. | For every class kept: an inventory/gate probe; `kimi-cli/tests/tools/test_demo_tools.py` is **not** needed if the three demo tools are deleted. |
| medium | `kimi_cli.tools.test` is a fully dead module — nothing imports it and nothing registers it | `kimi-cli/src/kimi_cli/tools/test.py:1-55` | Reachability scan over 84 modules: it is the **only** row `NO-IMPORT` (X05 L171–190: `== MODULES NOTHING IMPORTS AND NOTHING REGISTERS (1) ==` / `kimi_cli.tools.test (kimi-cli/src/kimi_cli/tools/test.py)` / `kimi_cli.tools.test NO IMPORT SITE`). It defines three `CallableTool2` demos — `Plus` (`:13`), `Compare` (`:28`), `Panic` (`:47`) — the same three G-ORPHAN rows. Class grep (X05 L620–780) finds no instantiation of `Plus`/`Compare`/`Panic` anywhere; the only `Compare` hits are unrelated PowerShell `Compare-Object` strings in `tests/test_process_pwsh.py`. | 55 LOC of demo code with a live `CallableTool2` surface but no importer: a reader (or agent) can re-wire it into a manifest and ship `plus`/`compare`/`panic` as real tools. It is also the sole module that the reachability dimension flags as dead, so leaving it keeps `G-ORPHAN` red for no benefit. | Delete `kimi-cli/src/kimi_cli/tools/test.py` (plan C16 option A) and prove `grep -rn "kimi_cli.tools.test"` is empty outside git history. | n/a (deletion); the C16 “keep” path would require `kimi-cli/tests/tools/test_demo_tools.py` with ≥6 probes. |
| medium | FP-06: 193 orphaned `.pyc` files remain for sources that no longer exist | e.g. `kimi-cli/src/kimi_cli/tools/__pycache__/context_retrieval.cpython-314.pyc`, `kimi-cli/src/kimi_cli/tools/file/edit/modes/__pycache__/hashline.cpython-314.pyc`, `src/kimix/tools/file/bash/__pycache__/cat.cpython-314.pyc`, `src/kimix/utils/__pycache__/rag.cpython-314.pyc` | `find … -name '__pycache__' -type d` → **70** dirs (X05 L312). `.pyc` census: **552** files, of which **193** have no sibling `.py` source (X05 L384, L581 `orphan count: 193`; full list L386–580). The list is dominated by the deleted `src/kimix/tools/file/bash/*` builtin shims (`cat`, `ls`, `xz`, …), plus deleted C-tree modules such as `context_retrieval`, `step_mem`, `dmail`, `file/plan_mode`, `file/edit/diff`, `file/edit/modes/hashline`, `file/edit/modes/patch`, `soul/dynamic_injections/*`. | Stale bytecode for removed modules: it inflates the tree, can mask a green “module exists” import (a `.pyc`-only path), and is exactly the FP-06 hygiene signal the plan calls out. It also confirms the removals that created the orphan classes (`file/edit/modes/hashline` gone, `dmail` gone). | Delete every `__pycache__` under `kimi-cli/src` and `src` (build artifacts; safe), and add a CI hygiene check that fails on a `.pyc` with no sibling `.py`. | `tests/test_no_orphan_pyc.py` — walk both roots, assert no `*.cpython-*.pyc` lacks its source. |
| medium | G-DUP red — `fetch_url` has two divergent implementations (cross-ref FP-02) | `kimi-cli/src/kimi_cli/tools/web/fetch.py:101` (C12) and `src/kimix/tools/web/fetch_url.py:23` (K14) | `gate_dup.py`: `# duplicated names: 2`, `[FAIL] fetch_url (2 implementations)` with both paths, `G-DUP: FAIL` (X05 L68–81). The other duplicate, `subagent`, is waived. | Two implementations of one model-facing tool name; whichever tree the agent loads decides behaviour, with no parity test. Recorded here because a duplicated name is also a dead-code risk (one copy is effectively unreachable in any given runtime). | Reconcile per FP-02: shared core or exact forwarding + a parity test; do **not** waive. | parity/identity test between C12 and K14. |
| low | Deprecated shims are imported only by tests — the production call graph has already migrated | `src/kimix/tools/check_fmt.py:13-22`; `kimi-cli/src/kimi_cli/tools/file/replace.py:1-12` | `kimix.tools.check_fmt`: the only importer is `tests/test_check_fmt.py:6` (`from kimix.tools.check_fmt import check_json, check_json_str`); all production code imports the canonical `kimi_cli.tools.file.check_fmt` (`kimi-cli/src/kimi_cli/tools/file/edit/base.py:19`, `parse_check.py:13`, `write.py:16`) — X05 L192–233. The shim raises `DeprecationWarning` at import (`check_fmt.py:21-22`), so `tests/test_check_fmt.py` is the only live user. `kimi_cli.tools.file.replace` (12-line re-export of `Edit`/`EditFile`/`Params` from `kimi_cli.tools.file.edit`) is imported **only** by test modules: `kimi-cli/tests/conftest.py:41`, `core/test_session_logging.py:284`, `tools/test_additional_dirs.py:20-21`, `test_edit_file*.py`, `test_edit_gate.py:38-40,585`, `test_tool_descriptions.py:15`, `test_work_dir_resolution.py:29`, `tests/conftest.py:199`, `tests/kimix/test_report_sync.py:185`, `tests/test_custom_config.py:12` — X05 L199–237. No production import of either shim. | Two back-compat shims whose only remaining consumers are the test suites: the deprecation warning is now exercised solely by `tests/test_check_fmt.py`, and `replace.py` survives purely as a test-fixture alias. They are load-bearing for the suite, so deleting them is a test-refactor, not a free delete. | Either (a) migrate the remaining test imports to the canonical modules and delete both shims, or (b) keep them and record them as an explicit allowlist with the “next minor release” removal date from `src/kimix/tools/check_fmt.py:20-23`. | update `tests/test_check_fmt.py` / the ten `file.replace` test imports to the canonical paths before deletion. |
| low | Five unused symbols at ≥90 % vulture confidence | `kimi-cli/src/kimi_cli/tools/file/glob.py:242`; `…/file/micro_compress.py:345`; `…/file/read_media.py:118`; `src/kimix/tools/file/bash/pwsh_tool.py:65`; `src/kimix/tools/swarm/__init__.py:267` | `vulture … --min-confidence 90 --sort-by-size` (X05 L583–589, exit 3): `glob.py:242 unused variable 'root_dir'` (100 %), `micro_compress.py:345 unused variable 'all_lines'` (100 %), `read_media.py:118 unused variable 'original_dimensions'` (100 %), `bash/pwsh_tool.py:65 unused import '_CallableTool2'` (90 %), `swarm/__init__.py:267 unused variable 'worker_dir'` (100 %). | Dead locals/imports: harmless at runtime but they hide intent (e.g. `original_dimensions` in the image-resize path suggests an abandoned size check) and clutter the diff surface. | Remove the five symbols (or, for `read_media.py:118`, wire the intended size check if it was dropped in error). | n/a — covered by FP-09 ruff/vulture cleanliness; `bash/pwsh_tool.py:65` overlaps the F-04 unused-import cluster. |
| low | One real TODO marker; the rest of the `TODO` grep is identifier noise | `kimi-cli/src/kimi_cli/tools/file/write.py:214` | `grep -rn "TODO\|FIXME\|XXX"` (X05 L590–616) matches 100+ `_TODO_LIST`/`_TODOLIST`/`MAX_TODOS` identifiers; the strict `\b(TODO\|FIXME\|XXX)\b` re-grep returns **exactly one** hit: `kimi-cli/src/kimi_cli/tools/file/write.py:214: # TODO: checks:` (X05 L617–619). | A single unresolved `# TODO: checks:` on the write path (plan-seeded FP-01) — comment only, no behaviour. | Resolve or remove the TODO and record the decision in FP-01. | n/a (comment). |
| info | Redirect/taxonomy tables name tools that no manifest registers (dangling canonical targets) | `kimi-cli/src/kosong/tooling/__init__.py:1848-1876`; `kimi-cli/src/kimi_cli/soul/tool_taxonomy.py:13-24` | Redirect map marks `Mkdir`, `Rm`, `ParserTool`, `FindStr`, `HashRead`, `HashEdit` as “canonical name — no redirect needed” and forwards hallucinated aliases to them (`MakeDir→Mkdir`, `Delete→Rm`, `Parse→ParserTool`, `FindString→FindStr`, `ReadHash→HashRead`, `EditHash→HashEdit`, X05 L620–780 corroborated by `93-orphans-and-removal.md` §1). `tool_taxonomy.py:15` puts `HashEdit` in `EDIT_TOOLS`. None of these six names is registered (X05 L782–783). | The tables encode an intent that these tools exist, contradicting G-ORPHAN. Either the tables are aspirational (register) or stale (delete the entries with their classes). | Make the decision list below authoritative and prune the redirect/taxonomy rows of any tool that is deleted. | n/a. |

### 1.1 Orphan decision list (feeds Phase 3 / FP-03)

All nine G-ORPHAN rows, one decision each. “Tested?” = an instantiation/import exists under
`kimi-cli/tests` or `tests` (X05 L620–780); “canonical?” = named in the redirect map or
`tool_taxonomy.py`.

| # | orphan (`module:Class`) | file:line | tested? | canonical? | **decision** | rationale |
|---|---|---|---|---|---|---|
| 1 | `kimi_cli.tools.file.hash_line:HashRead` | `hash_line.py:677` | yes (`conftest.py:361-374`, `test_hash_line.py`, `test_plan25_integration.py`, `native/test_diff_glob_tools_equivalence.py:201-225`) | yes (`ReadHash→HashRead`, `:1871-1874`) | **register** | Complete, exported (`file/__init__.py:18,32 __all__`), heavily tested; the only defect is the missing manifest entry. Add to a C-agent/coder manifest (or record it as an intentional opt-in). |
| 2 | `kimi_cli.tools.file.hash_line:HashEdit` | `hash_line.py:872` | yes (`conftest.py:372-374`, `test_hash_line.py:758+`, `test_plan25_integration.py:114+`) | yes (`EDIT_TOOLS` has `"HashEdit"`; `EditHash→HashEdit`) | **register** | Same as HashRead; `tool_taxonomy.py:15` already treats it as an edit tool, so leaving it unregistered contradicts the churn/verification gate. |
| 3 | `kimi_cli.tools.test:Plus` | `test.py:13` | no | no | **delete** | Demo tool; module has zero importers (dead module, finding 2); plan C16 option A. |
| 4 | `kimi_cli.tools.test:Compare` | `test.py:28` | no | no | **delete** | Same as Plus (C16). |
| 5 | `kimi_cli.tools.test:Panic` | `test.py:47` | no | no | **delete** | Same as Plus (C16). |
| 6 | `kimix.tools.file:Mkdir` | `src/kimix/tools/file/__init__.py:13` | no (K17 test list empty) | yes (`MakeDir→Mkdir`) | **delete** | Untested and redundant with `bash`/`Run`; plan K17 allows delete. Deleting also removes the dangling `MakeDir`/`CreateDir` redirects. *If the owner prefers to expose it, the register path requires the K17 ≥10-probe gate.* |
| 7 | `kimix.tools.file:Rm` | `src/kimix/tools/file/__init__.py:38` | no (K18 test list empty) | yes (`Delete/Unlink→Rm`) | **delete** | Same as Mkdir (K18). |
| 8 | `kimix.tools.file.find_str:FindStr` | `src/kimix/tools/file/find_str.py:27` | yes (`tests/native/test_behavior_equivalence.py:109-124`, end-to-end both modes) | yes (`FindString→FindStr`) | **register** | Already has an end-to-end native-equivalence test and a canonical redirect; K16 decision gate → keep and add the K16 probe suite (and fix F-22: synchronous walk inside `async def __call__`). |
| 9 | `kimix.tools.parser:ParserTool` | `src/kimix/tools/parser/__init__.py:105` | yes (`tests/test_parsers.py::TestParserTool:529-618`) | yes (`Parse→ParserTool`) | **register** | K15 is a normal gate (not a decision gate), the tool is tested; the orphan is a missing-manifest defect → register and add the K15 gate suite. |
| — | `kimi_cli.tools.context_prune:context_prune` | `context_prune.py` | (programmatic) | — | **allowlist** (already) | Registered programmatically at `kimi-cli/src/kimi_cli/soul/kimisoul.py:490` (`agent.toolset.add(context_prune(self))`); rationale recorded in `tools/tool_registry_allowlist.txt`. This is the one non-orphan allowlisted row from the registry output. |

Summary: **4 register · 5 delete · 1 allowlist**. Deletions: finding 2 (`test.py`) + K17/K18
`Mkdir`/`Rm`. This differs from `93-orphans-and-removal.md`, which left `Mkdir`/`Rm`/`FindStr`/
`ParserTool` as “delete or register”; the single-decision list above resolves those gates using
the tested?/canonical? evidence.

## 2. Waivers / non-findings

| item | why it is not a finding |
|---|---|
| `kimi_cli.tools.context_prune:context_prune` | Programmatically registered at `kimi-cli/src/kimi_cli/soul/kimisoul.py:490`; allowlisted with rationale (X05 L20, L26–62). Not an orphan. |
| `subagent` duplicated across trees | Intentionally distinct (C01 `AgentTool` fg-default vs K01 `Agent` bg-default) and contrast-probe-mandated; waived in `tools/gate_dup_allowlist.txt` (X05 L76–81). |
| 11 C-tree helper modules that “look” unreferenced | All are imported by production code, not only tests: `micro_compress` (`soul/context_pruning.py:14`, `soul/toolset.py:349,1611`, `file/glob.py:20`, `file/grep_local.py:69`), `blackbox` (`file/edit_safety.py:12`), `parse_check` (`file/auto_repair.py:14`, `file/edit_safety.py:17`), `output_utils` (`file/glob.py:24`, `file/grep_local.py:73`, `src/kimix/tools/common.py:1739`), `grep_recorder` (`file/grep_local.py:60`), `grep_selectors` (`file/grep_archive.py:19`, `file/grep_local.py:61`), `reason` (`src/kimix/utils/system_prompt.py:10`), `display` (`file/auto_repair.py:13`, `file/edit/base.py:17`, `file/edit/modes/replace.py:15`, `file/write.py:13`, `todo/__init__.py:38`, `utils/diff.py:12`), `prompt_common` (6 K-tree call sites, `agent/__init__.py:21` …). None is a module-orphan (X05 L174–190). |
| Modules imported **only** by tests (reachable but production-dead) | `kimix.tools.check_fmt` (only `tests/test_check_fmt.py:6`), `kimi_cli.tools.file.replace` (only test modules), `kimix.tools.file.find_str` (only `tests/native/test_behavior_equivalence.py`), `kimix.tools.parser` (only `tests/test_parsers.py`). Recorded under findings 5 and 1.1; they are not “nothing imports” rows because the test trees count as importers. |
| `kimi_cli/src/kimi_cli/tools/file/check_fmt.py` | The **canonical** module (imported by `edit/base.py:19`, `parse_check.py:13`, `write.py:16`, and by the `kimix.tools.check_fmt` shim). It is the *source*, not a shim. |
| `kimix.tools.file` package `__init__.py` | Reported `imported: YES` only because importing any submodule (`kimix.tools.file.bash.…`) imports the parent package; the two classes it defines (`Mkdir`/`Rm`) are themselves unused (finding 1). |
| `TODO` grep noise | `_TODO_LIST`, `_TODOLIST`, `_MAX_TODOS`, `RETIRED_TODO_TOOL_NAMES`, etc. in `todo/__init__.py` and `tools/__init__.py` are identifiers, not markers (X05 L590–616). The strict word-boundary grep leaves one hit (finding 7). |
| 70 `__pycache__` directories | Build artifacts; their mere presence is not a finding. Only the 193 `.pyc` files whose source is gone are (finding 3, FP-06). |
| vulture findings <90 % confidence | The sweep used `--min-confidence 90`; lower-confidence entries are intentionally out of scope here. |

## 3. Gate result

| gate | command | rc | result |
|---|---|---|---|
| G-ORPHAN | `uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools` | 1 | **FAIL** — 9 orphans, 1 allowlisted, 27/37 registered |
| G-DUP | `uv run tools/gate_dup.py` | 1 | **FAIL** — `fetch_url` unwaived (2 impls); `subagent` waived |
| reachability | `mod_orphan.py` (84 modules) | 0 | 1 dead module (`kimi_cli.tools.test`); 4 test-only importers |
| FP-06 | `pyc_orphans.py` | 0 | 70 `__pycache__` dirs · 552 `.pyc` · **193 orphaned** |
| vulture | `vulture … --min-confidence 90` | 3 | 5 unused symbols (findings 6) |
| markers | strict `\b(TODO\|FIXME\|XXX)\b` | 0 | 1 real marker (`file/write.py:214`) |

The two mechanical gates (G-ORPHAN, G-DUP) stay red until the decision list in §1.1 and FP-02
are executed. **Outstanding decision list (Phase 3 input / FP-03):**
register `HashRead`, `HashEdit`, `FindStr`, `ParserTool`; delete `Plus`, `Compare`, `Panic`,
`Mkdir`, `Rm`; keep the existing allowlist entry for `context_prune`. K16/K17/K18 are the
plan's *decision gates* and require the orchestrator's recorded sign-off (recommendations
above).

## 4. JSON summary

```json
{
  "id": "X05",
  "title": "dead-code",
  "frozen_commit": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "evidence": "reviews/tools/gates/X05.txt",
  "scope": {
    "trees": ["kimi-cli/src/kimi_cli/tools", "src/kimix/tools"],
    "modules_scanned": 84
  },
  "gates": {
    "G-ORPHAN": {"rc": 1, "result": "FAIL", "discovered": 37, "registered": 27, "allowlisted": 1, "orphans": 9},
    "G-DUP": {"rc": 1, "result": "FAIL", "distinct_names": 35, "duplicated": 2, "unwaived": ["fetch_url"], "waived": ["subagent"]}
  },
  "allowlisted": [
    {"module": "kimi_cli.tools.context_prune", "class": "context_prune",
     "rationale": "programmatic registration kimi-cli/src/kimi_cli/soul/kimisoul.py:490; tools/tool_registry_allowlist.txt"}
  ],
  "orphans": [
    {"module": "kimi_cli.tools.file.hash_line", "class": "HashRead",  "file": "kimi-cli/src/kimi_cli/tools/file/hash_line.py", "line": 677, "tested": true,  "canonical": true,  "decision": "register"},
    {"module": "kimi_cli.tools.file.hash_line", "class": "HashEdit",  "file": "kimi-cli/src/kimi_cli/tools/file/hash_line.py", "line": 872, "tested": true,  "canonical": true,  "decision": "register"},
    {"module": "kimi_cli.tools.test",         "class": "Plus",     "file": "kimi-cli/src/kimi_cli/tools/test.py",          "line": 13,  "tested": false, "canonical": false, "decision": "delete"},
    {"module": "kimi_cli.tools.test",         "class": "Compare",  "file": "kimi-cli/src/kimi_cli/tools/test.py",          "line": 28,  "tested": false, "canonical": false, "decision": "delete"},
    {"module": "kimi_cli.tools.test",         "class": "Panic",    "file": "kimi-cli/src/kimi_cli/tools/test.py",          "line": 47,  "tested": false, "canonical": false, "decision": "delete"},
    {"module": "kimix.tools.file",            "class": "Mkdir",    "file": "src/kimix/tools/file/__init__.py",             "line": 13,  "tested": false, "canonical": true,  "decision": "delete"},
    {"module": "kimix.tools.file",            "class": "Rm",       "file": "src/kimix/tools/file/__init__.py",             "line": 38,  "tested": false, "canonical": true,  "decision": "delete"},
    {"module": "kimix.tools.file.find_str",   "class": "FindStr",  "file": "src/kimix/tools/file/find_str.py",             "line": 27,  "tested": true,  "canonical": true,  "decision": "register"},
    {"module": "kimix.tools.parser",          "class": "ParserTool","file": "src/kimix/tools/parser/__init__.py",          "line": 105, "tested": true,  "canonical": true,  "decision": "register"}
  ],
  "decision_summary": {"register": 4, "delete": 5, "allowlist": 1},
  "module_orphans": {
    "nothing_imports_and_nothing_registers": [
      {"module": "kimi_cli.tools.test", "file": "kimi-cli/src/kimi_cli/tools/test.py"}
    ],
    "imported_only_by_tests": [
      "kimix.tools.check_fmt",
      "kimi_cli.tools.file.replace",
      "kimix.tools.file.find_str",
      "kimix.tools.parser"
    ]
  },
  "deprecated_shims": [
    {"module": "kimix.tools.check_fmt", "file": "src/kimix/tools/check_fmt.py", "kind": "re-export + DeprecationWarning (lines 13-22)",
     "importers": ["tests/test_check_fmt.py:6"], "production_importers": []},
    {"module": "kimi_cli.tools.file.replace", "file": "kimi-cli/src/kimi_cli/tools/file/replace.py", "kind": "re-export (Edit/EditFile/Params)",
     "importers": ["kimi-cli/tests/conftest.py:41", "kimi-cli/tests/core/test_session_logging.py:284",
                   "kimi-cli/tests/tools/test_additional_dirs.py:20-21", "kimi-cli/tests/tools/test_edit_file.py:14",
                   "kimi-cli/tests/tools/test_edit_file_fuzzy.py:5", "kimi-cli/tests/tools/test_edit_file_parse_repair.py:11",
                   "kimi-cli/tests/tools/test_edit_gate.py:38-40,585", "kimi-cli/tests/tools/test_tool_descriptions.py:15",
                   "kimi-cli/tests/tools/test_work_dir_resolution.py:29", "tests/conftest.py:199",
                   "tests/kimix/test_report_sync.py:185", "tests/test_custom_config.py:12"],
     "production_importers": []}
  ],
  "pyc_orphans": {"pycache_dirs": 70, "pyc_total": 552, "orphaned_pyc": 193,
                  "examples": ["kimi-cli/src/kimi_cli/tools/__pycache__/context_retrieval.cpython-314.pyc",
                               "kimi-cli/src/kimi_cli/tools/file/edit/modes/__pycache__/hashline.cpython-314.pyc",
                               "src/kimix/tools/file/bash/__pycache__/cat.cpython-314.pyc",
                               "src/kimix/utils/__pycache__/rag.cpython-314.pyc"]},
  "vulture": {
    "rc": 3, "min_confidence": 90,
    "findings": [
      {"file": "kimi-cli/src/kimi_cli/tools/file/glob.py", "line": 242, "symbol": "root_dir", "kind": "unused variable", "confidence": 100},
      {"file": "kimi-cli/src/kimi_cli/tools/file/micro_compress.py", "line": 345, "symbol": "all_lines", "kind": "unused variable", "confidence": 100},
      {"file": "kimi-cli/src/kimi_cli/tools/file/read_media.py", "line": 118, "symbol": "original_dimensions", "kind": "unused variable", "confidence": 100},
      {"file": "src/kimix/tools/file/bash/pwsh_tool.py", "line": 65, "symbol": "_CallableTool2", "kind": "unused import", "confidence": 90},
      {"file": "src/kimix/tools/swarm/__init__.py", "line": 267, "symbol": "worker_dir", "kind": "unused variable", "confidence": 100}
    ]
  },
  "todo_markers": [{"file": "kimi-cli/src/kimi_cli/tools/file/write.py", "line": 214, "text": "# TODO: checks:"}],
  "findings": [
    {"id": "X05-F1", "sev": "high",   "title": "G-ORPHAN: 9 unregistered tool classes (4 register / 5 delete)"},
    {"id": "X05-F2", "sev": "medium", "title": "kimi_cli.tools.test is a dead module (zero importers, zero registrars) + 3 demo tools"},
    {"id": "X05-F3", "sev": "medium", "title": "FP-06: 193 orphaned .pyc / 70 __pycache__ dirs"},
    {"id": "X05-F4", "sev": "medium", "title": "G-DUP: fetch_url duplicated (FP-02)"},
    {"id": "X05-F5", "sev": "low",    "title": "deprecated shims kimix.tools.check_fmt & kimi_cli.tools.file.replace used only by tests"},
    {"id": "X05-F6", "sev": "low",    "title": "5 unused symbols at >=90% vulture confidence"},
    {"id": "X05-F7", "sev": "low",    "title": "one real TODO marker: file/write.py:214 (FP-01)"},
    {"id": "X05-F8", "sev": "info",   "title": "dangling canonical redirects/taxonomy rows for unregistered tools"}
  ],
  "fix_packages": {
    "FP-03": "register/delete the 9 orphans per decision_summary",
    "FP-06": "delete stale __pycache__ under kimi-cli/src + src",
    "FP-01": "resolve file/write.py:214 TODO",
    "FP-02": "reconcile duplicate fetch_url (cross-ref)",
    "FP-09": "vulture/ruff cleanliness (glob.py, micro_compress.py, read_media.py, pwsh_tool.py, swarm/__init__.py)"
  },
  "gate_result": "FAIL",
  "decision_list_outstanding": true
}
```
