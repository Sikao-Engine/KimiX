"""Kimi CLI X package.

Startup-performance design (Python 3.15+)
------------------------------------------
Historically this module eagerly executed ``from kimix.utils import *`` and
``from kimix.base import *``, which pulled in the SDK (``kosong`` /
``kimi_agent_sdk`` / pydantic) and cost ~0.6 s on a bare ``import kimix`` and
on ``kimix --help``.

The exact same public namespace is now provided lazily through PEP 562
(``__getattr__`` + ``__all__`` + ``__dir__``): the export map below was
generated from the pre-lazy namespace, and every name still resolves to the
*identical* object it used to, imported from the module that originally
provided it (verified by identity in the startup test-suite).  No heavy
module is imported merely to enumerate the exports — the map is static data.

PEP 810 lazy imports cannot be used here because ``lazy from ... import *``
is a syntax error; the explicit export map is the maintainable equivalent.
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Static export map: historical name -> module that provides it.
# Generated from the eager-star-import namespace (identity-verified). When
# adding a public export to a provider module, update this map and the
# compatibility test; unlike eager star imports, static maps do not auto-expand.
# ---------------------------------------------------------------------------
_EXPORTS: dict[str, str] = {
    # -- kimix.base (19)
    "COMMON_SKILL_DIRS": "kimix.base",
    "generate_memory": "kimix.base",
    "get_default_sub_provider": "kimix.base",
    "get_default_sub_providers_by_role": "kimix.base",
    "get_skill_dirs": "kimix.base",
    "run_process_with_error": "kimix.base",
    "run_process_with_error_async": "kimix.base",
    "run_script": "kimix.base",
    "run_thread": "kimix.base",
    "set_default_agent_file": "kimix.base",
    "set_default_agent_file_dir": "kimix.base",
    "set_default_manually_cot": "kimix.base",
    "set_default_provider": "kimix.base",
    "set_default_skill_dirs": "kimix.base",
    "set_default_sub_providers": "kimix.base",
    "set_default_thinking": "kimix.base",
    "set_default_yolo": "kimix.base",
    "sync_all": "kimix.base",
    "warnings": "kimix.base",
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
    # -- kimix.utils (39 public + 13 historical private re-exports)
    # These underscore names were in kimix.utils.__all__, so the old root star
    # import bound them as attributes even though root star exports excluded
    # them. Keep direct access compatible without adding them to __all__.
    "_SYSTEM_PROMP": "kimix.utils",
    "_add_cli_session": "kimix.utils",
    "_cli_sessions": "kimix.utils",
    "_create_config": "kimix.utils",
    "_create_default_session": "kimix.utils",
    "_create_default_session_async": "kimix.utils",
    "_create_session_async": "kimix.utils",
    "_default_session": "kimix.utils",
    "_ensure_skill_dirs": "kimix.utils",
    "_print_usage": "kimix.utils",
    "_refresh_cli_sessions": "kimix.utils",
    "_remove_cli_session": "kimix.utils",
    "_should_print_usage": "kimix.utils",
    "SystemPromptType": "kimix.utils",
    "ToolInvocation": "kimix.utils",
    "async_fix_error": "kimix.utils",
    "async_prompt": "kimix.utils",
    "build_planner_provider_dict": "kimix.utils",
    "cancel_prompt": "kimix.utils",
    "clear_default_context": "kimix.utils",
    "clear_session_async": "kimix.utils",
    "close_process_event_loop": "kimix.utils",
    "close_session": "kimix.utils",
    "close_session_async": "kimix.utils",
    "collect_tool_invocations": "kimix.utils",
    "compact_default_context": "kimix.utils",
    "context_path": "kimix.utils",
    "create_session": "kimix.utils",
    "create_supervisor_session": "kimix.utils",
    "delete_session_dir": "kimix.utils",
    "fix_error": "kimix.utils",
    "get_cancel_event": "kimix.utils",
    "get_default_session": "kimix.utils",
    "get_process_event_loop": "kimix.utils",
    "get_system_prompt": "kimix.utils",
    "get_tool_call_errors": "kimix.utils",
    "init": "kimix.utils",
    "iter_tool_invocations": "kimix.utils",
    "lock_planner_session_read_only": "kimix.utils",
    "make_kaos_dir": "kimix.utils",
    "print_usage": "kimix.utils",
    "prompt": "kimix.utils",
    "prompt_async": "kimix.utils",
    "prompt_path": "kimix.utils",
    "prompt_plan": "kimix.utils",
    "prompt_plan_async": "kimix.utils",
    "refresh_env_from_registry": "kimix.utils",
    "register_session_close_hook": "kimix.utils",
    "run_on_event_loop": "kimix.utils",
    "session_history": "kimix.utils",
    "split_tool_result": "kimix.utils",
    "tool_call_error_dicts": "kimix.utils",
}

# Sub-packages/modules that were previously bound as attributes by the eager
# star imports (``from kimix import base`` etc. must keep working).
_SUBMODULES = ("base", "ui", "utils")


def _platform_exports() -> list[str]:
    """All star-exportable names for the current platform.

    ``kernel32`` is only defined by :mod:`kimix.ui.printing` on Windows
    (``ctypes.windll.kernel32`` inside a ``try`` block); star-importing it on
    other platforms would raise ``AttributeError``.
    """
    names = [n for n in _EXPORTS if n != "kernel32" and not n.startswith("_")]
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
    """Expose the historical namespace (resolved or not) to ``dir()``."""
    private_exports = {name for name in _EXPORTS if name.startswith("_")}
    return sorted(set(globals()) | set(__all__) | private_exports)
