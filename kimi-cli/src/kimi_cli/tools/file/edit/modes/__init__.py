"""Mode executors for the multi-mode edit tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .replace import ReplaceModeExecutor
from .sloppy import SloppyModeExecutor

if TYPE_CHECKING:

    from kimi_cli.tools.file.edit.params import EditMode

MODE_REGISTRY: dict[EditMode, type] = {
    "replace": ReplaceModeExecutor,
    "sloppy": SloppyModeExecutor,
}
