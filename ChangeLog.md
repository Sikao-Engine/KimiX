# ChangeLog

## 2026-10-11 — Relocate remaining `kimix.tools` packages into `kimi_cli.tools`

Finished the tool-package migration started earlier (which had already moved
`common`, `security`, `prompt_common`, `file/bash`, `file/run`, `file/find_str`
and `background`). The remaining `src/kimix/tools/` packages were moved into
`kimi-cli/src/kimi_cli/tools/` and `src/kimix/tools/` was removed entirely:

- `kimix.tools.agent` -> `kimi_cli.tools.subagent` (new package; classes
  `Agent`/`AgentList`/`AgentClose` + `wait_for_background_agents`/`store`). The
  pre-existing `kimi_cli.tools.agent` (`AgentTool`) is left untouched; the two
  intentionally-distinct `subagent` implementations stay waived in
  `tools/gate_dup_allowlist.txt`.
- `kimix.tools.web.fetch_url` -> `kimi_cli.tools.web.kimix_fetch` (class
  `fetch_url` kept); `kimix.tools.web.web_fetcher` -> `kimi_cli.tools.web.web_fetcher`;
  the old `web/__init__.py` re-export was dropped.
- `kimix.tools.{context,note,parser,py,swarm}` -> `kimi_cli.tools.{context,note,parser,py,swarm}`
  (no name collisions existed; `parser`/`swarm` had none in `kimi_cli.tools`).

To keep `kimi_cli` from importing `kimix`/`kimi_agent_sdk` at module load (they
depend on `kimi_cli`, and the SDK imports `kimi_cli.app`, which would form an
import cycle), every `kimix.*` and `kimi_agent_sdk.Session`/`_sdk_sessions_dir`
reference in the moved modules was converted to a function-level lazy import
(matching the pattern already used by `kimi_cli.tools.common` and
`kimi_cli.tools.background`). Module-level instantiations that referenced
`kimix` at import time were deferred into lazy builders
(`kimi_cli.tools.parser._get_language_map`,
`kimi_cli.tools.swarm._subagent_type_map`), and the process-wide subagent close
hook registration was deferred into `_ensure_close_hook_registered`. Base tool
class/type imports moved from `kimi_agent_sdk` to `kosong.tooling`.

Repo-wide reference rewrites: all `kimix.tools.*` module paths and
`src/kimix/tools/...` path strings were updated across the five
`src/kimix/agent_*.json` manifests, `src/kimix/utils/prompt.py`, the server
session managers, `kimi_cli.soul.toolset`, the `tools/` gate/inventory/allowlist
scripts, `scripts/verify_python_enhancements.py`, the affected tests (including
retargeting monkeypatch strings for the now-lazy symbols to their source
`kimix.utils.*`), and the skill/docs (`tool` skill, `kimix_api` SKILL + api
reference). The dead `kimi-cli/src/kimix/tools` compat stub docstrings were
reworded. `import kimix.tools` now resolves to nothing (package removed).
