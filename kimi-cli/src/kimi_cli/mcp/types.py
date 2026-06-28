from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import fastmcp


@dataclass(slots=True)
class MCPConnectionInfo:
    """Connection state for one MCP server."""

    name: str
    client: fastmcp.Client[Any]
    config: dict[str, Any]
