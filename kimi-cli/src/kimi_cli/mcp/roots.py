from __future__ import annotations

from pathlib import Path
from typing import Any

import mcp.types
from kaos.path import KaosPath
from pydantic.networks import FileUrl


class MCPRootsHandler:
    """Handler for MCP ``roots/list`` requests."""

    def __init__(
        self,
        roots: list[Path] | None = None,
        *,
        work_dir: KaosPath | None = None,
    ) -> None:
        self._roots: list[Path] = []
        if roots:
            self.set_roots(roots, work_dir=work_dir)

    @staticmethod
    def _resolve_root(root: Path, work_dir: KaosPath | None) -> Path:
        """Resolve a single root to an absolute local Path.

        Relative roots are resolved against *work_dir* when provided; otherwise
        they are rejected so that callers cannot silently use the process cwd.
        """
        if root.is_absolute():
            return root.resolve()
        if work_dir is None:
            raise ValueError("work_dir is required to resolve relative MCP roots")
        return (Path(str(work_dir)) / root).resolve()

    def add_root(self, path: Path, *, work_dir: KaosPath | None = None) -> None:
        """Add a workspace root."""
        self._roots.append(self._resolve_root(path, work_dir))

    def set_roots(
        self, roots: list[Path], *, work_dir: KaosPath | None = None
    ) -> None:
        """Replace the workspace roots."""
        self._roots = [self._resolve_root(root, work_dir) for root in roots]

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
