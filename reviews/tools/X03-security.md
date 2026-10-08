# X03 — security

## 0. Method

Frozen commit: `52192b7c3a1baa66c96e0015d2868f3b106fc043` (evidence-file lines 1–4: `git rev-parse HEAD`
plus `git status --porcelain` = only this sweep's own untracked artifacts, `?? reviews/tools/gates/X01.txt`,
`X02.txt`, `X03.txt`; the worktree is otherwise clean).

Scope: `kimi-cli/src/kimi_cli/tools/**` and `src/kimix/tools/**`, plus the tests that pin them under
`kimi-cli/tests/**` and `tests/**`.

Evidence base: **`reviews/tools/gates/X03.txt`** (1231 lines) is the sole source for this report —
no command was re-run for it and no source outside the raw outputs in that file was read. Commands
recorded there (each preceded by a `=== <command> ===` marker):

1. `git rev-parse HEAD; git status --porcelain` → binds the sweep to the SHA above (L1–4).
2. `find kimix_native` (empty, L6), `ls` of the four test roots (L7–190), a compat-file `find` that
   mostly returned `.venv` noise (L191–240).
3. `native_loader` resolution (L241–244), the installed-shim listing (`site-packages/kimix_native`,
   L245–261), `bin/kimix_native/` (L262–280, L708–726 — `tools.py` 63,261 B, `grep.py` 16,614 B),
   and the full text of `kimi-cli/src/kimi_cli/native_loader.py` (L281–705).
4. Scrub/redaction grep set: `scrub_child_env` (L727–730), the scrub/redact/validate usage sweep
   (L731–844), the `src/` + `kimi-cli/src/` restriction (L845–888), `_bash_subprocess_env`
   (L889–901), `_env_with_rg_bin_path` (L902–913), the `bash_tool.py` module header (L915–1013),
   `ProcessTask(` sites (L1015–1086), `scrub_env` (L1087–1115), `grep -i scrub in kimi-cli/src`
   (**empty**, L1117).
5. Test inventories (`tests/unit/tools` L1118–1139, `tests/*.py` L1140–1204, `tests/native` L1205–1220).
6. **Gate runs** — the two prescribed commands (L1221–1231):
   `uv run pytest tests/unit/tools/test_security.py tests/test_shell_safety.py tests/test_shell_common.py -q`
   → `288 passed in 0.82s`; and
   `uv run pytest kimi-cli/tests/tools/test_url_safety.py tests/native/test_shell_security_equivalence.py -q`
   → `160 passed in 0.40s`. Both exit green; there is no AST-style helper (G-POLICY equivalent) for X03.

Dimensions checked: the nine mandated items **(a)** child-env scrubbing, **(b)** output redaction,
**(c)** path traversal, **(d)** protected paths, **(e)** shell injection, **(f)** ReDoS, **(g)** SSRF,
**(h)** size/resource caps, **(i)** secrets in logs. Each maps to a finding or to an explicit row in §2
that states its verdict — including the six rows where the recorded evidence is insufficient, which are
marked **NOT VERIFIED — evidence gap** rather than silently passed.

Limitations stated up front. (1) Six of the nine dimensions were never probed into the evidence file;
nothing about them is asserted here. (2) The prompt's instruction to *read* `src/kimix/tools/security.py`
and `src/kimix/tools/file/bash/safety.py` end to end was only satisfied by grep line-hits, so both files
are known here only at their hit lines. (3) Because neither gate command included
`tests/test_process_task.py`, `tests/unit/tools/test_python.py`, `tests/test_output_enhance.py`,
`tests/native/test_full_behavior_equivalence.py`, `tests/native/test_src_compat_delegation.py` or
`tests/native/test_mode_matrix.py`, every statement below that cites those files is "test exists in the
inventory", **not** "test ran green". (4) Child-env absence was never demonstrated by the prescribed live
probe (running `env` through a tool); it rests on the unit tests that did run plus the recorded call
sites, and is labelled accordingly.

## 1. Findings

| severity | title (<=80 chars) | file:line | evidence | impact | fix | test_to_add |
|---|---|---|---|---|---|---|
| low | bash/pwsh/Run child processes inherit the full parent env (no scrub_child_env) | src/kimix/tools/common.py:1910 (default); spawn sites src/kimix/tools/file/bash/bash_tool.py:825, :882, :1217; src/kimix/tools/file/bash/pwsh_tool.py:535, :595, :914; src/kimix/tools/file/run.py:424 | `ProcessTask` defaults to `scrub_env: bool = False` (common.py:1910, stored :1924) and only scrubs when asked (`process_env = os.environ.copy()` :1966 → `if self.scrub_env:` :1967 → `process_env = scrub_child_env(process_env)` :1968). None of the eight recorded shell spawn sites passes `scrub_env`, and their env snapshot comes from `_bash_subprocess_env()` (bash_tool.py:147, used :133/:817/:823) or `_env_with_rg_bin_path()` (def common.py:1774, `result = os.environ.copy() if env is None else env.copy()` :1790) — i.e. the full parent env. `grep -i scrub in kimi-cli/src` returns **nothing** (L1117). Contrast the Python tool, which scrubs **by default**: `scrub_on = bool(cfg.get("scrub_env", True)) and not bool(cfg.get("env_passthrough", False))` (src/kimix/tools/py/__init__.py:354, :443) forwarded as `scrub_env=scrub_on` (:360–361, :449–450) and into `_build_env` (:181). Function under test is unit-pinned and green (tests/unit/tools/test_security.py:13–109 inside the 288-passed run); the ProcessTask-level pins tests/test_process_task.py:425 (`test_scrub_env_applied_before_merge`) and :452 (`test_scrub_env_merge_override_wins`) exist but were **not executed** by either recorded gate command. Hypothesis (not proven here): the omission is deliberate so that user shell commands keep their environment; it is not documented as a waiver anywhere in the evidence. | Any credential exported to the agent process (`AWS_*`, `*_API_KEY`, `*_TOKEN`, `*_SECRET`) is visible to every bash/pwsh/Run child and therefore to `env` / `printenv` and to every grandchild script the model runs, and it is what gets redacted out of the *output* post hoc (bash_tool.py:1336, pwsh_tool.py:1018, run.py:492/:724) rather than kept out of the child. The agent's security posture is inconsistent: the same codebase scrubs by default for Python and never for the shells. Exploitation preconditions are that the model is steered into emitting the dump (prompt injection) and that redaction misses the encoding — see the §2 (b) gap. | Pass `scrub_env=True` at the four shell spawn paths (bash bash_tool.py:825/:882/:1217, pwsh pwsh_tool.py:535/:595/:914, run run.py:424) with the same `env_passthrough` opt-out the Python tool already has (py/__init__.py:354), or record a written waiver on `ProcessTask.scrub_env` for the shells. Re-run the two shell suites afterwards. | tests/unit/tools/test_security.py::test_bash_child_env_scrubbed — spawn `env` (`bash -c env`, `pwsh -c Get-ChildItem Env:`, and `run` of a probe executable) with `AWS_SECRET_ACCESS_KEY`/`OPENAI_API_KEY` set in `os.environ` and assert the child output does not contain them; plus a mutation test asserting the caller's `os.environ` dict is unchanged after the call (extend the existing scrub_child_env section at :13–109). |

## 2. Waivers / non-findings

| item | why it is not a finding |
|---|---|
| **(a) child-env scrubbing** — position | Partially clean, gap filed as **X03-F1**. Clean half: `scrub_child_env` is a single shared implementation (src/kimix/tools/security.py:46 def, native branch :64, pure-Python branch :65; shim reference `bin/kimix_native/tools.py:585 _compat_scrub_child_env`, :978 `scrub_child_env`) and its unit section ran green inside the 288-passed run (tests/unit/tools/test_security.py:13–109, incl. `scrub_child_env({}) == {}` :106 and the `AWS_ACCESS_KEY_ID`/`DB_PASSWORD` case :109), so "never mutates the caller's dict" is pinned at unit level. Gap half = the non-Python spawn paths (F1). Method gap: the prompt's live probe ("run env/set through a tool and prove the variable is absent in the child") was never executed — no probe output exists in gates/X03.txt — so child-absence is proven only for the function, not end-to-end through a tool. |
| **(b) output redaction** — JWT / PEM / `Authorization: Bearer` / `password=` / URL userinfo / AWS keys, and bypasses | **Checked, clean at the level the evidence allows.** `redact_sensitive_output` has one owner (security.py:68 def, native :78, compat :79; shim `_compat_redact_sensitive_output` bin/kimix_native/tools.py:558) and is applied at 12 recorded emission sites: bash bash_tool.py:1336, pwsh pwsh_tool.py:1018, run.py:492 and :724, and the shared ProcessTask output paths common.py:2028/2036/2047/2065/2074/2086/2159/2171. Re-export identity is pinned by tests/unit/tools/test_security.py:199–208 (also `validate_workdir`). The full probe matrix is present in tests/test_output_enhance.py:225–304 — JWT :237, PEM :247, `token=ghp_` :252, `github_pat_` :257, `glpat-` :262, `AKIA` :267 (plus the `"AKIA" not in …` assertion at test_security.py:190), `Authorization: Bearer` :272, `x-api-key` :277, URL userinfo :281, `password=` :287 and :292, no-match passthrough :301, empty :304. What actually ran: the smoke subset (tests/unit/tools/test_security.py:166–190) plus the native/pure-Python equivalence suite tests/native/test_shell_security_equivalence.py:48–150, both green (288 + 160 passed). **Documented gap, not claimed clean:** base64/hex-encoded or deliberately split-token bypasses were never probed (the full matrix file was in the inventory but not executed), so "any bypass?" is unanswered for encodings. |
| **(c) path traversal** — read/write/edit/glob/grep/Mkdir/Rm, `..`, symlinks, `\` vs `/`, `C:foo`, UNC; archive members | **NOT VERIFIED — evidence gap.** The only traversal control in the evidence is `validate_workdir` (security.py:82 def, :91 delegating to the shim's `_compat_validate_workdir`; re-exported at file/bash/safety.py:114 and in its `__all__` :55; consumed at run.py:252) and it is clean at unit level: tests/unit/tools/test_security.py:115–159 and tests/test_shell_safety.py:141–187 both ran green inside the 288-passed run. But it is used by Run only — no probe covers read/write/edit/glob/grep/Mkdir/Rm escaping via `..`, symlink, backslash, drive-relative `C:foo` or `\\server`, and there is **no** archive-member probe (`../`, absolute, backslash) for read_archive/grep_archive; tests/unit/tools/test_read_archive.py exists in the inventory but was not run. Backlog: run the traversal/archive matrix into gates/X03.txt before this row can be reclassified. |
| **(d) protected paths** | **NOT VERIFIED — evidence gap.** gates/X03.txt contains no enumeration of the protected-path rules and no assertion that any protected path is blocked with a naming message. This dimension was never instrumented in this sweep. |
| **(e) shell injection** — quoting in shell_common, `Run(shell=False)` metacharacter rejection | **NOT VERIFIED — evidence gap, with one clean sub-result.** `tests/test_shell_common.py` was part of the recorded green run (inside `288 passed`), and the recorded grep hits show `shell_common.py` builds an `(argv, env)` pair for `asyncio.create_subprocess_exec` via `tool._bash_subprocess_env()` (:88, :95; module docstring :4/:8) rather than a shell string — i.e. no `shell=True` shape is visible in the hits. However, quoting was never read end-to-end (the "read safety.py end to end" instruction was not fulfilled) and `Run(shell=False)` rejection of `\|`, `>`, `&&`, `;`, backticks and `$(...)` was never probed. Cannot be called clean. |
| **(f) ReDoS** | **NOT VERIFIED — evidence gap.** No scan of model/user-supplied patterns (`grep -e`, glob patterns, `find_str`, parsers), and no timeout/complexity cap, appears anywhere in gates/X03.txt; no catastrophic pattern was tested. (Context only: the native grep kernel is gated on `runtime_py.grep` and the shim ships `bin/kimix_native/grep.py` at 16,614 B, L274/L720 — that says nothing about pattern safety.) |
| **(g) SSRF** — loopback, link-local, private ranges, metadata IPs, DNS rebinding, redirect re-validation | **Checked, clean only at suite level.** `kimi-cli/tests/tools/test_url_safety.py` is the second recorded command's first target and the pair finished `160 passed in 0.40s`, so the URL-safety rules that exist are green. But `web/url_safety.py` and `web/web_fetcher` never appear in the evidence (no read, no grep), and no probe covers redirect-chain re-validation or DNS rebinding — those specific claims remain unproven. Not worth restating as a finding; recorded as a bounded clean result. |
| **(h) size / resource caps** — response caps, MAX_LINES/MAX_BYTES, zip-bomb, recursive symlink loops | **NOT VERIFIED — evidence gap.** The only cap recorded is `_MAX_INLINE_BASH_ARGV = 6000` in the printed `bash_tool.py` header block (gates/X03.txt:1013), which is an MSYS2 argv-length workaround (its own comment: paths ≥ ~8 KB corrupt `bash -c`), not a response/archive/traversal cap. No MAX_LINES/MAX_BYTES, zip-bomb or symlink-loop evidence; tests/unit/tools/test_read_window.py and test_read_archive.py are in the inventory but were not run. |
| **(i) secrets in logs** — any `logger.*` printing a command line / env / header | **NOT VERIFIED — evidence gap, partially mitigated.** No `logger`/`logging` grep was run, so no positive claim is possible. Mitigation that *is* recorded: every stdout/stderr emission point of the shell tools passes through `redact_sensitive_output` before surfacing (bash_tool.py:1336, pwsh_tool.py:1018, run.py:492/:724, common.py ×8), and the `native_loader.py` body reproduced at L281–705 contains no logging call. Backlog: grep `logger\.` / `logging\.` over both trees and append. |
| Gate rows — the two prescribed pytest commands | Not a security defect: both are green (288 + 160 passed, L1221–1231), so this sweep contributes no red row to §3. There is no AST-style helper for X03, so the pytest pair *is* the gate. |
| **Out-of-scope observation** — `native_loader` trust of writable/env paths (file `kimi-cli/src/kimi_cli/native_loader.py`, outside the declared `tools/**` scope) | Recorded as an observation, not a finding. `_candidate_dirs()` puts `<repo root>/bin` first (:422) and honours `$KIMIX_NATIVE_PATH` as a single-dir override (:419–421) and `$KIMIX_BASE` for the dev fallback (:411–413); `_setup()` inserts those dirs at `sys.path[0]` (:482–483, :494–495) and then does the import (:455, :497), with `_repo_root()` falling back to `os.getcwd()` when the layout is not detectable (:401). The installed wheel is only the *fallback* path (:503–508). So a directory that the in-scope write/edit tools can write into (`bin/kimix_native/`) and env-set paths are trusted ahead of site-packages for the next process start. Hypothesis label: the exploit precondition (a model-controlled write into `<repo>/bin/kimix_native/`, or control of the agent's env/cwd — which already implies code execution) is not demonstrated anywhere in the evidence. File is outside this sweep's scope; route to the loader's owning unit. |
| **Out-of-scope observation** — `get_compat` lacks the private-name guard `get_module` has | `get_module` refuses `name in ("_native",) or name.startswith("_")` (:647) while `get_compat` imports `kimix_native.<name>` with no such guard (:657–670). Purely a hypothesis at this evidence level: no recorded caller passes a model-controlled name to `get_compat` (its only recorded consumers are the literal-name delegations in tests/native/test_src_compat_delegation.py:71–79). Not a finding. |

## 3. Gate result

**PASS on the gate as defined; verdict = gaps (not clean).** The sweep's gate is the prescribed pytest
pair; raw output is the final two sections of `reviews/tools/gates/X03.txt`:

```
uv run pytest tests/unit/tools/test_security.py tests/test_shell_safety.py tests/test_shell_common.py -q
........................................................................  [ 25%]
........................................................................  [ 50%]
........................................................................  [ 75%]
........................................................................  [100%]
288 passed in 0.82s
EXIT=0

uv run pytest kimi-cli/tests/tools/test_url_safety.py tests/native/test_shell_security_equivalence.py -q
........................................................................  [ 45%]
........................................................................  [ 90%]
................                                                          [100%]
160 passed in 0.40s
EXIT=0
```

- No red rows: 448 tests, all green. The scrub/redaction *functions* and the *workdir validator* are
  covered by the first run (tests/unit/tools/test_security.py sections :13–109, :115–159, :166–208;
  tests/test_shell_safety.py:141–187; tests/test_shell_common.py), and the native↔pure-Python security
  equivalence by the second (tests/native/test_shell_security_equivalence.py).
- No G-POLICY-style AST/helper gate is defined for X03; the pytest pair is the only pass/fail helper, so
  a green gate here means "the recorded tests pass", **not** "the tool surface is secure".
- Coverage, counted over the nine mandated dimensions: **2 clean** ((b) redaction at the evidence level,
  (g) SSRF at suite level), **1 with a finding** ((a) → X03-F1), **6 NOT VERIFIED** ((c), (d), (e), (f),
  (h), (i)). Two of the three prescribed command lines were run; the third instruction ("read
  security.py and safety.py end to end") was not, and no live child-env probe exists, so the evidence
  file under-covers the sweep it is supposed to justify.
- Exit criteria for this unit: (1) X03-F1 fixed (scrub on the shell spawn paths or a written waiver),
  (2) the six unverified dimensions each instrumented — traversal/archive matrix (c), protected-path
  enumeration (d), shell-quoting + `Run(shell=False)` rejection (e), ReDoS pattern scan with a cap (f),
  MAX_LINES/MAX_BYTES + zip-bomb + symlink-loop probes (h), and a `logger.*` secrets scan (i) — each
  either fixed or waived in writing, (3) the base64/split-token bypass gap in (b) probed, (4) the
  ProcessTask scrub pins (tests/test_process_task.py:425/:452) and the redaction matrix
  (tests/test_output_enhance.py) added to the gate command so they actually run, and (5) a green re-run
  of the enlarged gate appended to the evidence file. Until then this sweep must not be reported as
  "security clean".

## 4. JSON summary

```json
{
  "id": "X03",
  "sweep": "security",
  "gate": "G7 (prescribed pytest pair; no AST helper defined for X03)",
  "commit": "52192b7c3a1baa66c96e0015d2868f3b106fc043",
  "worktree_clean_at_start": true,
  "scope_dirs": ["kimi-cli/src/kimi_cli/tools", "src/kimix/tools"],
  "tests_in_scope": ["kimi-cli/tests/**", "tests/**"],
  "command": "uv run pytest tests/unit/tools/test_security.py tests/test_shell_safety.py tests/test_shell_common.py -q ; uv run pytest kimi-cli/tests/tools/test_url_safety.py tests/native/test_shell_security_equivalence.py -q",
  "result": "PASS",
  "exit_code": 0,
  "gate_output": "288 passed in 0.82s ; 160 passed in 0.40s (448 tests, 0 failures)",
  "verdict": "gaps",
  "items": {"total": 9, "clean": 2, "with_finding": 1, "unverified": 6},
  "findings": [
    {
      "id": "X03-F1",
      "severity": "low",
      "item": "(a) child-env scrubbing",
      "title": "bash/pwsh/Run child processes inherit the full parent env (no scrub_child_env)",
      "file": "src/kimix/tools/common.py:1910",
      "sites": ["src/kimix/tools/file/bash/bash_tool.py:825", "src/kimix/tools/file/bash/bash_tool.py:882", "src/kimix/tools/file/bash/bash_tool.py:1217", "src/kimix/tools/file/bash/pwsh_tool.py:535", "src/kimix/tools/file/bash/pwsh_tool.py:595", "src/kimix/tools/file/bash/pwsh_tool.py:914", "src/kimix/tools/file/run.py:424"],
      "evidence": "ProcessTask default scrub_env=False (common.py:1910, stored :1924); scrub only when self.scrub_env (os.environ.copy() :1966, if self.scrub_env :1967, scrub_child_env :1968); none of the 8 recorded shell spawn sites passes scrub_env; env snapshot via _bash_subprocess_env (bash_tool.py:147, used :133/:817/:823) or _env_with_rg_bin_path (common.py:1774, os.environ.copy() :1790); grep -i scrub in kimi-cli/src is EMPTY; Python tool scrubs by default (py/__init__.py:354/:360-361, :443/:449-450, :181)",
      "impact": "every *_KEY/*_TOKEN/*_SECRET exported to the agent process reaches every bash/pwsh/Run child and its grandchildren; the posture is inconsistent with the Python tool, which scrubs by default",
      "fix": "pass scrub_env=True at bash_tool.py:825/:882/:1217, pwsh_tool.py:535/:595/:914 and run.py:424 with the existing env_passthrough opt-out (py/__init__.py:354), or file a written waiver on ProcessTask.scrub_env for the shells",
      "test_to_add": "tests/unit/tools/test_security.py::test_bash_child_env_scrubbed (spawn bash -c env / pwsh -c Get-ChildItem Env: / run of a probe with AWS_SECRET_ACCESS_KEY and OPENAI_API_KEY set, assert absent) + caller-dict mutation assertion in the existing :13-109 section",
      "not_proven": "the prescribed live child-env probe was never run; tests/test_process_task.py:425/:452 (the ProcessTask scrub pins) were not part of either gate command"
    }
  ],
  "waivers": [
    {"item": "(b) output redaction (JWT/PEM/Bearer/password/URL-userinfo/AWS)", "verdict": "checked, clean at evidence level", "rationale": "single owner security.py:68/:78/:79 (shim tools.py:558); 12 applied sites (bash_tool.py:1336, pwsh_tool.py:1018, run.py:492/:724, common.py x8 :2028-:2171); full matrix in tests/test_output_enhance.py:225-304; smoke tests test_security.py:166-190 and equivalence tests/native/test_shell_security_equivalence.py:48-150 ran green", "gap": "base64/split-token bypasses never probed; the full matrix file was inventoried but not executed"},
    {"item": "(g) SSRF (loopback/link-local/private/metadata/DNS-rebinding/redirects)", "verdict": "checked, clean at suite level only", "rationale": "kimi-cli/tests/tools/test_url_safety.py is the recorded 160-passed run's first target; web/url_safety.py and web_fetcher never appear in the evidence, so redirect re-validation and DNS-rebinding coverage is unproven"},
    {"item": "(c) path traversal (read/write/edit/glob/grep/Mkdir/Rm; archive members)", "verdict": "NOT VERIFIED - evidence gap", "rationale": "only validate_workdir is evidenced (security.py:82/:91, safety.py:55/:114, used run.py:252) and it is green at unit level (test_security.py:115-159, test_shell_safety.py:141-187 inside the 288-passed run); no .. / symlink / backslash / C:foo / UNC probe and no archive-member probe; tests/unit/tools/test_read_archive.py not run"},
    {"item": "(d) protected paths", "verdict": "NOT VERIFIED - evidence gap", "rationale": "no rule enumeration and no blocked-with-message assertion anywhere in gates/X03.txt"},
    {"item": "(e) shell injection (shell_common quoting; Run(shell=False))", "verdict": "NOT VERIFIED - evidence gap", "rationale": "tests/test_shell_common.py is green inside the 288-passed run and the recorded hits show an (argv, env) pair for create_subprocess_exec (shell_common.py:4/:8/:88/:95), but quoting was not read end-to-end and Run(shell=False) metacharacter rejection (|, >, &&, ;, backticks, $()) was never probed"},
    {"item": "(f) ReDoS", "verdict": "NOT VERIFIED - evidence gap", "rationale": "no pattern/timeout/complexity scan for grep -e, glob, find_str or parsers; no catastrophic pattern tested"},
    {"item": "(h) size / resource caps (response caps, MAX_LINES/MAX_BYTES, zip bomb, symlink loops)", "verdict": "NOT VERIFIED - evidence gap", "rationale": "only recorded constant is _MAX_INLINE_BASH_ARGV = 6000 (gates/X03.txt:1013), a MSYS2 argv-length workaround; test_read_window.py / test_read_archive.py inventoried but not run"},
    {"item": "(i) secrets in logs", "verdict": "NOT VERIFIED - evidence gap, partially mitigated", "rationale": "no logger.*/logging.* scan was run; mitigation on record is that every shell stdout/stderr path passes redact_sensitive_output before surfacing (bash_tool.py:1336, pwsh_tool.py:1018, run.py:492/:724, common.py x8) and native_loader.py (L281-705) contains no logging call"},
    {"item": "gate rows (both prescribed pytest commands)", "verdict": "not a security defect", "rationale": "288 + 160 passed with zero failures; no AST helper exists for X03, so the pytest pair is the only pass/fail helper"}
  ],
  "out_of_scope_observations": [
    {"what": "native_loader trusts a repo-writable dir and env-supplied paths ahead of site-packages", "file": "kimi-cli/src/kimi_cli/native_loader.py", "detail": "_candidate_dirs() puts <repo>/bin first (:422), honours $KIMIX_NATIVE_PATH (:419-421) and $KIMIX_BASE (:411-413); _setup() inserts candidate dirs at sys.path[0] (:482-483, :494-495) then imports (:455, :497); _repo_root() falls back to os.getcwd() (:401); the installed wheel is only the fallback (:503-508)", "classification": "observation (file outside tools/** scope); precondition (model-controlled write into bin/kimix_native, or env/cwd control) is not demonstrated - hypothesis only"},
    {"what": "get_compat has no private-name guard", "file": "kimi-cli/src/kimi_cli/native_loader.py:657-670", "detail": "get_module refuses name.startswith(\"_\") (:647) but get_compat imports kimix_native.<name> unguarded; no recorded model-controlled caller (only literal names in tests/native/test_src_compat_delegation.py:71-79)", "classification": "non-finding / hypothesis"}
  ],
  "evidence_file": "reviews/tools/gates/X03.txt",
  "verified": "the two prescribed gate runs (8 sections of raw output, 448 green tests); scrub_child_env / redact_sensitive_output / validate_workdir existence, ownership, apply sites and unit-level behaviour; the ProcessTask env-scrubbing call sites and their defaults",
  "unverified": "the live child-env probe; (c), (d), (e), (f), (h), (i) in full; the base64/split-token redaction bypass; redirect/DNS-rebinding SSRF; tests/test_process_task.py, tests/unit/tools/test_python.py, tests/test_output_enhance.py, tests/native/test_full_behavior_equivalence.py and tests/native/test_src_compat_delegation.py were inventoried but never executed in this sweep"
}
```
