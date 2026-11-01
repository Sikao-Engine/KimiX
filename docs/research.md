# Research Tool

The `research` tool delegates a self-contained question to a fresh, anonymous, read-only child agent. It returns the researcher's final findings directly to the calling agent instead of returning only a session or output-file reference.

The worker and planner manifests register the tool as `research`. The child uses the `agent_readonly.json` manifest and can inspect files, search the workspace or web, fetch pages, and load skills, but cannot edit the project.

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
