"""Public-namespace compatibility for the lazy ``kimix`` / ``kimix.ui`` packages.

The eager star imports (``from kimix.utils import *`` + ``from kimix.base
import *``) were replaced by PEP 562 lazy export maps.  These subprocess
tests pin the contract so the namespace cannot silently drift:

* ``__all__`` / ``dir()`` must expose exactly the historical public names
  (lists embedded below — hermetic, no reliance on .kimix_cache artifacts).
* ``from kimix import *`` and ``from kimix.ui import *`` must bind every
  historical name.
* Attribute identity with the canonical source modules must be preserved
  after reification (e.g. ``kimix.print_info is kimix.ui.printing.print_info``),
  including the legacy accidental exports (``time``, ``annotations``, …) and
  the platform-conditional ``kernel32``.
"""

from __future__ import annotations

import os
import subprocess
import sys

# Historical ``sorted(n for n in vars(kimix) if not n.startswith('_'))``
# captured before the lazy-export change (130 names, Windows capture).
ROOT_PUBLIC = sorted(
    [
        "Any",
        "ApprovalRequest",
        "BackgroundTaskDisplayBlock",
        "BgColor",
        "BgColor256",
        "BgTrueColor",
        "BriefDisplayBlock",
        "COMMON_SKILL_DIRS",
        "Callable",
        "Color",
        "Color256",
        "CompactionBegin",
        "CompactionEnd",
        "DiffDisplayBlock",
        "DisplayBlock",
        "Enum",
        "GRAY",
        "GRAY_DARK",
        "GRAY_LIGHT",
        "GRAY_NEAR_BLACK",
        "MessageType",
        "Path",
        "PrintStream",
        "ShellDisplayBlock",
        "StepBegin",
        "StepInterrupted",
        "StreamPrintState",
        "Style",
        "SystemPromptType",
        "TOOL_NAME_REDIRECTS",
        "TRUE_GRAY",
        "TYPE_CHECKING",
        "TextPart",
        "ThinkPart",
        "TodoDisplayBlock",
        "ToolCall",
        "ToolCallPart",
        "ToolInvocation",
        "ToolResult",
        "TrueColor",
        "UnknownDisplayBlock",
        "annotations",
        "async_fix_error",
        "async_prompt",
        "asyncio",
        "base",
        "build_planner_provider_dict",
        "builtins",
        "cancel_prompt",
        "clear_default_context",
        "clear_session_async",
        "close_process_event_loop",
        "close_session",
        "close_session_async",
        "collect_tool_invocations",
        "colorful_print",
        "colorful_text",
        "compact_default_context",
        "context_path",
        "create_session",
        "create_supervisor_session",
        "ctypes",
        "dataclass",
        "delete_session_dir",
        "fix_error",
        "format_tool_args",
        "functools",
        "generate_memory",
        "get_cancel_event",
        "get_default_session",
        "get_default_sub_provider",
        "get_default_sub_providers_by_role",
        "get_process_event_loop",
        "get_skill_dirs",
        "get_system_prompt",
        "get_tool_call_errors",
        "init",
        "io",
        "iter_tool_invocations",
        "kernel32",
        "lock_planner_session_read_only",
        "make_kaos_dir",
        "normalize_tool_name",
        "orjson",
        "os",
        "percentage_and_token",
        "percentage_str",
        "print",
        "print_agent_json",
        "print_agent_json_flush_text",
        "print_debug",
        "print_error",
        "print_info",
        "print_string",
        "print_success",
        "print_usage",
        "print_warning",
        "prompt",
        "prompt_async",
        "prompt_path",
        "prompt_plan",
        "prompt_plan_async",
        "re",
        "refresh_env_from_registry",
        "register_session_close_hook",
        "resolve_tool_name",
        "run_on_event_loop",
        "run_process_with_error",
        "run_process_with_error_async",
        "run_script",
        "run_thread",
        "session_history",
        "set_default_agent_file",
        "set_default_agent_file_dir",
        "set_default_manually_cot",
        "set_default_provider",
        "set_default_skill_dirs",
        "set_default_sub_providers",
        "set_default_thinking",
        "set_default_yolo",
        "split_tool_result",
        "subprocess",
        "sync_all",
        "sys",
        "threading",
        "time",
        "tool_call_error_dicts",
        "ui",
        "utils",
        "warnings",
    ]
)

# ``kimix.utils.__all__`` historically included these private names, so the
# old ``from kimix.utils import *`` bound them on the root package. The root
# itself had no __all__, meaning they were attributes but were NOT re-exported
# by ``from kimix import *``.
ROOT_PRIVATE = sorted(
    [
        "_SYSTEM_PROMP",
        "_add_cli_session",
        "_cli_sessions",
        "_create_config",
        "_create_default_session",
        "_create_default_session_async",
        "_create_session_async",
        "_default_session",
        "_ensure_skill_dirs",
        "_print_usage",
        "_refresh_cli_sessions",
        "_remove_cli_session",
        "_should_print_usage",
    ]
)

# Historical ``sorted(n for n in vars(kimix.ui) if not n.startswith('_'))``
# (71 names, Windows capture).
UI_PUBLIC = sorted(
    [
        "Any",
        "ApprovalRequest",
        "BackgroundTaskDisplayBlock",
        "BgColor",
        "BgColor256",
        "BgTrueColor",
        "BriefDisplayBlock",
        "Callable",
        "Color",
        "Color256",
        "CompactionBegin",
        "CompactionEnd",
        "DiffDisplayBlock",
        "DisplayBlock",
        "Enum",
        "GRAY",
        "GRAY_DARK",
        "GRAY_LIGHT",
        "GRAY_NEAR_BLACK",
        "MessageType",
        "Path",
        "PrintStream",
        "ShellDisplayBlock",
        "StepBegin",
        "StepInterrupted",
        "StreamPrintState",
        "Style",
        "TOOL_NAME_REDIRECTS",
        "TRUE_GRAY",
        "TYPE_CHECKING",
        "TextPart",
        "ThinkPart",
        "TodoDisplayBlock",
        "ToolCall",
        "ToolCallPart",
        "ToolResult",
        "TrueColor",
        "UnknownDisplayBlock",
        "annotations",
        "asyncio",
        "builtins",
        "colorful_print",
        "colorful_text",
        "ctypes",
        "dataclass",
        "format_tool_args",
        "functools",
        "io",
        "kernel32",
        "normalize_tool_name",
        "orjson",
        "os",
        "percentage_and_token",
        "percentage_str",
        "print",
        "print_agent_json",
        "print_agent_json_flush_text",
        "print_debug",
        "print_error",
        "print_info",
        "print_string",
        "print_success",
        "print_warning",
        "printing",
        "re",
        "resolve_tool_name",
        "stream",
        "subprocess",
        "sys",
        "threading",
        "time",
    ]
)

# ``kernel32`` only exists on Windows (ctypes.windll); the export maps gate it.
if sys.platform != "win32":
    ROOT_PUBLIC.remove("kernel32")
    UI_PUBLIC.remove("kernel32")


def _run_snippet(code: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["KIMIX_NATIVE"] = "0"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _assert_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"snippet failed with {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_root_all_matches_historical_public_namespace() -> None:
    code = (
        "import kimix\n"
        "assert sorted(kimix.__all__) == " + repr(ROOT_PUBLIC) + ", 'kimix.__all__ drifted'\n"
        "missing = set(kimix.__all__) - set(dir(kimix))\n"
        "assert not missing, 'dir(kimix) missing: %s' % missing\n"
        "print('ALL_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "ALL_OK" in result.stdout


def test_ui_all_matches_historical_public_namespace() -> None:
    code = (
        "import kimix.ui\n"
        "assert sorted(kimix.ui.__all__) == " + repr(UI_PUBLIC) + ", 'kimix.ui.__all__ drifted'\n"
        "missing = set(kimix.ui.__all__) - set(dir(kimix.ui))\n"
        "assert not missing, 'dir(kimix.ui) missing: %s' % missing\n"
        "print('ALL_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "ALL_OK" in result.stdout


def test_root_star_import_binds_every_name() -> None:
    code = (
        "namespace = {}\n"
        "exec('from kimix import *', namespace)\n"
        "expected = " + repr(ROOT_PUBLIC) + "\n"
        "missing = [n for n in expected if n not in namespace]\n"
        "assert not missing, 'from kimix import * missing: %s' % missing\n"
        "print('STAR_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "STAR_OK" in result.stdout


def test_ui_star_import_binds_every_name() -> None:
    code = (
        "namespace = {}\n"
        "exec('from kimix.ui import *', namespace)\n"
        "expected = " + repr(UI_PUBLIC) + "\n"
        "missing = [n for n in expected if n not in namespace]\n"
        "assert not missing, 'from kimix.ui import * missing: %s' % missing\n"
        "print('STAR_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "STAR_OK" in result.stdout


def test_historical_private_reexports_remain_attributes_only() -> None:
    code = (
        "import kimix\n"
        "import kimix.utils\n"
        "expected = " + repr(ROOT_PRIVATE) + "\n"
        "for name in expected:\n"
        "    assert getattr(kimix, name) is getattr(kimix.utils, name), name\n"
        "    assert name in dir(kimix), name\n"
        "    assert name not in kimix.__all__, name\n"
        "namespace = {}\n"
        "exec('from kimix import *', namespace)\n"
        "leaked = [name for name in expected if name in namespace]\n"
        "assert not leaked, 'private names joined root star export: %s' % leaked\n"
        "print('PRIVATE_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "PRIVATE_OK" in result.stdout


def test_identity_with_canonical_source_modules() -> None:
    """Post-reification attributes must be the canonical objects."""
    code = (
        "import time as stdlib_time\n"
        "import kimix\n"
        "import kimix.base, kimix.utils, kimix.ui.printing, kimix.ui.stream\n"
        "checks = {\n"
        "    'print_info': kimix.print_info is kimix.ui.printing.print_info,\n"
        "    'run_thread': kimix.run_thread is kimix.base.run_thread,\n"
        "    'prompt': kimix.prompt is kimix.utils.prompt,\n"
        "    'sync_all': kimix.sync_all is kimix.base.sync_all,\n"
        "    'time': kimix.time is stdlib_time,\n"
        "    'annotations': kimix.annotations is kimix.base.annotations,\n"
        "    'DisplayBlock': kimix.DisplayBlock is kimix.ui.stream.DisplayBlock,\n"
        "    'ToolInvocation': kimix.ToolInvocation is kimix.utils.ToolInvocation,\n"
        "    'generate_memory': kimix.generate_memory is kimix.base.generate_memory,\n"
        "}\n"
        "if __import__('sys').platform == 'win32':\n"
        "    checks['kernel32'] = kimix.kernel32 is kimix.ui.printing.kernel32\n"
        "bad = [k for k, v in checks.items() if not v]\n"
        "assert not bad, 'identity broken for: %s' % bad\n"
        "print('IDENTITY_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "IDENTITY_OK" in result.stdout


def test_submodule_attributes_and_explicit_imports() -> None:
    code = (
        "import kimix\n"
        "import kimix.ui\n"
        "assert kimix.base.__name__ == 'kimix.base'\n"
        "assert kimix.utils.__name__ == 'kimix.utils'\n"
        "assert kimix.ui.__name__ == 'kimix.ui'\n"
        "assert kimix.ui.printing.__name__ == 'kimix.ui.printing'\n"
        "assert kimix.ui.stream.__name__ == 'kimix.ui.stream'\n"
        "from kimix import prompt, create_session, init as kimix_init\n"
        "from kimix.ui import print_error, MessageType, percentage_str\n"
        "from kimix.ui import printing, stream\n"
        "from kimix.base import run_thread\n"
        "from kimix.utils import close_session_async\n"
        "assert kimix.prompt is prompt\n"
        "assert kimix.ui.print_error is print_error\n"
        "print('IMPORT_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "IMPORT_OK" in result.stdout


def test_unknown_attribute_raises_attribute_error() -> None:
    code = (
        "import kimix\n"
        "import kimix.ui\n"
        "for mod in (kimix, kimix.ui):\n"
        "    try:\n"
        "        mod.definitely_not_a_real_export\n"
        "    except AttributeError:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('expected AttributeError from %r' % (mod.__name__,))\n"
        "print('ATTR_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "ATTR_OK" in result.stdout


def test_attrs_not_silently_dropped() -> None:
    """Every historical public name must resolve via attribute access."""
    code = (
        "import kimix\n"
        "import kimix.ui\n"
        "for name in " + repr(ROOT_PUBLIC) + ":\n"
        "    getattr(kimix, name)\n"
        "for name in " + repr(UI_PUBLIC) + ":\n"
        "    getattr(kimix.ui, name)\n"
        "print('RESOLVE_OK')\n"
    )
    result = _run_snippet(code)
    _assert_ok(result)
    assert "RESOLVE_OK" in result.stdout
