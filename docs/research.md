# Research Tool

The `research` tool delegates a self-contained question to a fresh, anonymous, read-only child agent. It returns the researcher's final findings directly to the calling agent instead of returning only a session or output-file reference.

The worker and planner manifests register the tool as `research`. The child uses the `agent_readonly.json` manifest and can inspect files, search the workspace or web, fetch pages, load skills, and save report files — but never edit the project (see "Report artifacts" below).

## Report artifacts

The child owns `write` and `edit` in **cache-only mode**: every argument path must be a plain relative file name, and the file is saved under `<work dir>/.kimix_cache/research/<session id>/`. Absolute paths, drive-qualified paths, `..` escapes, `conflict://`/URL schemes and symlinks that leave the sandbox are refused, so a researcher can leave a long report behind without ever touching the repository or the session/temp state that already lives in `.kimix_cache`. These writes are auto-approved (the child has no attached user) and every accepted path is recorded on the child session. The per-session subdirectory keeps two parallel research runs from overwriting each other's reports.

Each artifact path the child wrote is appended to the returned text:

```
## Research artifacts
The researcher wrote 1 file(s) (sandboxed to `.kimix_cache/`):
- .kimix_cache/research/<session id>/findings.md
Use `read` with these paths to inspect the full content.
```
The paths are relative to the shared work directory, so the calling agent can hand them straight to `read`. The cache-only scope lives in `kimi_cli/tools/file/cache_scope.py`; normal read-write sessions are unaffected.

## Configure the researcher provider

Add an entry with `"role": "researcher"` to the main provider's `sub_providers` list. The entry can contain a complete provider configuration or link to another JSON file.

```json
{
  "role": "researcher",
  "json": "qwen_flash.json"
}
```

Relative linked files are resolved from the directory containing the main config, then from its parent directories. An absolute path is also accepted. See [`research.json`](research.json) for a complete inline example.

If no `researcher` sub-provider is configured, the tool uses the parent session's provider. Configuring the role explicitly allows research to use a cheaper or faster model without changing the main, sub-agent, or planner providers.

## Call the tool

A foreground call waits for the researcher and returns its report:

```json
{
  "query": "Inspect the repository's provider configuration loader and summarize how linked sub-provider JSON files are resolved. Cite file paths."
}
```

The query must be self-contained because the researcher cannot see the caller's conversation. Include the goal, relevant paths or URLs, constraints, and the desired output.

For a longer task, start it in the background:

```json
{
  "query": "Compare the provider setup documented in docs/ with the implementation and report discrepancies.",
  "run_in_background": true
}
```

The result contains a task ID such as `research_1`. Use `job_output` with that ID to poll or wait for completion:

```json
{
  "job_id": "research_1",
  "wait": true,
  "timeout": 60
}
```

Use `job_output` with `"action": "kill"` to cancel a run. Research child sessions are closed after success, failure, or cancellation.
