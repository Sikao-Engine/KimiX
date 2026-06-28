from __future__ import annotations

from pathlib import Path
from typing import Any

import mcp.types
from pydantic.networks import FileUrl


class MCPRootsHandler:
    """Handler for MCP ``roots/list`` requests."""

    def __init__(self, roots: list[Path] | None = None) -> None:
        self._roots: list[Path] = list(roots or [])

    def add_root(self, path: Path) -> None:
        """Add a workspace root."""
        self._roots.append(path)

    def set_roots(self, roots: list[Path]) -> None:
        """Replace the workspace roots."""
        self._roots = list(roots)

    @property
    def roots(self) -> list[Path]:
        """Return the configured workspace roots."""
        return list(self._roots)

    def to_fastmcp_roots(self) -> list[mcp.types.Root]:
        """Convert the roots to fastmcp's ``RootsList`` format."""
        return [
            mcp.types.Root(
                uri=FileUrl(f"file://{root.resolve().as_posix()}"),
                name=root.name,
            )
            for root in self._roots
        ]

    async def handle_roots_request(self, ctx: Any) -> list[mcp.types.Root]:
        """Async handler for ``roots/list`` requests."""
        return self.to_fastmcp_roots()
