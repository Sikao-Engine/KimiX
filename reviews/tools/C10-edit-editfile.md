# C10 — edit (EditFile) review

## 0. Gate summary

| Gate | Result | Evidence |
|---|---|---|
| G0 scope | pass | `reviews/tools/gates/C10.txt` §G0; every in-scope file cited in §1/§2; registration = `registered` in `reviews/tools/00-registry.txt:21` |
| G1 simplicity | **fail** | `ruff` 33 errors (`[exit=1]`), `C901` 2 errors (`[exit=1]`), `review_metrics --max-fn-loc 120 --max-complexity 12` `[exit=1]` (`longest_fn_loc=239`, `complexity_max=38`), vulture 0 hits `[exit=0]` |
| G2 risk | **fail** | 13/13 items answered, `open_high = 2`, `open_medium = 5` |
| G3 coverage | **fail** | module `kimi_cli.tools.file.edit` (+`modes.replace`, `modes.sloppy`), floor 85, actual **79.29%**, waived_lines 136, waiver_budget 25 (`Total coverage: 79.29%` → `FAIL Required test coverage of 85% not reached`, `[exit=1]`) |
| G4 behavior | pass | `kimi-cli/tests/tools/test_edit_gate.py`, required 12, present **28**, `28 passed` `[exit=0]` |
| G5 regression | **fail (root suite blocked, pre-existing)** | root `uv run pytest tests -q` → collection `ERROR … tests/bash/cases.json` `[exit=2]`; CLI suite `43 failed, 5179 passed` — FAILED set **identical** to `reviews/tools/00-baseline.txt` (43) ⇒ no new failures; `syntax_check` `[exit=0]`; `git_diff` `[exit=0]` |
| G6 independent rerun | pending orchestrator | numbers recorded below; see §7 note on the harness `cov_rc=0` field |

**Verdict: major** — the tool works for its mainstream paths (all 104 in-scope tests + 28 new probes pass) but the gates fail on three independent axes: a 239-LOC/38-complexity `execute` (hard metric breach), 79.29% branch coverage against an 85 floor (≈110 of the 136 missing lines are dead code in `base.py`), and two high-severity correctness defects in sloppy mode.

Verbatim gate block (plan §4.4 C10) — every line is satisfied or reported as FAIL in §0/§3/§5/§6:

```
* G1: replace.py ≈ 550 LOC, sloppy.py ≈ 350 LOC; max_fn_loc ≤ 120.
* G3: uv run --with pytest-cov pytest kimi-cli/tests/tools/test_edit_file.py kimi-cli/tests/tools/test_edit_file_fuzzy.py kimi-cli/tests/tools/test_edit_file_parse_repair.py kimi-cli/tests/tools/test_edit_conflict.py kimi-cli/tests/tools/test_edit_conflict_plan24.py kimi-cli/tests/tools/test_edit_modes_detect.py kimi-cli/tests/tools/test_edit_safety.py kimi-cli/tests/tools/test_edit_sloppy.py --cov=kimi_cli.tools.file.edit --cov=kimi_cli.tools.file.edit.modes.replace --cov=kimi_cli.tools.file.edit.modes.sloppy --cov-branch --cov-report=term-missing --cov-fail-under=<FLOOR:85> -q
* G4 probes (kimi-cli/tests/tools/test_edit_gate.py, min 12): 1..12 (as enumerated in the plan)
```

## 1. Scope

Source read end-to-end (6 files, 1419 LOC):

| file | LOC | role |
|---|---|---|
| `kimi-cli/src/kimi_cli/tools/file/edit/__init__.py` | 107 | `EditFile` dispatcher + legacy delegation shims |
| `kimi-cli/src/kimi_cli/tools/file/edit/base.py` | 218 | `BaseEditTool` shared flow (path/VFS/approval/format/conflict) |
| `kimi-cli/src/kimi_cli/tools/file/edit/params.py` | 194 | pydantic models + mode detection |
| `kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py` | 525 | literal-replace executor |
| `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py` | 363 | `§`/`⟪old│new⟫` sparse-edit executor |
| `kimi-cli/src/kimi_cli/tools/file/replace.py` | 12 | backward-compat shim |

Adjacent (read, not in scope, cited only as evidence): `edit/modes/__init__.py` (10), `edit_safety.py` (125), `file/utils.py::check_path_protected`, `file/check_fmt.py`, `file/auto_generated.py`, `file/snapshot_store.py`, `file/fs_cache.py`, `file_mtine.py`, `kosong/tooling/{__init__,simple,error}.py`.

Tests in scope (8 files, 1703 LOC): `test_edit_file.py` (660), `test_edit_file_fuzzy.py` (527), `test_edit_file_parse_repair.py` (110), `test_edit_conflict.py` (71), `test_edit_conflict_plan24.py` (139), `test_edit_modes_detect.py` (56), `test_edit_safety.py` (55), `test_edit_sloppy.py` (85) — all under `kimi-cli/tests/tools/`.

Registration: **registered** — `reviews/tools/00-registry.txt:21` (`kimi_cli.tools.file.edit:EditFile name='edit'`), exported through `kimi-cli/tools/file/__init__.py:21,30`, constructed by `kimi-cli/tools/__init__.py:131`.

## 2. Behaviour map

| # | entry point | branches / early returns / except | notes |
|---|---|---|---|
| 1 | `EditFile.__call__` `edit/__init__.py:45-56` | `resolved_mode is None` repair (46-48); registry miss → `ToolError("Invalid edit mode")` (50-55); dispatch (56) | 48 reachable only for hand-built params; the registry-miss arm is unreachable unless `MODE_REGISTRY` is mutated (§3 S1-4) |
| 2 | `EditFile.__init__` `:25-27` | — | `BaseEditTool(runtime, approval, session, vfs)` |
| 3 | `EditFile` properties `:29-43` | — | 4 pass-through properties, never read by the executors |
| 4 | legacy shims `:63-98` | — | 8 test-only delegations; each builds a fresh `ReplaceModeExecutor()` |
| 5 | `MODE_REGISTRY` `modes/__init__.py:12-15` | — | `replace`, `sloppy` (covers the `EditMode` literal — `test_edit_modes_detect.py::test_mode_registry_covers_every_edit_mode`) |
| 6 | `BaseEditTool._validate_path` `base.py:51-84` | canonicalise (58-59); outside+relative → `ToolError("Invalid path")` (62-73); protected match → `ToolError("Protected path")` (76-83) | returns `(err, inside)` |
| 7 | `_resolve_for_write` `:86-89` | — | VFS-aware; **used by replace only** (see §4 (e)) |
| 8 | `_check_format` `:108-121` | `.json`/`.yaml|.yml`/`.toml`/`.xml` (111-120) | `test_edit_file.py::test_replace_invalid_json/xml/valid_*` |
| 9 | `_try_repair_json` `:123-130` | success (126-127); `except Exception` (128-129) | |
| 10 | `_request_approval` `:132-156` | justification (146-147); reject → `rejection_error()` (154-155); else `None` (156) | **dead** — 0 callers (§3 S1-2) |
| 11 | `_check_conflicts` `:158-183` | `allow_conflicts` short-circuit (166-167); marker scan (168-175) | 4 in-scope tests + probes 06/13d |
| 12 | `_check_staleness` `:185-195` | — | **dead** |
| 13 | `_stat_is_regular_file` `:197-203` | `FileNotFoundError` (202-203) | **dead** |
| 14 | `_wrap_exception` `:205-218` | `contextlib.suppress` (210-214) | **dead** |
| 15 | `_remove_file` / `_ensure_parent` `:100-106` | `parent` missing (104-106) | **dead** |
| 16 | `ReplaceEditItem` `params.py:13-40` | `aliases old/new` (18-25), `replace_all` (26-29), `max_replacements ge=1` (30-35), `match_mode` (36-40) | probes 03/12 |
| 17 | `EditParams` `:43-106` | mode/file_path(`AliasChoices`)/edit|edits/old_string/new_string/input/sandbox_permissions(84-87,**unread**)/justification/allow_conflicts/allow_auto_generated/resolved_mode(`exclude=True`) | probe 12/18 |
| 18 | `_normalize_single_edit` `:108-126` | shorthand present (115-125); dict but already `edit` (114) | probe 04 |
| 19 | `_normalize_edit` `:128-138` | dict (132-133), list (134-135), `ReplaceEditItem` (136-137) | probes 04/12 |
| 20 | `_coerce_edit` `:140-146` | non-dict/non-model → `ValueError` (146) | `test_edit_file.py::test_call_with_string_edit_is_repaired_and_succeeds` |
| 21 | `_resolve_mode` `:148-159` | explicit branch (152-156) / auto (157-158) | |
| 22 | `normalize_edit_mode` `:162-171` | falsy (164-165); mapping hit/miss (167-171) | case/space tolerance unreachable via the `Literal` field (§3 S1-6) |
| 23 | `detect_mode` `:174-193` | `§`-prefixed input (176-184); shorthand (186-187); edits (188-189); else `ValueError` (190-193) | probes 01/02 |
| 24 | `ReplaceModeExecutor._apply_edit` `replace.py:201-227` | empty/same old (206-207); `replace_all` (213-216); exact (218-220); `match_mode=="exact"` (222-223); fuzzy fallback (225-227) | probes 03/04/05/09 |
| 25 | `_apply_replace_all` `:145-170` | `max_replacements` loop (154-165); unlimited count/replace (167-170) | probe 03 |
| 26 | `_apply_fuzzy_fallback` `:172-199` | strip (181-183); fuzzy + no-op guard (185-197); suggestion (199) | `test_edit_file_fuzzy.py::test_apply_fuzzy_fallback_*`, probe 05 |
| 27 | `_try_strip_match` `:87-108` | empty old (90-91); CRLF/LF/CR endings (99-105) | line 104-105 uncovered (§5) |
| 28 | `_find_similar` / `_find_best_fuzzy_match` `:58-143` | cutoff 75.0 (defaults), CRLF preservation | probe 05 (boundary 100.0/100.1) |
| 29 | `_try_snapshot_recovery` `:229-285` | store failure (246-247); drifted head (251-256); unmatched edit aborts (265); no-op replay (273-280); line-ending re-apply (282-285) | mostly uncovered (§5) |
| 30 | `ReplaceModeExecutor.execute` `:287-525` | 37 branch points — see §3 S1-1 | the whole mainstream flow is covered |
| 31 | `SloppyModeExecutor.execute` `sloppy.py:247-362` | 22 branch points; atomic preflight (256-279), plan (281-298), write loop (300-356) | probes 13a-13e |
| 32 | sloppy parsers `:54-238` | `_split_sections`, `_parse_op`, `parse_sloppy_input`, `_find_exact_block`, `_find_fuzzy_block`, `_apply_block_op`, `_apply_inline_op` | probes 13b-13c |
| 33 | shim `file/replace.py:9-12` | — | probe 11 |

## 3. S1 Simplicity (G1 detail)

**verdict: major (G1 FAIL)** — `ruff [exit=1]`, `C901 [exit=1]`, metrics `[exit=1]`.

| finding | file:line | evidence | change | waiver? |
|---|---|---|---|---|
| S1-1 `execute` 239 LOC / 38 complexity (hard breach > 200 / > 15) | `edit/modes/replace.py:287` | `review_metrics`: `longest_fn_loc=239`, `complexity_max=38`, `execute loc=239 complexity=38 branches=37 nesting=4`; `ruff C901 execute is too complex (33 > 12)` | split into `_preflight`, `_plan`, `_commit` (mirroring sloppy's structure) | none — automatic FAIL per §3 thresholds |
| S1-2 ~110 LOC (50%) of `base.py` is dead | `base.py:100-101,103-106,132-156,185-195,197-203,205-218` | repo-wide `grep --include=*.py` for `_remove_file`, `_ensure_parent`, `_request_approval`, `_check_staleness`, `_stat_is_regular_file`, `_wrap_exception` → **0 non-definition usages** each | delete the six helpers | none; also the dominant cause of the G3 miss (see §5) |
| S1-3 sloppy re-implements base helpers | `sloppy.py:312-334` vs `base.py:132-156,158-183` | inline `_approval.request` + inline marker scan (55 lines) duplicating `_request_approval`/`_check_conflicts`, with a different refusal message ("line {i}" vs the full offending-line list) | call the base helpers | — |
| S1-4 unreachable dispatcher error arm | `edit/__init__.py:50-55` (+ `:46-48`) | `params.resolved_mode` is always set by `params.py:148-159` and the registry covers every `EditMode` (`test_mode_registry_covers_every_edit_mode`) | delete, or keep and mark with a `# pragma: no cover` | waiver candidate, 6 lines |
| S1-5 placeholder dead code | `sloppy.py:269-271` (`if canonical in seen_paths and not op.all_match: pass`) and `:324-325` (`if params.allow_conflicts: pass`) | the `pass` bodies prove the intended duplicate-path handling was never written; `seen_paths` is write-only | implement (see §4 (a)) or delete | none |
| S1-6 dead normalisation | `params.py:166` | `mode` is `Literal["auto","replace","sloppy"]` (`params.py:48`) so `"REPLACE"`/`"  sloppy  "` never reach `normalize_edit_mode`; `normalize_edit_mode("  sloppy  ") is None` (probe 01) | drop the `.lower()/replace()` dance, or widen the field to `str` | — |
| S1-7 test-only delegation shims | `edit/__init__.py:63-98` | 8 wrappers × `ReplaceModeExecutor()` construction; only `object.__new__(EditFile)` tests use them | move to a test helper / read-only alias | — |
| S1-8 ruff: 33 errors in scope (34 with `modes/__init__.py`) | `sloppy.py:170,171` (`E741` ×2), `sloppy.py:187` (`F541` f-string with no placeholder), `replace.py:395,521`, `base.py:172,178,212,216`, `params.py:59` (`UP007`), `params.py:149` (`UP037`), I001 ×5, `modes/__init__.py:11` (`F401 Callable`) | `ruff check` output in §G1a | `ruff check --fix` + manual E501 wraps | none |
| S1-9 inconsistent 2-space indentation | `replace.py:343-348` and `:351-362` | `sed -n … \| cat -A` shows the `for`/`if` bodies indented by 2 where the rest of the file uses 4 | reformat | cosmetic |
| S1-10 unnamed fuzzy magic numbers | `replace.py:58,111` (`cutoff=75.0`, 0-100 scale) vs `sloppy.py:150` (`threshold=0.75`, 0-1 scale) vs `sloppy.py:163` (`/100.0`) | two spellings of the same concept in two unit systems | one named constant per module (`FUZZY_CUTOFF_PCT = 75.0`) | — |
| S1-11 ignored parameter field | `params.py:84-87` | `sandbox_permissions` is declared + documented but never read anywhere in the tool (probe 18 asserts it never reaches the approval prompt) | consume it or delete it | — |
| S1-12 internal field leaks into the tool schema | `params.py:106` | `resolved_mode` appears in `EditParams.model_json_schema()["properties"]` and in `edit_file_tool.base.parameters["properties"]` (probe 12); `exclude=True` only hides it from dumps | `Field(exclude=True, json_schema_extra=…)`/`PrivateAttr`, or make it non-field state | — |
| S1-13 optional: `execute` complexity in sloppy (23 > 15 hard) | `sloppy.py:247` | `ruff C901 execute is too complex (18 > 12)`; `review_metrics complexity=23` | extract the write-phase loop | none — hard breach |

Long-function table (all > 40 LOC from `review_metrics`):

| function | LOC | complexity | decision |
|---|---|---|---|
| `replace.py::execute` | **239** | **38** | **split (mandatory)** — > `max_fn_loc_hard` 200 |
| `sloppy.py::execute` | 117 | **23** | keep LOC (≤120) but reduce complexity (split the 300-356 write loop) |
| `replace.py::_try_snapshot_recovery` | 57 | 12 | keep (at the complexity limit) |
| `sloppy.py::_parse_op` | 48 | 11 | keep |
| `sloppy.py::_apply_block_op` | 38 | 11 | keep |
| `base.py::_validate_path` | 34 | 5 | keep |

Module LOC: `replace.py 525` (plan says ≈550 ✓), `sloppy.py 363` (≈350 ✓), `base.py 218`, `params.py 194`, `edit/__init__.py 107`, `replace.py` shim 12 — **no module ≥1200**, so no split decision is mandatory at module level; the split decision is at function level (`execute`). `module_loc` total 1419 matches `reviews/tools/gates/C10.json`.

vulture: `--min-confidence 90` → **0 hits** (`[exit=0]`). This does **not** clear S1-2: vulture does not report unused *methods* of a class, which is why the six dead helpers must be evidenced by grep (as above). `vulture_waived = 0`.

LOC non-regression: `uv run tools/git_diff.py <in-scope files>` → `[exit=0]`, no hunks (no source file was touched by this review).

## 4. S2 Potential issues (G2 detail)

13/13 items answered. `open_high = 2`, `open_medium = 5`, `waived_medium = 0`.

| # | item | severity | file:line | evidence | impact | fix | waiver |
|---|---|---|---|---|---|---|---|
| 1 | (a) error contract | **high** | `sloppy.py:256-346`, `269-271` | probe 13c: two sections for the same path → the tool returns `ToolError(brief="Stale file")` **after** having committed the first section's write (`multi_a.ts` = `ALPHA\nbeta\n`). `seen_paths` is collected but the guard is an empty `pass` (269-271); new contents are all computed from the same pre-read text (281-298) and written one-by-one (300-346) | non-atomic, partially-applied edits + a misleading error; the last section would clobber earlier ones | implement the duplicate-path merge/reject the placeholder intended, and compute each section against the running content | none |
| 2 | (a) error contract | **high** | `base.py:94-98` | `_read_text`/`_write_text` use `errors="replace"`; probe 16e: `b"caf\xe9 here\n"` + one edit → `b"caf\xef\xbf\xbd HERE\n"` | any edit to a non-UTF-8 (Latin-1/CP-1252/binary-ish) file silently rewrites every invalid byte as U+FFFD — whole-file, irreversible corruption | refuse non-UTF-8 (or edit bytes) instead of `errors="replace"` | none |
| 3 | (a)/(i) error contract | medium | `replace.py:364-373`, `377-401`, `403-406` | probe 09: `[one→1, one→2]` on `one two` → success, `"2 edit(s) with 1 total replacement(s)"`; only an all-edits-miss call errors | a dropped edit inside a multi-edit payload is silent; the model must parse the count to notice | surface per-edit misses (warn/error listing the anchors) | — |
| 4 | (a)/(i) limits | medium | `replace.py:218-220`; `sloppy.py:234-237` | probe 10: repeated anchor + `replace_all=False` replaces the FIRST occurrence and reports success — there is **no ambiguous-match `ToolError`** (the G4 item-10 expectation is not implemented) | model intent ("replace the one I mean") is not enforceable; wrong occurrence can be edited | add an ambiguity guard (or document `replace_all`) | — |
| 5 | (e)/(l) path safety / parity | medium | `sloppy.py:260,273` vs `replace.py:319` | replace calls `tool._resolve_for_write(...)` (VFS remap, `base.py:86-89`); sloppy uses `kaos_path_from_tool_input` directly | VFS/overlay-mapped files are editable in replace mode but not in sloppy mode — silent behavioural divergence | route sloppy through `_resolve_for_write` too | — |
| 6 | (j) state | medium | `replace.py:450-456` vs `sloppy.py:336-343` | probe 13e: replace allows a write-after-write with only a note; sloppy returns `ToolError("Stale file")` | consecutive sloppy edits of the same file always fail (and multi-section payloads always fail, cf. #1) | one staleness policy for both modes | — |
| 7 | (i) limits | medium | `base.py:168-175` | probe 06: a bare `=======` line (Markdown setext underline, YAML doc separator, RST) is treated as a conflict marker and blocks the whole file; an 8-char `========` is not | false-positive refusals with a workaround (`allow_conflicts=true`) that also disables real protection | require a `<<<<<<<`/`>>>>>>>`-anchored block, or only flag `=======` between such anchors | — |
| 8 | (b) async | low | `sloppy.py:283-298` | the whole sloppy plan (rapidfuzz `fuzz.ratio` over windows, `_find_fuzzy_block`) runs on the event loop; replace offloads to `asyncio.to_thread` (`replace.py:375,381`) | event-loop stalls on large payloads (probe 14b pushes 2000 selection lines through this path) | wrap the plan in `asyncio.to_thread` | — |
| 9 | (k) params | low | `params.py:84-91` | `sandbox_permissions` declared/documented but never read (probe 18); `resolved_mode` leaks into the advertised schema (probe 12) | the schema advertises behaviour the tool does not have | consume or delete `sandbox_permissions`; hide `resolved_mode` from the schema | — |
| 10 | (a) error contract | low | `edit/__init__.py:50-55` | unreachable "Invalid edit mode" arm (§3 S1-4) | untested error path; if the registry were ever incomplete the message would be right, so it is defence-in-depth | cover with the probe added (`test_probe15_dispatcher_unknown_mode_is_a_tool_error`) or delete | — |
| 11 | (m) library policy | info | `sloppy.py:5` | `import re` in a file that uses two linear regexes | AGENTS.md prefers `regex` | `import regex as re` | X01 owns the finding |
| 12 | (c)/(d)/(f)/(g)/(h) | clean | `base.py`, `replace.py`, `sloppy.py` | no temp files, no handles held (KaosPath read/write complete), no subprocess, no shell, no network, no child env, no credential output. ReDoS: `_SECTION_RE`/`_INLINE_RE` (`sloppy.py:49,51`) are anchored with no nested quantifiers | — | — | — |
| 13 | (j)/(k) state & params | clean (rest) | `params.py`, `edit/__init__.py` | `MODE_REGISTRY` is never mutated (`monkeypatch` only in the probe); no module-level mutable cache in scope; `EditFile` holds no per-call mutable state; aliases round-trip (probe 12, `model_validate(model_dump(by_alias=True))`) | — | — | — |

Native parity (item l): **no native module exists for `edit`**; the only delegating module is the 12-line shim `file/replace.py`, whose identity is asserted in probe 11 (`shim.EditFile is edit.EditFile`, `shim.Params is edit.EditParams`, `shim.Edit is edit.ReplaceEditItem`). The plan's probe text says `Edit is file.edit.Edit`, but `kimi_cli.tools.file.edit` does **not** export `Edit` (probe 11 asserts `not hasattr(edit_pkg, "Edit")`) — deviation recorded in §9, not a parity divergence.

## 5. S3 Coverage (G3 detail)

Gate command (verbatim from §4.4 C10) + `--cov-fail-under=85` → **FAIL**: `Total coverage: 79.29%` (floor 85), `TOTAL 777 136 300 57 79%`, `[exit=1]`. Per module: `edit/__init__.py 82%`, `base.py 68%`, `modes/__init__.py 100%`, `modes/replace.py 88%`, `modes/sloppy.py 69%`, `params.py 91%`.

Behaviour → asserting test map (in-scope tests + this review's probe file):

| behaviour | asserting test | status |
|---|---|---|
| auto mode detection (replace/list/§-input/ambiguous) | `test_edit_modes_detect.py::test_detect_*`; probe 01 | covered |
| explicit `replace`/`sloppy` mode | probe 02; `test_edit_sloppy.py::test_sloppy_inline_selection` | covered |
| shorthand `old/new` == list form | probe 04 | covered |
| `max_replacements` boundary (1/5/0/−1) | probe 03; `test_edit_file_fuzzy.py::test_apply_replace_all_max_replacements` | covered |
| fuzzy strip-match / fuzzy / suggestion / below cutoff | probes 05, 09; `test_edit_file_fuzzy.py::test_apply_edit_fuzzy_*`, `test_apply_fuzzy_fallback_chain` | covered |
| CRLF/LF normalisation | `test_edit_file_fuzzy.py::test_apply_edit_*crlf*` | covered |
| conflict markers refused / `allow_conflicts` | probes 06, 13d; `test_edit_conflict.py::*`, `test_edit_conflict_plan24.py::*` | covered |
| parse guard warning + rollback + blackbox | probe 07; `test_edit_file_parse_repair.py::*`, `test_edit_safety.py::*` | covered |
| snapshot recorded + fs-cache invalidated | probe 08 | covered |
| multi-edit ordering / partial no-op / non-overlap | probe 09 | covered |
| missing file / no-match / empty path / directory | probe 10; `test_edit_file.py::test_replace_nonexistent_file`, `test_replace_directory_instead_of_file`, `test_replace_empty_path` | covered |
| ambiguous match → distinct error | probe 10 (characterises the ABSENCE) | **gap vs the plan text** (§4 #4) |
| shim identity | probe 11 | covered |
| every alias (`file_path`/`path`, `edit`/`edits`, `old`/`old_string`, `new`/`new_string`) | probe 12 | covered |
| protected write path | probe 16a | covered (new) |
| format guard json/xml/yaml/toml + json repair | probe 16b/16c; `test_edit_file.py::test_replace_invalid_json/xml/valid_json/xml` | covered (new) |
| generic write failure → `ToolError` | probe 16d; `test_edit_file.py::test_replace_oserror_on_write` | covered |
| non-UTF-8 content | probe 16e | covered (new, characterises risk #2) |
| `allow_auto_generated` guard | probe 17 | covered (new) |
| `justification` / `sandbox_permissions` | probe 18 (new) | covered |
| approval rejection (replace + sloppy) | `test_edit_file.py::test_replace_approval_rejected`; probes 13d | covered |
| sloppy: parse errors, bare `§`, no-change, inline miss, all-match, block rewrite/delete, fuzzy block | probe 13a/13b; `test_edit_sloppy.py::*` | covered |
| sloppy: multi-section same-path clobbering | probe 13c | covered (characterises risk #1) |
| snapshot recovery on drift | `test_edit_file.py::test_replace_all_repeat_same_edit_reports_no_replacements`; probe 07 | partially covered |

Summary: behaviours mapped **26** / covered **25** / gaps **1** (ambiguous-match refusal, §4 #4). Coverage raised from 79.29% to **90.44%** by adding the probe file to the same command; within the verbatim 8-file command it stays 79.29%.

Waiver table — the 136 uncovered lines of the verbatim run, with the reason (budget 25 is the plan's default; C10's §4.4 block does not state one):

| lines | module | why it cannot be covered without writing tests for dead code |
|---|---|---|
| `base.py:101,104-106,142-156,187-195,199-203,208-215` (≈40 lines) | base | the six **dead** helpers (S1-2) — 0 callers; covering them would raise coverage while cementing dead code |
| `base.py:76-77,116,118,127-129` | base | covered by probes 16a/16b/16c **when the probe file is in the run** (68% → 75%) |
| `edit/__init__.py:31,35,39,43,48,52,73,88` | `__init__` | the 4 unused pass-through properties, the unreachable dispatcher arm (48/52), and 2 legacy shims covered by probe 15 only |
| `replace.py:91,101,104-105,164,246-247,261,265,271-272,274,282-285,296-299,356,389-390,410,433-435,455-456` | replace | CR-stripping, `max_replacements` no-match, the snapshot-recovery chain, format-error/except annotations; probes cover 91/164/356/433-435 (88% → 90%) |
| `sloppy.py:63,80,117,126,133,142,146,152-172,180,185,187,197-210,218,226,229-232,249,253-254,264,270,274-275,304,322,325,331,337,356` | sloppy | parse-error arms, `_find_fuzzy_block`, all-match, approval/conflict/stale arms — probes 13a-13d cover all but 7 lines (69% → 96%) |
| `params.py:113,146,155,165` | params | `_normalize_single_edit` early return, `_coerce_edit` raise, `_resolve_mode` raise, `normalize_edit_mode` falsy — reachable only by direct model construction |

`waived_lines = 136 > waiver_budget = 25` → the waiver row is itself a FAIL; the fix is delete-dead-code (S1-2) plus adding the probe file to the gate command.

## 6. G4 Behavioural probes

`kimi-cli/tests/tools/test_edit_gate.py` — **created by this review**, `uv run pytest … -q` → `28 passed` `[exit=0]` (required 12, present 28).

| probe | assertion (concrete) | result |
|---|---|---|
| 01 auto-mode detection | `resolved_mode == "replace"/"sloppy"`; `EditParams(input="no §")` and `EditParams(path=…)` raise `ValidationError`; `normalize_edit_mode("  sloppy  ") is None` | pass |
| 02 explicit modes | end-to-end file contents `ALPHA beta\n` / `alpha BETA\n`; `mode="patch"`, `"REPLACE"`, `" Sloppy "` all raise `ValidationError` | pass |
| 03 `replace_all`+`max_replacements` | `2/2/1` replacements, `"A A a a a\n"`, `max_replacements=0/-1` raise | pass |
| 04 shorthand == list | `shorthand.edit == explicit.edit == [ReplaceEditItem(old="old",new="new")]`; identical success messages | pass |
| 05 fuzzy threshold | strip-drift `count==1, suggestion is None`; dissimilar → `count==0`; boundary `cutoff=100.0` hits, `100.1` misses | pass |
| 06 conflicts | `brief=="Conflict markers detected"`, "line 2" named, file unchanged; `allow_conflicts=True` proceeds; `=======` refused; `========` allowed | pass |
| 07 parse repair | "no longer parses" + blackbox record `prev/next`; rollback call has no warning; introduce-and-restore in one call has no warning | pass |
| 08 snapshot + fs_cache | `get_file_snapshot_store(session).versions(key)` contains both `alpha\n` and `beta\n`; `key in invalidator_calls` | pass |
| 09 ordering / overlap | `"uno two\n"` vs `"1 two\n"`; all-miss → `brief=="No replacements made"`; `"AAA bbb CCC\n"`; `2 edit(s) with 1 total replacement(s)` | pass |
| 10 distinct errors | 4 distinct briefs (`File not found`, `Empty file path`, `Invalid path`, `No replacements made`); ambiguous → first-occurrence success (documented deviation) | pass |
| 11 shim identity | `shim.EditFile is edit.EditFile is file.EditFile`; `shim.Edit is edit.ReplaceEditItem`; `not hasattr(edit, "Edit")` | pass |
| 12 aliases | all 4 spellings end-to-end identical; schema keys/`required`; by-alias round-trip; `resolved_mode` leak asserted | pass |
| 13a-13e sloppy | parse errors, block/inline/all-match/fuzzy/no-change, marker+approval-rejection, same-path clobbering, staleness divergence | pass |
| 14a/14b error contract & hostile input | `None`/`""`/`[1,2,3]`/`edits=5`/missing `new`/no-mode → `is_error`; JSON-string arg repaired; 2000-line payload OK; 200 000-char old string; NUL/unicode; shell metacharacters literal | pass |
| 15 dispatcher | `resolved_mode=None` repaired; registry miss → `brief=="Invalid edit mode"` | pass |
| 16a-16e remaining ToolError sites | protected path, YAML/TOML format guard, JSON repair + unrepairable, `OSError` on write, non-UTF-8 corruption | pass |
| 17 `allow_auto_generated` | `zz_generated.py` and `# @generated` header refused (`brief=="Auto-generated file"`); flag bypasses | pass |
| 18 `justification`/`sandbox_permissions` | prompt ends with the justification; `sandbox_permissions` never surfaces | pass |

Params-field coverage: all 12 `EditParams` fields (`mode`, `file_path`, `edit`, `old_string`, `new_string`, `replace_all`, `input`, `sandbox_permissions`, `justification`, `allow_conflicts`, `allow_auto_generated`, `resolved_mode`) are exercised by ≥1 probe; all 5 `ReplaceEditItem` fields likewise. Alias coverage: `file_path`/`path`, `edit`/`edits`, `old`/`old_string`, `new`/`new_string` — each probed independently and end-to-end (probe 12). Boundary coverage: `max_replacements` 0/1/2/5, fuzzy cutoff 100.0/100.1, empty/oversized payloads, 4 distinct error briefs. Every `ToolError` return site in scope is reached by ≥1 probe except `replace.py:492-499` (format validation), which the in-scope test `test_edit_file.py::test_replace_invalid_json` covers and probe 16b/16c also reach.

## 7. G5/G6 Evidence

Raw commands (all appended to `reviews/tools/gates/C10.txt`, line 1 = `git rev-parse HEAD`, line 2 = `git status --porcelain`):

```
G1a  uv run ruff check <6 in-scope files>                                  -> Found 33 errors   [exit=1]
G1a2 + edit/modes/__init__.py                                             -> Found 34 errors   [exit=1]
G1b  uv run ruff check --select C901 --config lint.mccabe.max-complexity=12 -> 2 errors (execute 33>12, 18>12) [exit=1]
G1c  uv run tools/review_metrics.py <files> --json                         -> [exit=0] (JSON in evidence file)
G1d  ... --max-fn-loc 120 --max-complexity 12                              -> 3 breaches        [exit=1]
G1e  uv run --with vulture vulture <files> --min-confidence 90 --sort-by-size -> (no output)    [exit=0]
G1f  uv run tools/git_diff.py <files>                                      -> no hunks          [exit=0]
G3a  verbatim G3 command                                                   -> TOTAL 777 … 79% / 104 passed [exit=0]
G3b  verbatim G3 command + --cov-fail-under=85                             -> 79.29% < 85       [exit=1]  <-- G3 gate
G3c-final2 verbatim + test_edit_gate.py (28 probes) + --cov-fail-under=85   -> 90.44% / 132 passed [exit=0]
G3d  verbatim G3 command -rs                                               -> 104 passed, 0 skipped/xfailed [exit=0]
G4   uv run pytest kimi-cli/tests/tools/test_edit_gate.py -q               -> 28 passed         [exit=0]
G5a  uv run pytest tests -q (ROOT)                                         -> collection ERROR (tests/bash/cases.json missing) [exit=2]
G5b  uv run pytest kimi-cli/tests -q (CLI)                                 -> 43 failed, 5179 passed [exit=1]
G5c  uv run tools/syntax_check.py kimi-cli/tests/tools/test_edit_gate.py   -> Syntax OK          [exit=0]
G5d  uv run tools/git_diff.py <this unit's artifacts>                      -> see §G5d
```

G5 analysis:
* ROOT suite: `ERROR tests/test_bash_e2e.py - ValueError: cannot read corpus file …tests\bash\cases.json` → collection abort. This is the **pre-existing** condition documented in `reviews/tools/00-baseline.txt:31-32`; this unit changes nothing under `tests/`. Verdict: **blocked by pre-existing repo state**, not a regression.
* CLI suite: `43 failed, 5179 passed, 100 skipped, 1 xfailed, 1 xpassed`. A set-difference of the `FAILED` lines against the baseline gives **0 new failures and 0 newly-passing** (`baseline FAILED: 43`, `new-run FAILED: 43`, both diffs empty). The 43 failures are the pre-existing ones (`test_grep_engine_parity` ×36, `test_read_file` ×2, `test_session_logging` ×1, `test_toolset_todo_redirects` ×2, …) named in `00-baseline.txt`. Verdict: **pass (no new failures)**. The +24 passes come from this unit's probe file (21–28 tests) and unrelated concurrent units.
* `syntax_check` clean; `git_diff` on this unit's files shows only the intended new artifacts.

G6 notes for the orchestrator:
* Expected `complexity_max = 38`, `longest_fn_loc = 239`, `longest_fn = "execute"`, `module_loc = 1419`, `functions_over_120_loc = 1`, `functions_over_12_cx = 2` — identical to `reviews/tools/gates/C10.json`.
* Coverage: the harness `tools/review_gates.py:207-213` runs the coverage command **without** `--cov-fail-under`, so `gates/C10.json` reports `cov_rc=0 / cov="pass"`. That field is **not** a floor check. The plan's G3 line carries `<FLOOR:85>`; adding it gives `79.29% → [exit=1]` (G3 FAIL). `cov_actual = 79.29`.
* Coverage is reproducible at `TOTAL 777 … 79%` (5 of 7 runs). Twice the `--cov-fail-under` invocation additionally measured one unrelated module (`src\kimix\tools\common.py` 1106 stmts → 64.98%; `src\kimix\tools\file\run.py` 390 stmts → 65.78%); both variants are still a FAIL and the anomaly is a pytest-cov/source-resolution artifact, not a property of the edit package (see §G3 NOTE in the evidence file).
* Probe count `28`; G4 command `[exit=0]`.

## 8. Git provenance

- `git rev-parse HEAD` at review time — `52192b7c3a1baa66c96e0015d2868f3b106fc043`, equal to line 1 of `reviews/tools/gates/C10.txt`.
- `git status --porcelain` at review start (line 2 of the evidence file) — **not clean**: ` M kimi-cli/src/kimi_cli/tools/file/read_media.py` (another concurrent wave-1 unit) plus a long list of untracked peer artifacts (`reviews/tools/gates/C0*.txt`, `X0*.txt`, `reviews/tools/9*.md`, `.coverage`). This unit's artifacts are exactly `kimi-cli/tests/tools/test_edit_gate.py`, `reviews/tools/C10-edit-editfile.md`, `reviews/tools/gates/C10.txt` — nothing else was created, and **no source or test file was modified** (G1f/G5d confirm an empty diff for the in-scope files).
- commit SHA (filled in by the orchestrator after acceptance): …
- tag: `tools-review/p2-w1`
- `git diff --stat <prev-tag> HEAD -- <in-scope files>` — see §G5d in the evidence file (no changes to the six in-scope source files; the reviewed revision is the base tag itself).

## 9. Duplication / native-parity / policy notes

* Duplication: sloppy's inline approval request + marker scan duplicate `BaseEditTool._request_approval` / `_check_conflicts` (S1-3); the six dead `base.py` helpers are a second, unused copy of flows that `replace.py`/`sloppy.py` implement inline (S1-2); the fuzzy-cutoff concept is spelled twice in two unit systems (S1-10).
* Native parity: **no native counterpart for `edit`** exists in this tree; `kimi_cli/tools/file/replace.py` is a pure re-export shim, and probe 11 pins the identities.
* Gate-text deviation: the plan's probe 11 asserts `Edit is file.edit.Edit`; `kimi_cli.tools.file.edit` exports `ReplaceEditItem`, not `Edit` (`edit/__init__.py:101-107`). Probe 11 asserts the real identity (`shim.Edit is edit.ReplaceEditItem`) **and** the absence of `Edit`, so the deviation is visible; adding `Edit = ReplaceEditItem` to the package's `__all__` would close it (Phase 4, cosmetic).
* Library policy (X01 owns the finding): `sloppy.py:5` uses `import re` where AGENTS.md prescribes `regex`; the module's two patterns are linear and ReDoS-safe. No `json`/`hashlib`/`base64`/`datetime`/`sqlite3`/`difflib` imports in scope; `pendulum` is used per policy (`replace.py:10`).
* Risk register overlap: X02 (async/resources) owns the missing `to_thread` in sloppy; X03 (security) owns the `errors="replace"` data-loss and the conflict false positive. Filed here too so the tool-level gate cannot go green while they stand.

## 10. Verdict + JSON

```json
{
  "id": "C10",
  "tool": "edit (EditFile)",
  "files": [
    "kimi-cli/src/kimi_cli/tools/file/edit/__init__.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/base.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/params.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py",
    "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py",
    "kimi-cli/src/kimi_cli/tools/file/replace.py"
  ],
  "verdict": "major",
  "gates": {
    "G0_scope": "pass",
    "G1_simplicity": {"ruff": "fail", "complexity_max": 38, "longest_fn_loc": 239,
                      "loc_regression": "none", "vulture_hits": 0, "vulture_waived": 0},
    "G2_risk": {"items_answered": 13, "open_high": 2, "open_medium": 5, "waived_medium": 0},
    "G3_coverage": {"module": "kimi_cli.tools.file.edit", "floor": 85,
                    "actual": 79.29, "waived_lines": 136, "waiver_budget": 25},
    "G4_behavior": {"probe_file": "kimi-cli/tests/tools/test_edit_gate.py",
                    "probes_required": 12, "probes_present": 28, "result": "pass"},
    "G5_regression": {"root_suite": "blocked-pre-existing", "cli_suite": "pass-no-new-failures",
                      "syntax_check": "pass", "git_diff": "pass"},
    "G6_independent_rerun": "pending-orchestrator"
  },
  "findings": [
    {"severity": "high", "axis": "risk",
     "title": "sloppy multi-section same-path edits are non-atomic and partially applied",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:269",
     "evidence": "probe 13c: `§f ⟪alpha│ALPHA⟫ § ⟪beta│BETA⟫` returns ToolError(brief='Stale file') but leaves ALPHA on disk; seen_paths guard body is `pass` (269-271)",
     "impact": "data loss / misleading error; every bare-§ continuation payload fails after a partial write",
     "fix": "reject duplicate canonical paths or fold all sections for a path into one plan+write; remove the `pass` placeholder",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe13c_sloppy_multi_section_duplicate_path_characterisation"},
    {"severity": "high", "axis": "risk",
     "title": "non-UTF-8 files are silently corrupted by errors=\"replace\"",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/base.py:95",
     "evidence": "probe 16e: b'caf\\xe9 here\\n' + one edit -> b'caf\\xef\\xbf\\xbd HERE\\n'",
     "impact": "any edit of a Latin-1/CP-1252/binary-ish file irreversibly rewrites every invalid byte as U+FFFD",
     "fix": "validate UTF-8 on read and refuse (or edit bytes) instead of errors=\"replace\" on both read and write",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe16e_non_utf8_bytes_are_silently_replaced"},
    {"severity": "medium", "axis": "simplicity",
     "title": "replace.execute is 239 LOC / complexity 38 (hard metric breach)",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py:287",
     "evidence": "review_metrics: execute loc=239 complexity=38 branches=37; ruff C901 'execute is too complex (33 > 12)'",
     "impact": "G1 FAIL (>max_fn_loc_hard 200); the flow cannot be unit-tested or reviewed as a unit",
     "fix": "extract _preflight/_plan/_commit helpers (sloppy.py already separates plan from write)",
     "test_to_add": "n/a (refactor; behaviour is already pinned by the 104 in-scope tests + probe file)"},
    {"severity": "medium", "axis": "simplicity",
     "title": "~110 LOC of base.py helpers have zero callers",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/base.py:100",
     "evidence": "grep --include=*.py over kimi-cli/src + kimi-cli/tests + tests: _remove_file, _ensure_parent, _request_approval, _check_staleness, _stat_is_regular_file, _wrap_exception -> 0 usages each (evidence §G1c/G1d missing-line columns)",
     "impact": "half of base.py is dead; it is also the dominant cause of 68% module coverage",
     "fix": "delete the six helpers",
     "test_to_add": "n/a (removal); makes G3 reachable inside the verbatim command"},
    {"severity": "medium", "axis": "risk",
     "title": "multi-edit payloads silently drop individual edits",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py:364",
     "evidence": "probe 09: edits [one->1, one->2] on 'one two' -> success 'Applied 2 edit(s) with 1 total replacement(s)'",
     "impact": "the model cannot tell which anchor failed; wrong files look edited",
     "fix": "collect per-edit outcomes and surface unmatched anchors (warn or error)",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe09_multi_edit_ordering_and_overlap"},
    {"severity": "medium", "axis": "risk",
     "title": "repeated anchor is replaced without any ambiguity error",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/replace.py:218",
     "evidence": "probe 10: 'x x x' + old='x' -> success, '1 total replacement(s)', file 'Y x x\\n'; no ToolError site exists for ambiguity",
     "impact": "plan requirement G4-10 ('ambiguous match -> distinct ToolError') is unimplemented; the wrong occurrence can be edited",
     "fix": "count occurrences and refuse (or require replace_all) when old occurs >1 time",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe10_missing_no_match_and_ambiguous_outcomes"},
    {"severity": "medium", "axis": "risk",
     "title": "sloppy ignores the VFS write resolution that replace honours",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:260",
     "evidence": "replace.py:319 calls tool._resolve_for_write(); sloppy.py:260/273 use kaos_path_from_tool_input + _read_text directly",
     "impact": "VFS/overlay-mapped edits work in replace mode and silently target the wrong file in sloppy mode",
     "fix": "route every sloppy op through _resolve_for_write",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py (add a VFS-mapped sloppy probe once the fix lands)"},
    {"severity": "medium", "axis": "risk",
     "title": "staleness policy diverges between replace and sloppy modes",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:336",
     "evidence": "probe 13e: replace allows write-after-write with a note; sloppy returns ToolError(brief='Stale file') for the same sequence",
     "impact": "any consecutive sloppy edit of one file fails; multi-section payloads always fail",
     "fix": "share one staleness policy (the plan-25 M2 note-only policy) across modes",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe13e_staleness_policy_diverges_between_modes"},
    {"severity": "medium", "axis": "risk",
     "title": "bare '=======' is treated as a conflict marker and blocks whole files",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/base.py:168",
     "evidence": "probe 06: 'a\\n=======\\nb\\n' -> brief 'Conflict markers detected'; '========' (8) is allowed",
     "impact": "false-positive refusal for Markdown setext underlines / YAML doc separators; the workaround (allow_conflicts=true) disables real protection",
     "fix": "only flag '=======' when enclosed by '<<<<<<<' and '>>>>>>>' anchors",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe06_conflict_markers_refused_and_allowed"},
    {"severity": "low", "axis": "risk",
     "title": "sloppy planning runs fuzz matching on the event loop",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:283",
     "evidence": "replace.py:375,381 offload to asyncio.to_thread; sloppy.py:281-298 does all rapidfuzz work inline (probe 14b drives 2000 selections through it)",
     "impact": "event-loop stalls proportional to payload size",
     "fix": "await asyncio.to_thread(self._plan_all, prepared)",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe14b_oversized_and_hostile_payloads"},
    {"severity": "low", "axis": "simplicity",
     "title": "internal resolved_mode is advertised in the tool parameter schema",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/params.py:106",
     "evidence": "probe 12: 'resolved_mode' in EditParams.model_json_schema()['properties'] and in edit_file_tool.base.parameters['properties']",
     "impact": "the model is offered a parameter that the validator always overwrites; schema noise",
     "fix": "exclude the field from the schema (PrivateAttr or a custom schema generator)",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe12_every_params_alias"},
    {"severity": "low", "axis": "simplicity",
     "title": "sandbox_permissions is declared, documented and never read",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/params.py:84",
     "evidence": "probe 18: the value never reaches the approval prompt or the result message; grep finds no reader in the edit package",
     "impact": "documented behaviour that does not exist",
     "fix": "consume it or delete the field",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe18_justification_and_sandbox_permissions_fields"},
    {"severity": "low", "axis": "simplicity",
     "title": "normalize_edit_mode's case/space tolerance is unreachable through EditParams.mode",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/params.py:166",
     "evidence": "probe 01/02: mode is a closed Literal, so 'REPLACE' and ' Sloppy ' raise ValidationError; '  sloppy  ' -> None",
     "impact": "dead normalisation logic; mode strings with stray whitespace are rejected",
     "fix": "strip/normalise the raw mode in a before-validator, or drop the mapping",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe01_auto_mode_detection_replace_and_sloppy_shapes"},
    {"severity": "info", "axis": "cross-cutting",
     "title": "library policy: sloppy.py uses stdlib re instead of regex",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:5",
     "evidence": "import re at the top of the module; AGENTS.md mandates regex as a drop-in replacement",
     "impact": "policy deviation only (patterns are linear and safe)",
     "fix": "import regex as re (X01 owns the finding)",
     "test_to_add": "n/a (policy gate tools/gate_library_policy.py)"},
    {"severity": "info", "axis": "cross-cutting",
     "title": "sloppy duplicates the base approval and conflict-guard implementations",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:312",
     "evidence": "inline _approval.request + 55-line marker scan vs base.py:132-156 and base.py:158-183, with a different refusal message",
     "impact": "two copies to keep in sync; error wording differs between modes",
     "fix": "call tool._request_approval / tool._check_conflicts",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe13d_sloppy_conflict_markers_and_approval_rejection"},
    {"severity": "info", "axis": "coverage",
     "title": "plan probe 11 asserts a name the package does not export",
     "file": "kimi-cli/src/kimi_cli/tools/file/edit/__init__.py:101",
     "evidence": "probe 11: hasattr(kimi_cli.tools.file.edit, 'Edit') is False; __all__ exports ReplaceEditItem",
     "impact": "the gate text cannot be satisfied literally; the shim identity is still pinned",
     "fix": "optionally add `Edit = ReplaceEditItem` to the package __all__ (Phase 4)",
     "test_to_add": "kimi-cli/tests/tools/test_edit_gate.py::test_probe11_shim_identity"}
  ],
  "coverage": {"behaviors_mapped": 26, "covered": 25, "gaps": 1,
               "gap_list": ["ambiguous repeated anchor has no distinct ToolError (replace.py:218)"]},
  "gate_evidence": "reviews/tools/gates/C10.txt",
  "git_sha": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "worktree_clean_at_start": false,
  "needs_fix_phase": true,
  "duplication_notes": [
    "sloppy.execute inlines the approval request and the conflict-marker scan that base.py._request_approval/_check_conflicts already implement (different refusal wording).",
    "base.py keeps six dead copies of flows (remove/ensure-parent/stat/staleness/wrap-exception) that neither executor calls.",
    "The fuzzy similarity threshold is expressed twice in different units: cutoff=75.0 (0-100, replace.py:58,111) and threshold=0.75 (0-1, sloppy.py:150).",
    "file/replace.py is a pure re-export shim (EditFile/Params/Edit) with no behaviour of its own."
  ]
}
```
