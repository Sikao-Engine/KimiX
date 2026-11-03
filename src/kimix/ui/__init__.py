"""kimix UI layer: terminal printing and wire-message streaming (P8).

Startup-performance design (Python 3.15+)
------------------------------------------
This package used to eagerly execute ``from kimix.ui.printing import *`` and
``from kimix.ui.stream import *``.  The identical public namespace is now
provided lazily through PEP 562 (``__getattr__`` + ``__all__`` + ``__dir__``)
with a static, identity-verified export map, so ``import kimix.ui`` no longer
executes the printing/stream modules (which pull in ``kimi_cli``).

PEP 810 lazy imports cannot express star imports
(``lazy from ... import *`` is a syntax error); the explicit export map is
the maintainable equivalent.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Static export map: public name -> module that provides it.
# Generated from the eager-star-import namespace (identity-verified).
# ---------------------------------------------------------------------------
_EXPORTS: dict[str, str] = {
    # -- kimix.ui.printing (39)
    "Any": "kimix.ui.printing",
    "BgColor": "kimix.ui.printing",
    "BgColor256": "kimix.ui.printing",
    "BgTrueColor": "kimix.ui.printing",
    "Callable": "kimix.ui.printing",
    "Color": "kimix.ui.printing",
    "Color256": "kimix.ui.printing",
    "Enum": "kimix.ui.printing",
    "GRAY": "kimix.ui.printing",
    "GRAY_DARK": "kimix.ui.printing",
    "GRAY_LIGHT": "kimix.ui.printing",
    "GRAY_NEAR_BLACK": "kimix.ui.printing",
    "MessageType": "kimix.ui.printing",
    "Path": "kimix.ui.printing",
    "PrintStream": "kimix.ui.printing",
    "StreamPrintState": "kimix.ui.printing",
    "Style": "kimix.ui.printing",
    "TRUE_GRAY": "kimix.ui.printing",
    "TrueColor": "kimix.ui.printing",
    "annotations": "kimix.ui.printing",
    "builtins": "kimix.ui.printing",
    "colorful_print": "kimix.ui.printing",
    "colorful_text": "kimix.ui.printing",
    "ctypes": "kimix.ui.printing",
    "dataclass": "kimix.ui.printing",
    "functools": "kimix.ui.printing",
    "io": "kimix.ui.printing",
    "kernel32": "kimix.ui.printing",  # platform-conditional (Windows only)
    "os": "kimix.ui.printing",
    "print": "kimix.ui.printing",
    "print_debug": "kimix.ui.printing",
    "print_error": "kimix.ui.printing",
    "print_info": "kimix.ui.printing",
    "print_string": "kimix.ui.printing",
    "print_success": "kimix.ui.printing",
    "print_warning": "kimix.ui.printing",
    "sys": "kimix.ui.printing",
    "threading": "kimix.ui.printing",
    "time": "kimix.ui.printing",
    # -- kimix.ui.stream (30)
    "ApprovalRequest": "kimix.ui.stream",
    "BackgroundTaskDisplayBlock": "kimix.ui.stream",
    "BriefDisplayBlock": "kimix.ui.stream",
    "CompactionBegin": "kimix.ui.stream",
    "CompactionEnd": "kimix.ui.stream",
    "DiffDisplayBlock": "kimix.ui.stream",
    "DisplayBlock": "kimix.ui.stream",
    "ShellDisplayBlock": "kimix.ui.stream",
    "StepBegin": "kimix.ui.stream",
    "StepInterrupted": "kimix.ui.stream",
    "TOOL_NAME_REDIRECTS": "kimix.ui.stream",
    "TYPE_CHECKING": "kimix.ui.stream",
    "TextPart": "kimix.ui.stream",
    "ThinkPart": "kimix.ui.stream",
    "TodoDisplayBlock": "kimix.ui.stream",
    "ToolCall": "kimix.ui.stream",
    "ToolCallPart": "kimix.ui.stream",
    "ToolResult": "kimix.ui.stream",
    "UnknownDisplayBlock": "kimix.ui.stream",
    "asyncio": "kimix.ui.stream",
    "format_tool_args": "kimix.ui.stream",
    "normalize_tool_name": "kimix.ui.stream",
    "orjson": "kimix.ui.stream",
    "percentage_and_token": "kimix.ui.stream",
    "percentage_str": "kimix.ui.stream",
    "print_agent_json": "kimix.ui.stream",
    "print_agent_json_flush_text": "kimix.ui.stream",
    "re": "kimix.ui.stream",
    "resolve_tool_name": "kimix.ui.stream",
    "subprocess": "kimix.ui.stream",
}

# Submodules that were previously bound as attributes by the eager star
# imports (``from kimix.ui import printing`` etc. must keep working).
_SUBMODULES = ("printing", "stream")


def _platform_exports() -> list[str]:
    """All star-exportable names for the current platform.

    ``kernel32`` is only defined by :mod:`kimix.ui.printing` on Windows
    (``ctypes.windll.kernel32`` inside a ``try`` block); star-importing it on
    other platforms would raise ``AttributeError``.
    """
    names = [n for n in _EXPORTS if n != "kernel32"]
    if sys.platform == "win32":
        names.append("kernel32")
    names.extend(_SUBMODULES)
    return names


__all__ = sorted(_platform_exports())


def __getattr__(name: str) -> Any:  # PEP 562 module-level __getattr__
    """Resolve a public export lazily from its providing module."""
    if name in _SUBMODULES:
        # Importing the submodule also sets it as an attribute of this
        # package (import-system side effect), so this branch runs once.
        return importlib.import_module(f"{__name__}.{name}")
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name)
    try:
        value = getattr(module, name)
    except AttributeError:
        # Platform-conditional names (e.g. kernel32 off-Windows) must behave
        # exactly as if the providing module never defined them.
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    # Cache the reified object so repeated attribute lookups (and later
    # ``vars()``/``dir()`` introspection) see a normal namespace, matching
    # the historical eager-import behavior.
    globals()[name] = value
    return value


def __dir__() -> list[str]:  # PEP 562 module-level __dir__
    """Expose the full public namespace (resolved or not) to ``dir()``."""
    return sorted(set(globals()) | set(__all__))
