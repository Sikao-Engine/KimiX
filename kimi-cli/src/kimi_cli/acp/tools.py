"""ACP tool integration stubs.

The Shell and Terminal tools have been removed from the codebase.
The replace_tools function is kept as a no-op for compatibility with
callers in acp/server.py.
"""

from __future__ import annotations

from kimi_cli.soul.toolset import KimiToolset


def replace_tools(
    client_capabilities: object,
    acp_conn: object,
    acp_session_id: str,
    toolset: KimiToolset,
    runtime: object,
) -> None:
    """No-op: Shell tool has been removed, so ACP terminal replacement is unused."""
    pass
