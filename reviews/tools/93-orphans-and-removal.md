# 93 — Orphans, duplicates and removal decisions (executed)

Evidence: `reviews/tools/00-registry.txt` (G-ORPHAN), `tools/gate_dup.py` (G-DUP),
`gates/X05.txt` (dead-code sweep). **All decisions below are executed.**

## 1. G-ORPHAN result

```
uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools
  # discovered tool classes: 32
  # registered in a manifest: 29
  # allowlisted (rationale recorded): 3
  # ORPHANS: 0
  G-ORPHAN: PASS
```

Before this phase: 37 discovered / 27 registered / 1 allowlisted / **9 orphans**.

## 2. Decisions and evidence

| class | evidence | decision | executed |
|---|---|---|---|
| `kimi_cli.tools.test:Plus` (`plus`) | no tests, no manifest, no docs, no native mirror; one live reference only (a pyinstaller `hiddenimports` string list) | **delete** (plan §4.3 C16 option A) | yes — file removed, list updated |
| `kimi_cli.tools.test:Compare` (`compare`) | as above | **delete** | yes |
| `kimi_cli.tools.test:Panic` (`panic`) | as above | **delete** | yes |
| `kimix.tools.file:Mkdir` | no tests, no manifest, no docs, no native mirror | **delete** | yes — class removed |
| `kimix.tools.file:Rm` | as above (and registering it would grant the model file deletion) | **delete** | yes — class removed |
| `kimix.tools.file.find_str:FindStr` | **mirrored in the native shim** (`bin/kimix_native/tools.py:362`) and imported directly by `tests/native/test_behavior_equivalence.py:110` | **allowlist with rationale** (REVISED - was "register") | yes — `tools/tool_registry_allowlist.txt` |
| `kimix.tools.parser:ParserTool` | imported directly by 6 test sites in `tests/test_parsers.py` | **allowlist with rationale** (REVISED - was "register") | yes — `tools/tool_registry_allowlist.txt` |
| `kimi_cli.tools.file.hash_line:HashRead` | exported by `kimi_cli.tools.file.__all__`, canonical in `kimi_cli/soul/tool_taxonomy.py`, covered by `test_hash_line.py`; opt-in rather than default | **allowlist with rationale** | yes — `tools/tool_registry_allowlist.txt` |
| `kimi_cli.tools.file.hash_line:HashEdit` | as above | **allowlist with rationale** | yes |
| `kimi_cli.tools.context_prune:context_prune` | registered programmatically at `kimisoul.py:490` | allowlist (already) | yes |

Rule applied: **untested + unwired + undocumented -> delete; everything else -> allowlist with a
written rationale.** No tool was deleted that any test, manifest, native mirror or document
referenced.

**Revision (owner decision, post-review):** `FindStr` and `ParserTool` were initially *registered*
in `src/kimix/agent_worker.json`, which is the option the plan's K15/K16 gates name first.  The owner
then directed **no new tool registrations**, so that manifest change was reverted byte-for-byte and
both tools moved to the allowlist instead.  G-ORPHAN's own pass condition (plan §3 G7) accepts
"registered in a manifest **or** listed in `tools/tool_registry_allowlist.txt` with a rationale", so
the gate stays green: 32 discovered / 27 registered / 5 allowlisted / **0 orphans**.

## 3. Duplicate tool names (G-DUP)

| name | implementations | state |
|---|---|---|
| `subagent` | `kimi-cli/.../agent/__init__.py:53` `AgentTool` and `src/kimix/tools/agent/__init__.py:518` `Agent` | **waived** (`tools/gate_dup_allowlist.txt`) — intentionally distinct: `run_in_background` defaults `False` in C01 and `True` in K01, and the plan mandates a contrast probe in both gate blocks |
| `fetch_url` | `kimi-cli/.../web/fetch.py:101` (C12) and `src/kimix/tools/web/fetch_url.py:23` (K14) | resolved by FP-02 (see `90-findings.md`) |

## 4. Dead modules / stale bytecode (FP-06, done)

* 24 stale `__pycache__` trees / 238 `.pyc` under both tool roots were removed;
  they are gitignored so this leaves no diff, and they held bytecode for modules
  deleted from the source tree (notably `src/kimix/tools/eval/`, which has no `.py`
  on disk and no tracked files at all).
* `src/kimix/tools/check_fmt.py` is a deprecation shim whose `DeprecationWarning` is
  still exercised by `tests/test_check_fmt.py`; kept deliberately.
* `kimi_cli/tools/file/replace.py` is a re-export shim whose identity is asserted by
  C10's probe 11; kept deliberately.
* `kimi-cli/src/kosong/tooling/__init__.py:1848-1851` still maps the legacy tool names
  `MakeDir`/`CreateDir`/`CreateDirectory` to `Mkdir`. That is a name-redirect table in
  the `kosong` SDK (outside the reviewed roots); the dead redirect is harmless but is
  recorded here as a cleanup candidate.
