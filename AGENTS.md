# Rules:

- After writing any Python file, run `uv run tools/syntax_check.py <python_file> [<python_file> ...]` to verify python syntax. Run related tests to verify.
- Fix all errors reported by the syntax checker before proceeding.
- use `uv run tools/git_diff.py <file> [<file> ...]` to check file diff.
- use `uv sync --extra=all` after update any `pyproject.toml` to verify the changes.

# MCP conventions

- New MCP client/server code belongs under `kimi-cli/src/kimi_cli/mcp/`.
- Use the `MCPClient` wrapper around `fastmcp.Client` instead of calling `fastmcp` APIs directly,
  so future version upgrades are isolated.
- Keep server mode in the CLI layer (`kimi-cli/src/kimi_cli/cli/mcp.py` and
  `src/kimix/cli_impl/mcp_cmd.py`); do not pull `mcp/server.py` into `soul/` or `agent.py` to
  avoid circular dependencies.
- Project-level MCP configuration lives in `.kimix/mcp.json`; global configuration lives in
  `~/.kimi/mcp.json`. Discovery helpers are in `kimi_cli/mcp/config.py`.
- After modifying any MCP file, run `uv run tools/syntax_check.py <file>` and
  `uv run pytest kimi-cli/tests/mcp`.