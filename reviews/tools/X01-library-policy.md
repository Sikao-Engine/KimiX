# X01 — library-policy

## 0. Method

Frozen commit: `52192b7c3a1baa66c96e0015d2868f3b106fc043` (working tree clean except the new evidence file `?? reviews/tools/gates/X01.txt`).

In-scope roots (declared): `kimi-cli/src/kimi_cli/tools` (54 `.py`) and `src/kimix/tools` (30 `.py`) — 84 reviewed `.py` files.

Evidence collected (all recorded in `reviews/tools/gates/X01.txt`; nothing re-run for this report):

1. Gate run — `uv run tools/gate_library_policy.py kimi-cli/src/kimi_cli/tools src/kimix/tools` → `G-POLICY: FAIL`, `policy hits: 3  waived: 0  unparsable: 0`, `EXIT=1`.
2. Machine-readable form — same command with `--json` (full `hits` array; `waived: []`; `unparsable: 0`).
3. Waiver state — `cat tools/library_policy_allowlist.txt`; only 15 lines and `grep -n '^[^#]'` returns nothing, i.e. **zero active waivers**. The file's own header states the three seeded hits "are REAL violations owned by FP-05 … and are intentionally NOT waived here".
4. AST-based usage scan of the policy-module attribute/call sites for each hit (import line + every `use` line), so no hit is classified without knowing what the module is actually used for.
5. Runtime capability probe: `orjson 3.12.0`, `regex 2026.7.19`, `rapidfuzz 3.14.5`; `orjson.JSONDecodeError is json.JSONDecodeError -> False` (but MRO includes `json.decoder.JSONDecodeError`); `rapidfuzz has unified_diff: False`, with the full `rapidfuzz` public top level (`fuzz`, `process`, `utils`, `distance`, …) and `distance` modules (`DamerauLevenshtein`, `Hamming`, `Indel`, `Jaro`, …).
6. Test-pinning search for the three hit modules (`slopy/section/inline` edit tests, `extract_key_argument` tests, `best_of_n` tests) plus `JSONDecodeError` grep across `kimi-cli/tests` and `tests`.
7. Dependency check — `grep -n 'regex|rapidfuzz|orjson|xxhash|pendulum|apsw|pybase64|msgspec|uvloop' pyproject.toml`.
8. Optional-companion scans (context, not part of the pass/fail count): `hashlib`/crypto scan (`xxhash` adoption), dynamic-import evasion scan (`__import__`/`importlib`), and a **supplementary** gate run over the test trees (`kimi-cli/tests`, `tests`) → 72 hits, recorded here as observations only because the test trees are outside this unit's declared scope.

Dimensions checked: (a) import-level policy hits, (b) whether the import is live or dead, (c) whether the proposed replacement is capability-equivalent, (d) whether a waiver rationale already exists, (e) whether the replacement is a declared dependency, (f) test coverage that would catch a behaviour change.

## 1. Findings

Scope = the 3 hits reported for the in-scope source trees. All three are classified as findings; none is waived.

| severity | title | file:line | evidence | impact | fix | test_to_add |
|---|---|---|---|---|---|---|
| medium | `difflib` used for `unified_diff` in swarm diff generation; the prescribed replacement `rapidfuzz` has no unified-diff API and is not a declared dependency | `src/kimix/tools/swarm/best_of_n.py:23` (uses at `:194`, `:212`) | Gate hit `import difflib -> rapidfuzz`. AST scan: `L23 Import difflib`, `L194 difflib.unified_diff`, `L212 difflib.unified_diff`. `collect_diff()` (L165-220) builds the candidate diff that workflow selection and `apply_diff_to_workspace()` (L217+) consume. Runtime probe: `rapidfuzz has unified_diff: False`; `rapidfuzz` top level is `fuzz / process / distance / utils` only. `pyproject.toml` grep (lines 26-33) shows `orjson`, `regex`, `msgspec`, `uvloop`, `apsw` — **no `rapidfuzz` entry**. | The hit cannot be closed by a mechanical import swap. A naive `import rapidfuzz as difflib` (or `rapidfuzz.unified_diff`) raises `AttributeError` at the first diff computation — i.e. every swarm/workflow run with a copy-mode worker (`kind == "copy"`, L201-220) and every untracked-file diff (L190-205). If instead the diff producer is rewritten on `rapidfuzz.distance.Editops`, the emitted unified-diff text is consumed as a plain string by the selector prompt (L306-345) and by `apply_diff_to_workspace` only via `[workspace:kind]` prefix parsing (L294, L404), so format drift is silently survivable but would degrade selector quality. Diff text also appears in user-facing candidate reports. | Preferred (policy-honest, zero risk): since `difflib.unified_diff` is *not* replaced by any library in the AGENTS.md table — the table maps `difflib -> rapidfuzz` for *fuzzy matching*, which is not what this call site does — add a **module-scoped waiver** `src/kimix/tools/swarm/best_of_n.py::difflib  # unified_diff has no rapidfuzz counterpart; rapidfuzz covers fuzz/process only`, and adopt `rapidfuzz` where fuzzy work is actually needed (candidate/self-report similarity scoring in the selector path, L300-345) to satisfy the intent of the rule. Alternative (only if a waiver is refused): keep the diff *format* by emitting it ourselves from `rapidfuzz.distance.Editops`/`Indel` on the same line lists, pinning the emitter to `difflib.unified_diff` byte-equality. Either way `rapidfuzz` must be added to `pyproject.toml` before any code imports it. | `tests/test_best_of_n.py` — add `test_unified_diff_emitter_matches_difflib` (golden-line equality: same `old`/`new` line lists ⇒ byte-identical output, incl. `fromfile="a/<rel>"`, `tofile="b/<rel>"`, `/dev/null` untracked case at L190-205), keep/extend `test_parallel_sample_isolation_and_diffs` (L48-62, asserts each candidate's diff contains only its own file) and `test_collect_diff...` (L176-177 asserts `"new.txt" in diff`); add a policy regression test asserting `best_of_n.py` has no unwaived policy hit (see §3). |
| low | Redundant stdlib `json` import kept only for `except (orjson.JSONDecodeError, json.JSONDecodeError)` | `kimi-cli/src/kimi_cli/tools/__init__.py:1` | Gate hit `import json -> orjson`. The file **already imports `orjson` (L3)**; AST scan shows the only stdlib-`json` use is `L99 json.JSONDecodeError`. The except tuple at L99 is `except (orjson.JSONDecodeError, json.JSONDecodeError)`. Probe: `orjson.JSONDecodeError is json.JSONDecodeError -> False`, but MRO is `orjson.JSONDecodeError -> json.decoder.JSONDecodeError -> ValueError` — the stdlib member of the tuple is dead weight because it already catches everything `json.JSONDecodeError` would. Covered behaviour: `extract_key_argument` swallows malformed streamed tool args; `loads_relaxed` is imported from `kosong.utils.jsonx`. | Purely redundant-import/dead-except-arm debt: no runtime risk from the fix, but the extra arm hides which decoder actually raised and keeps a policy hit alive in the toolset core (C00), the hottest shared module. Missing the fix leaves `G-POLICY` red for the whole in-scope run. | Delete `import json` (L1) and narrow L99 to `except orjson.JSONDecodeError:` — the same set of inputs still maps to the same `None`/"unparsable args" path. Do **not** also swap `kosong.utils.jsonx.loads_relaxed` here (out of this unit's scope; four tests patch it with stdlib `json.JSONDecodeError`). | `kimi-cli/tests/tools/test_extract_key_argument.py` — it already patches `loads_relaxed` with `side_effect=json.JSONDecodeError("Extra data", "doc", 0)` (L70-75); add the mirror case with `side_effect=orjson.JSONDecodeError(...)` plus a direct assertion that a malformed arg string returns the fallback key argument instead of propagating, and that the module no longer imports stdlib `json` (policy regression test, §3). |
| low | `re` used for two Unicode-marker compile patterns in the sloppy-edit mode | `kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py:5` | Gate hit `import re -> regex`. AST scan shows exactly two live uses, both compile-only: `L49 _SECTION_RE = re.compile(r"^\s*§(\*?)(.*?)\s*$")` and `L51 _INLINE_RE = re.compile(r"⟪([^│⟫\n]+)│([^⟫\n]*)⟫")`. Dependencies are declared (`pyproject.toml:29 regex>=2023.0.0`) and `regex` is importable (`2026.7.19`). | Low blast radius (two module-level patterns, applied to already-decoded `str`), but the patterns contain literal Unicode separators (`§`, `│`, `⟫`) and depend on `\s`/match semantics; the fix must be a drop-in (`regex.compile` is API-compatible) with the edit pipeline re-verified, otherwise sloppy edit document parsing silently mis-sections and edits apply to the wrong region. Sitting in C10 (edit) makes a silent regression high-consequence. | Change L5 to `import regex as re` (drop-in; keep the alias so L49/L51 are untouched), then confirm the patterns are still equivalent under `regex` (both are matched with `.match`/`.fullmatch` on `str`; no backreference/recursion/timed features used, so no semantic gap expected). No `# type: ignore` or stub work needed — `regex` ships types. | `kimi-cli/tests/tools/test_edit_sloppy.py` and `kimi-cli/tests/tools/test_edit_modes_detect.py` (existing) — add an equivalence test that runs the same fixture corpus through both `re` and `regex` compiled `_SECTION_RE`/`_INLINE_RE` and asserts identical match/no-match and identical groups (including the `§*` variant and an inline marker containing `│`), so a future pattern edit cannot diverge between engines. Plus the §3 policy regression test. |

Net effect of §1 on the gate: 3 findings, 0 waivers. Applying the two `low` fixes and filing the module-scoped waiver for `best_of_n.py::difflib` (or implementing the emitter) is the minimum required to turn `G-POLICY` green for the in-scope trees.

## 2. Waivers / non-findings

- **Active waivers in scope: none.** `tools/library_policy_allowlist.txt` is 15 lines and every line is a comment (`grep -n '^[^#]'` → no output); `--json` reports `"waived": []`. The header explicitly records the three hits as intentional FP-05 baseline violations, so their presence is `FAIL`, not a waiver. Finding X01-F3 proposes the *only* waiver this unit would add, and it is not yet filed.
- **Observation (out of scope, 72 hits, not findings): the test trees.** The supplementary run `tools/gate_library_policy.py kimi-cli/tests tests` → `G-POLICY: FAIL`, `policy hits: 72  waived: 0  unparsable: 0`, `EXIT=1`. These trees are outside this unit's declared roots; they are recorded so the gate scope is explicit rather than silently green-biased. Composition: overwhelmingly `json -> orjson` (test serialization of fixtures/snapshots, e.g. `kimi-cli/tests/kosong/api_snapshot_tests/*`, `kimi-cli/tests/core/test_session_export.py:5`), then `re -> regex` (e.g. `kimi-cli/tests/core/test_config.py`-family `test_agent_spec.py:3`, `kimi-cli/tests/tools/test_grep.py:1599`, `tests/test_bash.py:3884`), then `datetime -> pendulum` (`kimi-cli/tests/background/test_manager.py:7`, `kimi-cli/tests/kosong/test_field_aliases.py:881`, `kimi-cli/tests/mcp/test_client.py:3`), `base64 -> pybase64` (`kimi-cli/tests/mcp/test_resources.py:3`, `kimi-cli/tests/tools/test_mcp_tool_result.py:5`, `kimi-cli/tests/tools/test_read_media_file.py:5`, `tests/test_bash.py:667`), `hashlib -> xxhash` (`tests/unit/tools/test_python.py:4`) and one `difflib -> rapidfuzz` (`tests/test_bash_e2e.py:70`). Several of these stdlib imports are *semantically load-bearing in tests* — e.g. `kimi-cli/tests/core/test_export_cli.py:189-203` and `kimi-cli/tests/tools/test_extract_key_argument.py:70-75` deliberately raise/patch **stdlib** `json.JSONDecodeError`, and `tests/test_sdk_export.py:231` catches `stdjson.JSONDecodeError` — so a blanket swap there would change what is being asserted. Recommendation for the owning units: scope the gate to source roots, or add a `kimi-cli/tests/`, `tests/` waiver entry with the rationale "test doubles must exercise stdlib exception types".
- **Non-finding: no dynamic-import evasion of the policy.** The `__import__`/`importlib` scan only surfaces `read_archive.py:265-269` lazily importing `gzip`/`bz2`/`lzma` (optional-decompressor loading; none of the three is in the policy table) — not an avoidance pattern for a policy module.
- **Non-finding: `xxhash` already adopted where hashing is hot.** `file/hash_line.py` (`xxhash.xxh32`, L85), `file/micro_compress.py:41` (`xxhash.xxh64`), `file/snapshot_store.py:32` (`xxhash.xxh32`/`xxh64`) and `web/content.py:82` (`xxhash.xxh64`) — no `hashlib` hit anywhere in either in-scope tree. Positive evidence the rule is being followed elsewhere.
- **Observation (dead import, no gate hit):** `src/kimix/tools/swarm/best_of_n.py:147` — `import xxhash  # noqa: F401 — fast hashing available if needed later`. Not a policy violation (xxhash is the prescribed library) but it is an unused import with a suppression comment; clean up in the same change as X01-F3.
- **Observation (dependency gap, affects fix feasibility):** the root `pyproject.toml` grep matches only lines 26-33 — `orjson>=3.11.9` (L26), `regex>=2023.0.0` (L29), `msgspec>=0.19.0` (L30), `uvloop>=0.22.1; sys_platform != 'win32'` (L31), `apsw>=3.45.0` (L32), plus a `rich` line whose comment mentions `regex`. **No `rapidfuzz`, `xxhash`, `pybase64` or `pendulum` entry appears**, even though `rapidfuzz 3.14.5` is importable at runtime (transitive/other config). Before X01-F3's alternative fix (or any future swap) is attempted, `rapidfuzz` must be declared explicitly — which is a further argument for the waiver-first option.
- **Non-finding: no `.pyi` shadowing.** The `.pyi` search found nothing in either tree, so no stub file can mask a stdlib import from the gate.

## 3. Gate result

**FAIL (blocking).** `uv run tools/gate_library_policy.py kimi-cli/src/kimi_cli/tools src/kimix/tools` → `G-POLICY: FAIL`, `policy hits: 3  waived: 0  unparsable: 0`, exit code 1.

- Gate is **re-runnable and determinate**: the bare command (no flags) reproduces the same 3 hits; `--json` agrees with the text output (`hits` length 3, `waived` empty, `unparsable: 0`).
- The gate is **not** satisfiable by editing only the allowlist in this unit's current state: `tools/library_policy_allowlist.txt` carries zero active entries, and the header declares these three hits unwaived by design (FP-05-owned).
- Exit criteria for this unit: (1) X01-F1 and X01-F2 fixed (2 one-line changes with no behavioural delta), (2) X01-F3 closed by either the module-scoped `difflib` waiver or an emitter rewrite, and `rapidfuzz` declared if the rewrite path is taken, (3) regression test added so the gate cannot regress silently, (4) re-run of the *same* command reproduces `waived: 1` (or `0` if the emitter path is taken) and `EXIT=0` on the in-scope trees.
- Not in this unit's exit criteria: the 72 test-tree hits (§2) — they belong to the test-tree owner and must be resolved either by scope definition or by an explicit `kimi-cli/tests/`, `tests/` waiver entry.

## 4. JSON summary

```json
{
  "id": "X01",
  "unit": "library-policy",
  "gate": "G-POLICY",
  "commit": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "scope": ["kimi-cli/src/kimi_cli/tools", "src/kimix/tools"],
  "files_in_scope": 84,
  "command": "uv run tools/gate_library_policy.py kimi-cli/src/kimi_cli/tools src/kimix/tools",
  "result": "FAIL",
  "exit_code": 1,
  "hits": 3,
  "waived": 0,
  "unparsable": 0,
  "findings": [
    {
      "id": "X01-F1",
      "severity": "low",
      "title": "Redundant stdlib json import kept only for a dead except arm",
      "file": "kimi-cli/src/kimi_cli/tools/__init__.py",
      "line": 1,
      "module": "json",
      "replacement": "orjson",
      "uses": ["L99 json.JSONDecodeError"],
      "fix": "delete `import json` (L1); narrow L99 to `except orjson.JSONDecodeError:` (orjson is already imported at L3; its MRO already includes json.decoder.JSONDecodeError)",
      "test_to_add": "kimi-cli/tests/tools/test_extract_key_argument.py: orjson.JSONDecodeError mirror of the existing stdlib-JSONDecodeError patch case + no-stdlib-json policy regression test",
      "test_file_present": true
    },
    {
      "id": "X01-F2",
      "severity": "low",
      "title": "stdlib re used for two Unicode-marker compile patterns in sloppy edit mode",
      "file": "kimi-cli/src/kimi_cli/tools/file/edit/modes/sloppy.py",
      "line": 5,
      "module": "re",
      "replacement": "regex",
      "uses": ["L49 re.compile", "L51 re.compile"],
      "fix": "`import regex as re` (drop-in; L49/L51 unchanged). regex is declared (pyproject.toml:29) and importable (2026.7.19)",
      "test_to_add": "kimi-cli/tests/tools/test_edit_sloppy.py + test_edit_modes_detect.py: re/regex pattern-equivalence over the marker fixtures (incl. `§*` and an inline marker containing `│`)",
      "test_file_present": true
    },
    {
      "id": "X01-F3",
      "severity": "medium",
      "title": "difflib.unified_diff in swarm diff generation; rapidfuzz provides no unified-diff API and is not a declared dependency",
      "file": "src/kimix/tools/swarm/best_of_n.py",
      "line": 23,
      "module": "difflib",
      "replacement": "rapidfuzz",
      "uses": ["L194 difflib.unified_diff", "L212 difflib.unified_diff"],
      "blocker": "runtime probe: `rapidfuzz has unified_diff: False`; rapidfuzz top level = annotations/distance/fuzz/process/utils; pyproject.toml grep shows no rapidfuzz entry",
      "fix": "preferred: module-scoped waiver `src/kimix/tools/swarm/best_of_n.py::difflib` with rationale \"unified_diff has no rapidfuzz counterpart\" + adopt rapidfuzz for candidate self-report similarity in the selector path; alternative: emit the unified diff from rapidfuzz.distance.Editops/Indel pinned to difflib byte-equality, after declaring rapidfuzz in pyproject.toml",
      "test_to_add": "tests/test_best_of_n.py: unified-diff emitter golden-equality test (incl. `a/<rel>`/`b/<rel>` and `/dev/null` untracked case) + keep tests/.../test_parallel_sample_isolation_and_diffs (L48-62) and collect_diff assertion (L176-177) green",
      "test_file_present": true
    }
  ],
  "active_waivers_in_scope": [],
  "proposed_waivers": [
    {
      "entry": "src/kimix/tools/swarm/best_of_n.py::difflib",
      "rationale": "difflib.unified_diff has no rapidfuzz counterpart (rapidfuzz has no unified_diff); rapidfuzz is used for fuzzy candidate scoring instead",
      "status": "not yet filed"
    }
  ],
  "out_of_scope_observations": [
    {
      "what": "supplementary gate run over test trees",
      "command": "uv run tools/gate_library_policy.py kimi-cli/tests tests",
      "result": "FAIL",
      "exit_code": 1,
      "hits": 72,
      "waived": 0,
      "unparsable": 0,
      "classification": "observation (out of scope), not findings",
      "dominant": "json->orjson; also re->regex, datetime->pendulum, base64->pybase64, hashlib->xxhash, difflib->rapidfuzz (tests/test_bash_e2e.py:70)",
      "note": "several stdlib imports are load-bearing test doubles (stdlib json.JSONDecodeError asserted/patched in kimi-cli/tests/core/test_export_cli.py:189-203, kimi-cli/tests/tools/test_extract_key_argument.py:70-75, tests/test_sdk_export.py:231) - do not blanket-swap; scope the gate or add a test-tree waiver"
    },
    {
      "what": "dependency declaration gap",
      "detail": "pyproject.toml grep matches only orjson (L26), regex (L29), msgspec (L30), uvloop (L31), apsw (L32); no rapidfuzz/xxhash/pybase64/pendulum entry, though rapidfuzz 3.14.5 is importable at runtime",
      "classification": "observation, affects fix feasibility for X01-F3"
    },
    {
      "what": "dead import in the same file as X01-F3",
      "file": "src/kimix/tools/swarm/best_of_n.py",
      "line": 147,
      "detail": "`import xxhash  # noqa: F401` unused; not a policy hit (xxhash is the prescribed library)",
      "classification": "observation"
    },
    {
      "what": "dynamic-import evasion scan",
      "detail": "only lazy `__import__(\"gzip\"/\"bz2\"/\"lzma\")` in kimi-cli/src/kimi_cli/tools/file/read_archive.py:265-269; none of these modules is in the policy table",
      "classification": "non-finding"
    },
    {
      "what": "xxhash adoption where hashing is hot",
      "detail": "file/hash_line.py (xxh32 L85), file/micro_compress.py:41 (xxh64), file/snapshot_store.py:32, web/content.py:82 - zero hashlib hits in either in-scope tree",
      "classification": "positive evidence / non-finding"
    },
    {
      "what": ".pyi shadowing check",
      "detail": "no .pyi files found in either in-scope tree",
      "classification": "non-finding"
    }
  ],
  "gate_pass_criteria": [
    "X01-F1 fixed",
    "X01-F2 fixed",
    "X01-F3 closed via module-scoped difflib waiver (or emitter rewrite + rapidfuzz declared in pyproject.toml)",
    "policy regression test added for the three files",
    "re-running the same in-scope command yields EXIT=0"
  ],
  "verified": "gate output (text + --json), allowlist contents, AST usage scan, runtime capability probe, test-pinning search, dependency grep",
  "unverified": "no fix was applied and no test was executed in this unit; rapidfuzz's absence from pyproject.toml is asserted from the recorded grep output only"
}
```
