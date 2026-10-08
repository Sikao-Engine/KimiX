# X04 — native-parity

## 0. Method

Frozen commit: `52192b7c3a1baa66c96e0015d2868f3b106fc043` (evidence-file lines 1–6: `git rev-parse HEAD`
plus `git status --porcelain`, whose only entries are this sweep's own artifacts, `?? reviews/tools/gates/X01.txt`
… `X04.txt`; the worktree is otherwise clean).

Scope: the `kimix_native` shim (`bin/kimix_native/**`), its `src/kimix` + `kimi-cli/src` call sites and the
native test trees that pin both (`tests/native/**`, `kimi-cli/tests/native/**`, `kimi-cli/tests/tools/**`).

Evidence base: `reviews/tools/gates/X04.txt` (1048 lines) is the sole source for this report — no command
was re-run for it and no source outside the raw outputs in that file was read. Commands recorded there
(each preceded by a `=== <command> ===` marker):

1. `git rev-parse HEAD`; `git status --porcelain` → binds the sweep to the SHA above (L1–6).
2. Two import-site greps: `grep -rn 'kimix_native' --include=*.py .` (L7–268, excluding `bin/kimix_native`)
   and `grep -rn 'import runtime_py|runtime_py' --include=*.py .` (L8, hits merged into the same block).
3. `git show --stat fe10182b` (empty in the window, L269–270), `git log --oneline -15` (L271–286),
   `git show fe10182b` full stat/message (L287–299, L466–478) and its `grep_local.py` diff (L745–895)
   plus its `tests/native/test_src_compat_delegation.py` diff (L897–922).
4. `find` for `_compat` files (L300, results L343–381), `wc -l bin/kimix_native/*.py` (L301–316),
   `grep -n '^def |^class |…' bin/kimix_native/grep.py` (L318–342).
5. Shipped-copy comparison: `ls .venv/Lib/site-packages | grep -i 'kimix|runtime|pth'` (L445–451),
   the `.pth` contents (L453–465), `diff -r bin/kimix_native .venv/Lib/site-packages/kimix_native`
   (L480–483), per-file `sha256` of both shim dirs (L486–499), `diff -u` of the two divergent files
   …/`__init__.py` (L500–517) and …/`_shell_compat.py` (L519–640), `diff -q tests/native kimi-cli/tests/native`
   (L924–941).
6. Gate runs — the prescribed pytest pair (L642–735):
   the parity module `kimi-cli/tests/native/test_grep_engine_parity.py` → **38 failed, 329 passed,
   1 xfailed, 1 xpassed in 7.90s, EXIT=1**; and `uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py`
   → **524 passed in 8.93s, EXIT=0**. A single-node re-run of `TestEngineMirrorParity::test_roots_mix`
   (L996–1048) → **1 failed in 0.39s, EXIT=1**.
7. Regression check against the hand-off baseline: `grep -c FAILED reviews/tools/00-baseline.txt` → 43,
   every hit at `test_grep_engine_parity.py:428` / `:160` (L942–995).
8. Removal bookkeeping: `git log --oneline -- bin/kimix_native/grep.py` (L736–738, only `cd8c19e9`),
   `git log --oneline -- kimi-cli/tests/native/test_grep_engine_parity.py` (L739–741, only `cd8c19e9`),
   `git status` of `bin/kimix_native/grep.py` → **TRACKED** (L742–744).
9. `find` for accidental `nul`/`NUL` files → `(end nul search)`, no matches (L383–384).

Dimensions checked (a–i): (a) frozen commit / worktree cleanliness, (b) shim inventory on both sides,
(c) repo-shim ↔ installed-copy byte parity, (d) completeness of the native GREP-kernel removal,
(e) parity-suite pass/fail split across the two test trees, (f) root-vs-`kimi-cli` native test-tree drift,
(g) which copy actually wins at import (loader resolution), (h) the version gate / `native_active` toggles,
(i) pure-Python fallback equivalence coverage. Each maps either to a finding in §1 or to an explicit row in §2.

Limitations stated up front. (1) Only raw pytest summaries were recorded: the command header of the
parity run is outside the recorded window (the run's FAILED list and summary are inside it, L642–724), and
`git show --stat fe10182b` produced no lines, so result counts below are quoted from the pytest summary
lines themselves. (2) No live interpreter probe exists in the evidence — there is no
`python -c "import kimix_native; print(kimix_native.__file__)"`, no `runtime_py.version()` and no
`kimix_native.grep.native_active()` call, so the "which copy wins" question is argued from `.pth` files,
shim mtimes and `native_loader` code, **not** demonstrated; it is labelled dimension (g) = unverified.
(3) The mirror-vs-engine failure is quoted only as far as the captured assertion window reaches
("Full output truncated (105 lines hidden)"); the divergence is **not root-caused** here. (4) `tools/sync_native.py`,
`tests/test_install_kimix_native.py`, `tests/test_dist.py` and `tools/bench_native_gate.py` appear in the
import-site grep but were never executed, so every statement about the copy/stage step is code-level.

## 1. Findings

| severity | title | file:line | evidence | impact | fix | test_to_add |
|---|---|---|---|---|---|---|
| high | repo shim vs installed copy diverge: `.venv` `kimix_native` is a stale wheel that also lacks `grep.py` | `.venv/Lib/site-packages/kimix_native/_shell_compat.py:725` (vs `bin/kimix_native/_shell_compat.py:728`); `.venv/Lib/site-packages/kimix_native/__init__.py:18` (vs `bin/kimix_native/__init__.py:21`) | `diff -r bin/kimix_native .venv/Lib/site-packages/kimix_native` → `Files … __init__.py … differ`, `Files … _shell_compat.py … differ`, `Only in bin/kimix_native: grep.py` (L480–483). Per-file sha256 (L486–499): 11 of 13 modules identical (`_common`, `_parse_compat`, `codec`, `diff`, `glob`, `index`, `parse`, `search`, `stream`, `text`, `tools`), divergent are `__init__.py` (repo `1433b45d32c94cfd`, site `aff403dadb307975`) and `_shell_compat.py` (repo `ffd42e3b374d41d7`, site `e0006a383c954dd0`); `diff -u site tools.py vs bin tools.py` is empty, i.e. byte-identical (L484–485). The installed copy is a symlink farm into the uv cache (`archive-v0/4gTj5WQfXt9S_WUZ7PbSk`, L427–443), dated 2026-09-20 11:54:56, while the repo files carry the 2026-10-08 10:25:01 timestamps (L501–502) and site-packages is a normal package (`kimix_native` + `kimix-0.1.29.dist-info`, L445–451) alongside `.pth` entries for `C:\dev\kimi-agent\src` and `…\kimi-cli\src` (L453–458). Repo-side deltas, from the unified diffs: `"builtin"` added to `_COMMAND_WRAPPERS` (repo `_shell_compat.py:728`) and to `_SAME_SHELL_WRAPPERS` (`:757`), `self.nul_fixes.extend(inner.nul_fixes)` propagation (`:1290`, `_watch_command_operand` `:1329-1360` now returns `bool`), `for_arith_header` / `for_brace_body` brace-group loop handling (`:1386-1393`, `:1422`, `:1511-1534`) and the GREP-kernel/version-gate paragraph in the shim docstring (repo `bin/kimix_native/__init__.py:21-27`). `tools/sync_native.py:8` and `dist.py:446-449` show the repo shim is *meant* to be staged into the wheel/bundle, but nothing in the evidence verifies the installed copy was refreshed. | Two installable copies of the same shim are live in one interpreter. Whichever wins changes real behaviour — the stale copy lacks the `builtin` wrapper, the `nul_fixes` propagation and the `for ((...)) { … }` body handling — and it has no `grep.py` peer at all, so an installed-copy winner silently reverts the Oct-8 scanner fixes and changes whether the GREP kernel exists even with `KIMIX_NATIVE_GREP=1`. Because both copies answer to the same import name, an operator cannot tell which one ran, and a "fixed in `bin/`" change can be dead in the venv. | Make the repo shim the single source: re-run `tools/sync_native.py` (or reinstall `kimix-0.1.29`) so the venv copy matches `bin/kimix_native`, add a `--check` mode that fails on drift, and have `native_loader` log/raise when the imported `kimix_native.__file__` is not under `<repo>/bin/kimix_native` in a source checkout. | `tests/test_native_shim_copy_parity.py` — sha256 every `bin/kimix_native/*.py` and assert equality with the importable `kimix_native` package directory (skip with a clear reason when only one copy exists); plus a `tests/native/test_loader.py` case asserting `kimix_native.__file__` resolves under `<repo>/bin/kimix_native` and that a missing `grep.py` in the loaded copy is diagnosed, not silent. |
| high | parity suite is red (38 failed) while the standard native run is green (524 passed) — the green run never collects it | `kimi-cli/tests/native/test_grep_engine_parity.py:160`, `:428`, `:448` | Parity run: **38 failed, 329 passed, 1 xfailed, 1 xpassed in 7.90s, EXIT=1** (L642–724). Failures: 30 × `TestEngineMirrorParity::test_matrix[False|True-{files_with_matches,count_matches,content}-{hit,hi.,h[oi]t,^hit,}]` — both toggle states, so the shim's pure-Python mirror disagrees with the compiled engine — 4 × `test_context_head_limit[0-0-250|1-1-0|2-0-2|0-3-1]`, 1 × `test_roots_mix`; the assertion is `assert mir == eng` (`_run_both`, `:160`) and the captured diff is a **file-list/order mismatch at equal counts** — both sides report `'16 match(es) in 13 file(s)'`/`total_matches: 16` while the mirror emits `…\\.\m_dir\x.py` where the engine does not (L673–683: "Full output truncated (105 lines hidden)"). Single-node re-run reproduces: **1 failed in 0.39s, EXIT=1** (L996–1048). The module exists **only** in `kimi-cli/tests/native` (`diff -q`: `ONLY-CLI test_grep_engine_parity.py`, L941), whereas the prescribed root command `uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py` → **524 passed, EXIT=0** (L725–735). The red pre-dates this sweep: `grep -c FAILED reviews/tools/00-baseline.txt` → 43, all at `test_grep_engine_parity.py:428`/`:160` (L942–995). | The repository's only engine↔mirror parity signal is **green by omission**: the command a CI-style job would run passes 524 tests and returns 0 without ever importing the one module that asserts engine/mirror equality, so a reviewer or agent reading "524 passed" concludes native parity holds while 38 parity assertions fail. The failures cover all three output modes, both native toggles, head/context limits and mixed roots, i.e. the mirrored GREP engine cannot currently be trusted as a behaviour-preserving fallback. Because the baseline already carried 43 FAILED lines, this is an unrepaired, carried-over red — not a regression introduced by the review. | Decide the kernel's fate: (i) finish the removal by deleting `kimi-cli/tests/native/test_grep_engine_parity.py` together with `bin/kimix_native/grep.py` (X04-F3), or (ii) repair the mirror — root-cause the roots canonicalization (`.\`-joined dot/relative roots emitted by the mirror) and fix `_norm`/the mirror — and **relocate the module into the root `tests/native` tree** so the canonical command collects it; re-run both commands to green. | `kimi-cli/tests/native/test_grep_engine_parity.py` (unchanged, once green) as the parity pin, plus a guard test asserting the root and `kimi-cli` native test file sets are equal, so no suite can again live only in the tree the standard command skips; add the parity module to the prescribed command in the gate definition. |
| medium | `remove native module grep` left the kernel and its consumers half-deleted (dead 407-line shim module + orphaned red test) | `bin/kimix_native/grep.py` (407 lines, tracked); `kimi-cli/src/kimi_cli/tools/file/grep_local.py:5`; `.kimix_cache/grep_local_head.py:1138` | `fe10182b "remove native module grep"` touches exactly 6 files (`_rtk_common.py`, `tools/file/grep_local.py` −883 lines, `tests/tools/test_grep.py` +36, new `tests/tools/test_grep_ripgrep_only.py` +226, `scripts/install_git.py`, `tests/native/test_src_compat_delegation.py`, L466–478). The consumer side is genuinely gone: `grep_local.py:5` states the native (`kimix_native`/`runtime_py`) GREP kernel "is intentionally NOT used here", and the diff removes the `native_loader` imports, `_NATIVE_TOOLS`, `_compat_tools`, the `_native_use_native("TOOLS")` fast paths (now plain `_REGEX_NEWLINE_ESCAPE_RE`, `:108-130`), `_normalize_output_lines` and `_display_path_for` (L745–895); `_force_pure` became a `hasattr` guard so `grep_local` is simply skipped (L904–922). But the kernel itself survives: `git log --oneline -- bin/kimix_native/grep.py` shows only `cd8c19e9` and `git status` reports it **TRACKED** (L736–744) — it is still in the tree at 407 lines with its full public surface (`run`, `native_active`, `pattern_supported`, `run`'s mode constants, L318–342) and its parity test is still red (X04-F2). Residual references to the removed integration also remain in a stale cache artifact, `.kimix_cache/grep_local_head.py:1138`, `:1198`, `:3036`. | Dead code with a live import surface: the shim module keeps promising a kernel that no production path reaches, the shim docstring still documents a GREP gate contract (`bin/kimix_native/__init__.py:21-27`) that nothing enforces, and the orphaned test file keeps the tree red. A future reader (or agent) can re-wire the dead module and re-introduce the very integration the commit removed. | Either delete `bin/kimix_native/grep.py` together with `kimi-cli/tests/native/test_grep_engine_parity.py` and drop the GREP paragraph from the shim docstring (a `--no-native-grep` flag in `tools/sync_native.py` keeps the wheel/bundle in step), or re-wire the consumer and document the kernel as supported; delete the stale `.kimix_cache/grep_local_head.py` copy. | `tests/native/test_no_dead_shim_modules.py` — for every tracked `bin/kimix_native/*.py`, assert at least one import site under `src/` or `kimi-cli/src` (or an explicit allowlist entry with a rationale), and keep `kimi-cli/tests/tools/test_grep_ripgrep_only.py` as the pin that `grep_local.py` spawns ripgrep only (no `kimix_native` import). |
| medium | the two native test trees have drifted: 1 file same, 7 differ, 9 exist in only one tree | `tests/native/` vs `kimi-cli/tests/native/` | `diff -q tests/native kimi-cli/tests/native` (L924–941): `SAME __init__.py`; `DIFF conftest.py`, `corpus.py`, `test_behavior_equivalence.py`, `test_compat_missing_dll.py`, `test_loader.py`, `test_mode_matrix.py`, `test_per_kernel_toggle.py`; `ONLY-ROOT test_compress_equivalence.py`, `test_full_behavior_equivalence.py`, `test_shell_security_equivalence.py`, `test_src_compat_delegation.py`, `test_stream_printing_equivalence.py`, `test_version_gate.py`; `ONLY-CLI test_additional_kernels_equivalence.py`, `test_diff_glob_tools_equivalence.py`, `test_grep_engine_parity.py`. | "The native suite" means different things depending on which path a command names: the shared-name files (`conftest.py`, `corpus.py`, `test_loader.py`, the equivalence and per-kernel toggle matrices) have silently diverged, so a fix or pin applied to one copy does not protect the other, and dimensions (e)/(g)/(h) are only testable in one tree each. This duplication is the mechanism that made X04-F2 possible. | Pick one canonical tree — the root `tests/native` looks canonical (it holds the version-gate, compat-delegation, stream/shell equivalence and compress/full-behaviour suites) — delete the duplicated copies or reduce each shared filename to a thin re-export of the canonical one, and record the choice. | A CI guard test comparing the two directories' file sets and hashing the shared names (`tests/native/test_tree_consistency.py`), failing with the exact `DIFF`/`ONLY-*` listing. |

## 2. Waivers / non-findings

| item | why it is not a finding |
|---|---|
| (a) frozen commit / clean worktree | `git rev-parse HEAD` = `52192b7c3a1baa66c96e0015d2868f3b106fc043` and `git status --porcelain` lists only this sweep's own untracked gate logs (`?? reviews/tools/gates/X01.txt` … `X04.txt`); no source file is modified (L1–6). |
| accidental `nul` / `NUL` file (AGENTS.md rule) | `find` for `nul`/`NUL` returns `(end nul search)` with no matches (L383–384). The `nul.dat` path inside the parity fixture (`test_grep_engine_parity.py`, roots list `…/nul.dat`, L1014–1016) is a deliberate test-file name in a temp tree, not a redirect artifact. |
| byte-identical shim modules (11/13) and identical `tools.py` | `sha256` over both dirs gives equal digests for `_common`, `_parse_compat`, `codec`, `diff`, `glob`, `index`, `parse`, `search`, `stream`, `text`, `tools`, and `diff -u` of `tools.py` is empty (L484–499) — the divergence is confined to the two files plus the missing `grep.py` recorded in X04-F1, so the byte-identical majority is clean and needs no re-verification. |
| the root native run is green | `uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py` → 524 passed in 8.93s, EXIT=0 (L725–735): the shared equivalence/toggle/version-gate suites pass. This is explicitly **not** evidence for engine↔mirror parity — that module is not in this tree (X04-F2) — and it is not evidence for the drift questions (X04-F1/F4). |
| `hash_line`'s per-line native kernel is unused (`kimi-cli/src/kimi_cli/tools/file/hash_line.py:79`) | The in-source note says the per-line kernel is not used and the module routes through the bulk native fast path `kimix_native.tools.compute_line_hashes` (`:92`); the alternative path is documented in the file and no red test tracks it, so this is a documented micro-optimisation gap, not a parity defect. |
| the pure-Python mirrors are a *hard* dependency, not the optional native path | `kimi-cli/src/kimi_cli/native_loader.py:5` calls the compiled extension/shkim optional, and that is true of `runtime_py`; but `src/kimix/parser/*.py:11-32` (py, c, html, lisp, pascal, shell, sql) and `src/kimix/tools/file/bash/{bash_fix,pwsh_fix,process_pwsh,bash_tool}.py:53-76` require `bin/kimix_native/_parse_compat.py` (2716 lines) / `_shell_compat.py` (4577 lines) to be importable. That is by design ("exactly one copy", per those docstrings) — but it makes X04-F1 load-bearing: when the stale installed copy wins, these parsers/scanners silently run the September code. Recorded here as an impact note, not scored twice. |
| `native_loader` resolves env/repo paths ahead of site-packages | Already recorded as an out-of-scope observation by the X03 sweep (`_candidate_dirs()` puts `<repo>/bin` first, honours `$KIMIX_NATIVE_PATH`/`$KIMIX_BASE`, inserts at `sys.path[0]`). X04 cites it only as the reason the two shim copies can both be live; it is not re-scored here, and no model-controlled write into `bin/kimix_native` is demonstrated in the X04 evidence. |
| dimension (g) — which copy actually wins at import | NOT VERIFIED — evidence gap: no `import kimix_native; print(__file__)` probe was run. The `.pth` files, mtimes and `native_loader` source are consistent with *either* copy winning, so X04-F1 asserts a divergence hazard, not the live resolution. The proposed F1 test closes this gap. |
| dimension (h) — the live version gate / `native_active()` toggles | NOT VERIFIED — evidence gap: the gate logic is recorded only as code and docstring (`bin/kimix_native/__init__.py:21-27`, `:66-96`; `grep.py:111-127` `_native_grep`/`native_active`), and no `runtime_py.version()` value or `KIMIX_NATIVE_VERSION` marker content appears anywhere in the gate file, so neither "gate engages" nor "gate rejects a stale build" is demonstrated. The `test_version_gate.py` / `test_per_kernel_toggle.py` / `test_mode_matrix.py` files are inventoried (root, green in the 524) but their assertions are not quoted. |

## 3. Gate result

**FAIL — the X04 gate is red.** The sweep's gate is the prescribed native pytest pair; raw output is in
`reviews/tools/gates/X04.txt`:

```
uv run pytest <kimi-cli/tests/native/test_grep_engine_parity.py> …
38 failed, 329 passed, 1 xfailed, 1 xpassed in 7.90s
EXIT=1

uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py
........................................................................  [ 13%]
… (524 dots) …
524 passed in 8.93s
EXIT=0

uv run pytest 'kimi-cli/tests/native/test_grep_engine_parity.py::TestEngineMirrorParity::test_roots_mix' -q -vv
1 failed in 0.39s
EXIT=1

grep -c FAILED reviews/tools/00-baseline.txt   # hand-off baseline, pre-sweep
43
```

- **The split is the finding, not the two numbers.** Run A (root `tests/native`) is fully green; Run B (the
  engine↔mirror parity module) is 38-red; and Run A cannot see Run B, because
  `test_grep_engine_parity.py` exists **only** in `kimi-cli/tests/native` (`diff -q`: `ONLY-CLI`). A green
  gate here therefore means "the recorded tests that were collected pass", **not** "the native mirrors are
  at parity with the compiled engine".
- **Pre-existing, not self-inflicted.** The hand-off baseline already carries 43 `FAILED` lines, all at
  `test_grep_engine_parity.py:428`/`:160`, so the red was inherited by this review; nothing in the X04
  evidence shows the sweep caused it. The 43-vs-38 count difference is consistent with a differently
  selected baseline run, but the baseline command is not recorded, so the delta is unexplained.
- **Coverage, counted over the nine dimensions (a)–(i): 3 clean** ((a) commit/worktree, (b) shim inventory,
  (i) fallback-equivalence run)**, 4 with findings** ((c) → X04-F1, (d) → X04-F3, (e) → X04-F2, (f) → X04-F4)**,
  2 NOT VERIFIED** ((g) live copy resolution, (h) live version gate). No dimension is asserted clean
  without an explicit row above.
- **Exit criteria for this unit:** (1) X04-F1 closed — one shim copy or a `sync_native --check` guard that
  makes drift loud, plus a `kimix_native.__file__` assertion; (2) the GREP kernel's fate decided and the
  tree made consistent with it (delete `bin/kimix_native/grep.py` + its parity module and the shim's GREP
  docstring paragraph, or repair the mirror and **move the parity module into the root tree**); (3) the
  parity module (or its replacement) collected by the canonical command so `524 passed` can no longer mean
  "parity not measured"; (4) X04-F4's tree-drift guard in place; (5) the two unverified dimensions
  instrumented — a probe printing `kimix_native.__file__`, `runtime_py.version()` and
  `kimix_native.grep.native_active()` — and (6) both prescribed commands re-run green.

## 4. JSON summary

```json
{
  "id": "X04",
  "sweep": "native-parity",
  "gate": "G4 (prescribed native pytest pair: kimi-cli parity module + `uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py`)",
  "commit": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "worktree_clean_at_start": true,
  "scope_dirs": ["bin/kimix_native", "src/kimix", "kimi-cli/src/kimi_cli/tools", "tests/native", "kimi-cli/tests/native", "kimi-cli/tests/tools"],
  "tests_in_scope": ["tests/native/**", "kimi-cli/tests/native/**", "kimi-cli/tests/tools/test_grep*.py"],
  "command": "uv run pytest <kimi-cli/tests/native/test_grep_engine_parity.py> -q ; uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py ; uv run pytest 'kimi-cli/tests/native/test_grep_engine_parity.py::TestEngineMirrorParity::test_roots_mix' -q -vv",
  "result": "FAIL",
  "exit_code": 1,
  "gate_output": "38 failed, 329 passed, 1 xfailed, 1 xpassed in 7.90s (EXIT=1) ; 524 passed in 8.93s (EXIT=0) ; single-node repro: 1 failed in 0.39s (EXIT=1) ; pre-sweep baseline reviews/tools/00-baseline.txt: 43 FAILED, all at test_grep_engine_parity.py:428/:160",
  "verdict": "fail",
  "items": {"total": 9, "clean": 3, "with_finding": 4, "unverified": 2},
  "findings": [
    {
      "id": "X04-F1",
      "severity": "high",
      "item": "(b)/(c) shim inventory + repo-shim vs installed-copy parity",
      "title": "repo shim vs installed copy diverge: the .venv kimix_native wheel is stale and lacks grep.py",
      "file": ".venv/Lib/site-packages/kimix_native/_shell_compat.py:725 (vs bin/kimix_native/_shell_compat.py:728); .venv/Lib/site-packages/kimix_native/__init__.py:18 (vs bin/kimix_native/__init__.py:21)",
      "evidence": "diff -r bin/kimix_native .venv/Lib/site-packages/kimix_native -> '__init__.py differ', '_shell_compat.py differ', 'Only in bin/kimix_native: grep.py' (gates/X04.txt L480-483). sha256 of both dirs (L486-499): 11/13 identical (_common, _parse_compat, codec, diff, glob, index, parse, search, stream, text, tools); divergent __init__.py repo=1433b45d32c94cfd site=aff403dadb307975 and _shell_compat.py repo=ffd42e3b374d41d7 site=e0006a383c954dd0; `diff -u site tools.py vs bin tools.py` empty (L484-485). Installed copy = symlinks into uv cache archive-v0/4gTj5WQfXt9S_WUZ7PbSk (L427-443), mtime 2026-09-20 11:54:56 vs repo 2026-10-08 10:25:01 (L501-502); site-packages ships the package plus kimix-0.1.29.dist-info (L445-451) while .pth files add C:\\dev\\kimi-agent\\src and kimi-cli\\src to sys.path (L453-458). Repo-side deltas: 'builtin' added to _COMMAND_WRAPPERS (repo _shell_compat.py:728) and _SAME_SHELL_WRAPPERS (:757); self.nul_fixes.extend(inner.nul_fixes) propagation (:1290); _watch_command_operand now returns bool (:1329-1360); for_arith_header/for_brace_body brace-group loop handling (:1386-1393, :1422, :1511-1534); GREP-kernel/version-gate paragraph in the shim docstring (repo __init__.py:21-27). tools/sync_native.py:8 and dist.py:446-449 stage the repo shim into the wheel/bundle, and no recorded command verifies the installed copy was refreshed.",
      "impact": "two installable copies of one shim are importable in the same interpreter; whichever wins changes bash/pwsh scanner behaviour (missing 'builtin' wrapper, missing nul_fixes propagation, missing `for ((...)) { }` body handling) and whether a grep.py kernel exists at all. A fix applied in bin/kimix_native can be dead in the venv, and the stale copy also silently backs the parsers/security scanners (src/kimix/parser/*.py:11-32, src/kimix/tools/file/bash/*:53-76) that require _parse_compat/_shell_compat to be importable.",
      "fix": "re-run tools/sync_native.py (or reinstall kimix-0.1.29) so the venv copy matches bin/kimix_native; add a --check mode that fails on drift; make native_loader log/raise when the imported kimix_native.__file__ is not under <repo>/bin/kimix_native in a source checkout.",
      "test_to_add": "tests/test_native_shim_copy_parity.py - sha256 every bin/kimix_native/*.py and assert equality with the importable kimix_native package dir (skip with reason when only one copy exists); plus a tests/native/test_loader.py case asserting kimix_native.__file__ resolves under <repo>/bin/kimix_native and that a loaded copy missing grep.py is diagnosed rather than silent.",
      "not_proven": "no live `import kimix_native; print(__file__)` probe was recorded, so which copy wins is inferred from .pth files, mtimes and loader source (dimension (g) = unverified)"
    },
    {
      "id": "X04-F2",
      "severity": "high",
      "item": "(e) parity-suite pass/fail split",
      "title": "parity suite is red (38 failed) while the standard native run is green (524 passed) and never collects it",
      "file": "kimi-cli/tests/native/test_grep_engine_parity.py:160 (assert mir == eng), :428, :448",
      "evidence": "parity run: 38 failed, 329 passed, 1 xfailed, 1 xpassed in 7.90s, EXIT=1 (L642-724). Failures: 30 x test_matrix[False|True-{files_with_matches,count_matches,content}-{hit,hi.,h[oi]t,^hit,}] (both native toggle states), 4 x test_context_head_limit[0-0-250|1-1-0|2-0-2|0-3-1], 1 x test_roots_mix; assertion `assert mir == eng` via _run_both (:160, _run_both def :160 area, call :428, test_roots_mix :448). The captured diff is a file-list/order mismatch at equal counts: both sides report '16 match(es) in 13 file(s)' / total_matches 16 while the mirror emits ...\\.\\m_dir\\x.py where the engine does not (L673-683; 'Full output truncated (105 lines hidden)'). Single-node re-run: 1 failed in 0.39s, EXIT=1 (L996-1048). The module exists only in kimi-cli/tests/native (`diff -q` -> 'ONLY-CLI test_grep_engine_parity.py', L941), whereas `uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py` is 524 passed, EXIT=0 (L725-735). Pre-existing red: reviews/tools/00-baseline.txt has 43 FAILED lines, all at test_grep_engine_parity.py:428/:160 (L942-995).",
      "impact": "the only engine-vs-mirror parity signal is green by omission - the command a CI-style job runs returns 0 without importing the module that asserts equivalence, so '524 passed' is read as 'native parity holds' while 38 parity assertions fail across all three output modes, both toggle states, context/head limits and mixed roots. The mirrored GREP engine therefore cannot be trusted as a behaviour-preserving fallback, and the red was inherited from the hand-off baseline rather than introduced by this sweep.",
      "fix": "decide the kernel's fate: (i) delete kimi-cli/tests/native/test_grep_engine_parity.py together with bin/kimix_native/grep.py (X04-F3), or (ii) repair the mirror by root-causing the roots canonicalization (dot/relative roots keep the '.\\' join) and fixing _norm/the mirror, then MOVE the module into the root tests/native tree so the canonical command collects it; re-run both commands to green.",
      "test_to_add": "keep kimi-cli/tests/native/test_grep_engine_parity.py (or its relocated copy) as the parity pin once green, plus a guard test asserting the root and kimi-cli native test file sets are equal so no suite can live only in the tree the standard command skips; add the parity module to the prescribed gate command.",
      "not_proven": "the divergence is not root-caused - the pytest window is truncated ('105 lines hidden'); the 43-vs-38 baseline delta is unexplained because the baseline command is not recorded"
    },
    {
      "id": "X04-F3",
      "severity": "medium",
      "item": "(d) completeness of the native GREP-kernel removal",
      "title": "`remove native module grep` left the kernel and its consumers half-deleted (dead 407-line shim module + orphaned red test)",
      "file": "bin/kimix_native/grep.py (407 lines, TRACKED); kimi-cli/src/kimi_cli/tools/file/grep_local.py:5; .kimix_cache/grep_local_head.py:1138",
      "evidence": "fe10182b touches exactly 6 files (L466-478): _rtk_common.py, kimi-cli/src/kimi_cli/tools/file/grep_local.py (-883 lines), kimi-cli/tests/tools/test_grep.py (+36), new kimi-cli/tests/tools/test_grep_ripgrep_only.py (+226), scripts/install_git.py, tests/native/test_src_compat_delegation.py. Consumer removal is real: grep_local.py:5 states the native (kimix_native/runtime_py) GREP kernel 'is intentionally NOT used here' and the diff removes the native_loader imports, _NATIVE_TOOLS, _compat_tools and the _native_use_native('TOOLS') fast paths, replacing them with _REGEX_NEWLINE_ESCAPE_RE (L745-895); _force_pure became a hasattr guard so grep_local is skipped (L904-922). But the kernel survives: `git log --oneline -- bin/kimix_native/grep.py` shows only cd8c19e9 and `git status` reports the file TRACKED (L736-744); wc -l gives 407 lines and the grep of its top-level defs shows the full public surface run/native_active/pattern_supported plus the _MODES constants (L318-342). Residual references to the removed integration remain in the stale cache artifact .kimix_cache/grep_local_head.py:1138/:1198/:3036.",
      "impact": "dead code with a live import surface: the shim keeps exporting a kernel no production path reaches, the shim docstring still documents a GREP gate contract (bin/kimix_native/__init__.py:21-27) that nothing enforces, and the orphaned parity test keeps the tree red. A future change can re-wire the dead module and re-introduce the removed integration, and the wheel/bundle (which has no grep.py) and the repo (which has one) describe different kernel sets - the same asymmetry that drives X04-F1.",
      "fix": "either delete bin/kimix_native/grep.py together with kimi-cli/tests/native/test_grep_engine_parity.py and drop the GREP paragraph from the shim docstring (with a tools/sync_native.py flag keeping the wheel/bundle in step), or re-wire the consumer and document the kernel as supported; delete the stale .kimix_cache/grep_local_head.py copy.",
      "test_to_add": "tests/native/test_no_dead_shim_modules.py - for every tracked bin/kimix_native/*.py assert at least one import site under src/ or kimi-cli/src (or an explicit allowlist entry with a rationale); keep kimi-cli/tests/tools/test_grep_ripgrep_only.py as the pin that grep_local.py spawns ripgrep only and imports no kimix_native module.",
      "not_proven": "no command in the evidence re-wires or executes bin/kimix_native/grep.py, so its post-removal behaviour is asserted only from its source surface"
    },
    {
      "id": "X04-F4",
      "severity": "medium",
      "item": "(f) root-vs-kimi-cli native test-tree drift",
      "title": "the two native test trees have drifted: 1 file same, 7 differ, 9 exist in only one tree",
      "file": "tests/native/ vs kimi-cli/tests/native/",
      "evidence": "`diff -q tests/native kimi-cli/tests/native` (L924-941): SAME __init__.py; DIFF conftest.py, corpus.py, test_behavior_equivalence.py, test_compat_missing_dll.py, test_loader.py, test_mode_matrix.py, test_per_kernel_toggle.py; ONLY-ROOT test_compress_equivalence.py, test_full_behavior_equivalence.py, test_shell_security_equivalence.py, test_src_compat_delegation.py, test_stream_printing_equivalence.py, test_version_gate.py; ONLY-CLI test_additional_kernels_equivalence.py, test_diff_glob_tools_equivalence.py, test_grep_engine_parity.py.",
      "impact": "'the native suite' means different things depending on which path a command names; the shared-name files (conftest.py, corpus.py, test_loader.py, the equivalence and per-kernel toggle matrices) have silently diverged, so a fix or pin applied to one copy does not protect the other, and dimensions (e)/(g)/(h) are each only testable in one tree. This duplication is the mechanism that let X04-F2 hide: the failing module lives only in the tree the canonical command skips.",
      "fix": "pick one canonical tree - the root tests/native looks canonical (it holds test_version_gate.py, test_src_compat_delegation.py, test_stream_printing_equivalence.py, test_shell_security_equivalence.py, test_compress_equivalence.py, test_full_behavior_equivalence.py) - delete the duplicated copies or reduce each shared filename to a thin re-export, and record the choice.",
      "test_to_add": "tests/native/test_tree_consistency.py - compare the two directories' file sets and hash the shared names, failing with the exact DIFF/ONLY-* listing.",
      "not_proven": "no per-file review of the 7 DIFF files was recorded, so the semantic size of each drift is unknown"
    }
  ],
  "waivers": [
    {"item": "(a) frozen commit / clean worktree", "verdict": "clean", "rationale": "git rev-parse HEAD = 52192b7c3a1baa66c96e0015d2868f3b106fc043; git status --porcelain lists only this sweep's untracked gate logs (?? reviews/tools/gates/X01.txt ... X04.txt); no source file modified (L1-6)"},
    {"item": "accidental nul/NUL file (AGENTS.md rule)", "verdict": "clean", "rationale": "the find for nul/NUL returns '(end nul search)' with no matches (L383-384); the nul.dat entry in the parity fixture roots list (test_grep_engine_parity.py, L1014-1016) is a deliberate temp-tree fixture name, not a redirect artifact"},
    {"item": "byte-identical shim modules (11/13) and identical tools.py", "verdict": "clean", "rationale": "sha256 over both shim dirs equal for _common, _parse_compat, codec, diff, glob, index, parse, search, stream, text, tools; diff -u of tools.py empty (L484-499) - divergence confined to the two files plus the missing grep.py recorded in X04-F1"},
    {"item": "the root native run (524 passed)", "verdict": "green but not a parity verdict", "rationale": "uv run pytest tests/native -q --ignore=tests/test_bash_e2e.py -> 524 passed in 8.93s, EXIT=0 (L725-735); it never imports kimi_native.grep, so it is not evidence for engine/mirror parity (X04-F2) nor for the drift questions (X04-F1/F4)"},
    {"item": "hash_line per-line native kernel unused", "verdict": "non-finding", "rationale": "kimi-cli/src/kimi_cli/tools/file/hash_line.py:79 records that the per-line kernel is not used and :92 routes through the bulk native path kimix_native.tools.compute_line_hashes; documented in-source, no red test tracks it - a micro-optimisation gap, not a parity defect"},
    {"item": "the pure-Python mirrors are a hard dependency, not the optional native path", "verdict": "non-finding, but load-bearing for X04-F1", "rationale": "native_loader.py:5 calls the compiled runtime_py extension optional, which is true; however src/kimix/parser/*.py:11-32 (py, c, html, lisp, pascal, shell, sql) and src/kimix/tools/file/bash/{bash_fix,pwsh_fix,process_pwsh,bash_tool}.py:53-76 require bin/kimix_native/_parse_compat.py (2716 lines, L304) / _shell_compat.py (4577 lines, L305) to be importable, by design ('exactly one copy'). Not a defect, but it means the stale installed copy (X04-F1) silently backs the parsers and scanners"},
    {"item": "native_loader resolves env/repo paths ahead of site-packages", "verdict": "cross-reference, not re-scored", "rationale": "already recorded by X03 as an out-of-scope observation (<repo>/bin first, $KIMIX_NATIVE_PATH/$KIMIX_BASE honoured, sys.path[0] insertion); X04 cites it only as the reason two shim copies can both be live, and no model-controlled write into bin/kimix_native is demonstrated in the X04 evidence"},
    {"item": "dimension (g) - which copy wins at import", "verdict": "NOT VERIFIED - evidence gap", "rationale": "no `import kimix_native; print(__file__)` probe was run; .pth files, mtimes and native_loader source are consistent with either copy winning, so X04-F1 asserts a divergence hazard rather than the live resolution"},
    {"item": "dimension (h) - live version gate / native_active() toggles", "verdict": "NOT VERIFIED - evidence gap", "rationale": "the gate is recorded only as code and docstring (bin/kimix_native/__init__.py:21-27, :66-96; grep.py:105-127 _native_grep/native_active); no runtime_py.version() value and no KIMIX_NATIVE_VERSION marker content appear in the gate file, so neither 'gate engages' nor 'gate rejects a stale build' is demonstrated; tests/native/test_version_gate.py, test_per_kernel_toggle.py and test_mode_matrix.py are inventoried (root, green inside the 524) but their assertions are not quoted"}
  ],
  "out_of_scope_observations": [
    {"what": "stale cached copy of the pre-removal grep_local.py", "file": ".kimix_cache/grep_local_head.py:1138, :1198, :3036", "detail": "the cache artifact still contains the native GREP integration comments ('# Native GREP kernel (kimix_native.grep over runtime_py.grep)', 'native_active() is consulted on every ...', 'Native line scan (kimix_native.tools.scan_lines_cb)') that fe10182b deleted from kimi-cli/src/kimi_cli/tools/file/grep_local.py", "classification": "process/cache hygiene observation (path outside tools/** and outside the shipped package); folded into X04-F3 as removal residue"},
    {"what": "the parity test module is a test-tree artifact, not a tools/** file", "file": "kimi-cli/tests/native/test_grep_engine_parity.py:3-6, :60-62, :515, :533-537", "detail": "it documents itself as the authoritative contract re-verified live against runtime_py 1.3.0, gates on the compiled kernel being active (:72), xfails on the unknown runtime_py regex_lite stack-overflow bug (:515) and shells out with sys.path.insert(0, <shim bin>); import sys; import runtime_py; runtime_py.grep.run(...) (:533-537) - i.e. the suite depends on a live compiled extension that no recorded command verifies is installed", "classification": "observation - the runtime_py installation itself (version, presence of the optional .grep submodule) was never probed in this sweep"}
  ],
  "evidence_file": "reviews/tools/gates/X04.txt",
  "verified": "the frozen commit and worktree state; the shim inventory on both sides (13 modules / 12,945 LOC in bin/kimix_native, incl. grep.py at 407 lines); byte-level sha256 comparison of all 13 modules plus the unified diffs of the two divergent files and the identity of tools.py; the TRACKED state and single-commit history of bin/kimix_native/grep.py; the full fe10182b file list and its grep_local.py / test_src_compat_delegation.py diffs; both prescribed pytest runs (524 green in tests/native, 38 red in the kimi-cli parity module) and the single-node reproduction; the 43 pre-existing FAILED lines in reviews/tools/00-baseline.txt; the root-vs-cli test-tree diff (1 SAME / 7 DIFF / 6 ONLY-ROOT / 3 ONLY-CLI); the absence of accidental nul/NUL files",
  "unverified": "which shim copy wins at import (2 dimensions: (g) live resolution, (h) live version gate) - no interpreter probe, no runtime_py.version(), no KIMIX_NATIVE_VERSION marker content, no kimix_native.grep.native_active() call; the root cause of the 38 mirror-vs-engine failures (the pytest window is truncated: 'Full output truncated (105 lines hidden)') and the 43-vs-38 baseline delta (baseline command unrecorded); the per-file semantics of the 7 DIFF native test files; the stage/sync path (tools/sync_native.py, tests/test_install_kimix_native.py, tests/test_dist.py) and tools/bench_native_gate.py - present in the import-site grep but never executed; tests/native/test_version_gate.py, test_per_kernel_toggle.py, test_mode_matrix.py, test_src_compat_delegation.py, test_full_behavior_equivalence.py and kimi-cli/tests/tools/test_grep_ripgrep_only.py were inventoried (and some ran inside the 524) but no assertion text from them is quoted in the evidence file"
}
```
