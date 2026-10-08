# 93 — Orphans, duplicates and removal decisions

Evidence: `reviews/tools/00-registry.txt` (G-ORPHAN), `tools/gate_dup.py` (G-DUP),
`gates/X05.txt` (dead-code sweep).

## 1. Orphan tool classes (G-ORPHAN)

`uv run tools/review_tool_registry.py --sources kimi-cli/src/kimi_cli/tools src/kimix/tools`
reports **37 discovered tool classes · 27 registered · 1 allowlisted · 9 ORPHAN**.

| orphan | file | registered anywhere? | instantiated anywhere? | decision |
|---|---|---|---|---|
| `kimi_cli.tools.file.hash_line:HashRead` | `kimi-cli/src/kimi_cli/tools/file/hash_line.py:677` | no | no | **register** (plan C15 treats HashRead/HashEdit as builtin; needs a manifest entry or an explicit delete) |
| `kimi_cli.tools.file.hash_line:HashEdit` | `kimi-cli/src/kimi_cli/tools/file/hash_line.py:872` | no | no | **register** (same) |
| `kimi_cli.tools.test:Plus` (`plus`) | `kimi-cli/src/kimi_cli/tools/test.py:13` | no | no | **delete** (plan C16 decision gate) |
| `kimi_cli.tools.test:Compare` (`compare`) | `kimi-cli/src/kimi_cli/tools/test.py:28` | no | no | **delete** |
| `kimi_cli.tools.test:Panic` (`panic`) | `kimi-cli/src/kimi_cli/tools/test.py:47` | no | no | **delete** |
| `kimix.tools.file:Mkdir` | `src/kimix/tools/file/__init__.py:13` | no | no | **delete or register** (plan K17 decision gate) |
| `kimix.tools.file:Rm` | `src/kimix/tools/file/__init__.py:38` | no | no | **delete or register** (plan K18 decision gate) |
| `kimix.tools.file.find_str:FindStr` | `src/kimix/tools/file/find_str.py:27` | no | no | **delete or register** (plan K16 decision gate) |
| `kimix.tools.parser:ParserTool` | `src/kimix/tools/parser/__init__.py:105` | no | no | **delete or register** (plan K15 decision gate) |

Assertion method: for each class the review ran
`grep -rn "ClassName("` and `grep -rn "module"` across `kimi-cli/src`, `src`,
`kimi-cli/tests`, `tests` (raw output in `gates/X05.txt`). No instantiation site
exists for any of the nine; no manifest references any of them.

**Allowlisted (not an orphan):**

| class | rationale |
|---|---|
| `kimi_cli.tools.context_prune:context_prune` | registered programmatically at `kimi-cli/src/kimi_cli/soul/kimisoul.py:490` (`agent.toolset.add(context_prune(self))`), so no manifest entry exists. Waived in `tools/tool_registry_allowlist.txt`. |

## 2. Duplicate tool names (G-DUP)

`uv run tools/gate_dup.py` reports **2 duplicated names** out of 35:

| name | implementations | decision |
|---|---|---|
| `fetch_url` | `kimi_cli/src/kimi_cli/tools/web/fetch.py:101` (C12) and `src/kimix/tools/web/fetch_url.py:23` (K14) | **reconcile** — FP-02. Divergent duplicate; must share one core or forward exactly with a parity test. |
| `subagent` | `kimi-cli/src/kimi_cli/tools/agent/__init__.py:53` `AgentTool` and `src/kimix/tools/agent/__init__.py:518` `Agent` | **waived** — intentionally distinct (`run_in_background` default `False` vs `True`); the plan mandates a contrast probe in both gate blocks. |

## 3. Dead modules / stale bytecode (FP-06)

* Stale `__pycache__` trees exist under both roots; `.pyc` files remain for
  modules whose `.py` source is gone (notably `src/kimix/tools/eval/`, which has
  no `.py` on disk and no tracked files at all). Evidence: `gates/X05.txt`.
* `src/kimix/tools/check_fmt.py` is a deprecation shim that emits a
  `DeprecationWarning` on import; `tests/test_check_fmt.py` still imports it, so
  the warning is load-bearing in the baseline output
  (`reviews/tools/00-baseline.txt:28`).
* `kimi_cli/tools/file/replace.py` is a pure re-export shim (`EditFile`/`Edit`),
  kept intentionally for backwards compatibility (C10 probe 11 asserts identity).

## 4. Removal decisions still required

Per plan §4.3 a tool that is unregistered **and** untested is not an acceptable
terminal state. Six of the nine orphans are also in the plan's explicit decision
gates (C16, K15, K16, K17, K18). The remaining three (`HashRead`, `HashEdit`,
plus the `HashLine` alias) are covered by C15 and must be registered or deleted.

Until the orchestrator records the decisions, `G-ORPHAN` stays **red** and the
nine rows above are the outstanding decision list.
