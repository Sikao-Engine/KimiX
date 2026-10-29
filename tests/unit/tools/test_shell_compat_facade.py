r"""Phase 1 facade/extraction regression tests for ``kimix_native._shell_compat``.

``bin/kimix_native/_shell_compat.py`` was reduced to a thin compatibility facade
over the new ``kimix_native.shell_compat`` package (fallback_data / bash_core /
pwsh_fix / pwsh_transform).  This module is a **pure code motion**: behavior,
dataclass identity, and the historical module-level symbol surface must be
unchanged.  These tests lock that contract:

1. every name in the Phase-0 symbol inventory is still an attribute of the
   facade, and every ``_shell.<name>`` the kimi-cli bash tool modules re-export
   resolves on it;
2. ``BashFix`` stays a frozen dataclass with the exact positional field order;
3. loading the facade *file* directly with
   ``importlib.util.spec_from_file_location`` (the way ``tests/test_bash.py``
   does) still execs and its delegates still run;
4. the delegating entry points (``fix_bash_command``, ``_process_unquoted``,
   ``fix_pwsh_command``, ``pwsh_transform``) produce the documented current
   outputs;
5. spot checks pin the fallback/unsupported data tables byte-for-byte.

All assertions are pure Python: no subprocess, no real Git Bash, no shell
execution.  The scanner is reached the same way the sibling
``tests/unit/tools/test_bash_*.py`` suites reach it -- through
``kimi_cli.native_loader.get_compat`` (which is ``import_module`` of the shim).
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import os
import sys

import pytest

from kimi_cli.native_loader import get_compat as _get_compat

_shell_compat = _get_compat("_shell_compat")

_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)
_INVENTORY_PATH = os.path.join(_REPO_ROOT, ".kimix_cache", "phase0_symbol_inventory.txt")
_SHIM_PATH = os.path.join(_REPO_ROOT, "bin", "kimix_native", "_shell_compat.py")
_CLI_DIR = os.path.join(_REPO_ROOT, "kimi-cli", "src", "kimi_cli", "tools", "file", "bash")
_CLI_FILES = (
    "bash_fix.py",
    "bash_tool.py",
    "pwsh_fix.py",
    "process_pwsh.py",
)


#: The inventory is a *generated* Phase-0 artifact written into the transient
#: ``.kimix_cache/`` scratch dir, so it is absent in a fresh checkout (and in
#: CI).  The inventory-derived checks below therefore skip instead of aborting
#: collection of the whole root suite; the authoritative re-export contract
#: (every ``_shell.<name>`` the app modules read) is asserted independently by
#: ``test_cli_bash_modules_all_names_resolve``.
_INVENTORY_MISSING = (
    f"phase-0 symbol inventory artifact not regenerated ({_INVENTORY_PATH})"
)


def _inventory() -> list[str]:
    """The frozen Phase-0 symbol surface, or ``[]`` when the artifact is absent."""
    try:
        with open(_INVENTORY_PATH, encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


_INVENTORY = _inventory()
_INVENTORY_PARAMS = (
    list(_INVENTORY)
    if _INVENTORY
    else [pytest.param("<inventory-missing>", marks=pytest.mark.skip(reason=_INVENTORY_MISSING))]
)


# --------------------------------------------------------------------------- 1
def test_inventory_is_195_names() -> None:
    if not _INVENTORY:
        pytest.skip(_INVENTORY_MISSING)
    assert len(_INVENTORY) == 195


@pytest.mark.parametrize("name", _INVENTORY_PARAMS)
def test_facade_exposes_every_inventory_name(name: str) -> None:
    """The facade re-exports the complete historical module-level surface."""
    assert hasattr(_shell_compat, name), f"kimix_native._shell_compat lost {name!r}"


def test_facade_importable_by_path() -> None:
    assert _shell_compat is importlib.import_module("kimix_native._shell_compat")


def _consumer_names() -> set[str]:
    """Every ``_shell.<name>`` the four kimi-cli bash modules reference.

    The app modules bind ``_shell = get_compat("_shell_compat")`` and then read
    ``_shell.<name>``.  We match the exact ``_shell`` alias (not the longer
    ``_shell_compat`` module name) so prose references to the file
    ``_shell_compat.py`` are not mistaken for attribute access.
    """
    names: set[str] = set()
    import regex  # the repo's blessed regex, per AGENTS.md

    # (?<!\w) prevents matching inside _shell_compat; the alias is exactly _shell
    pat = regex.compile(r"(?<!\w)_shell\.([A-Za-z_][A-Za-z0-9_]*)")
    for fname in _CLI_FILES:
        path = os.path.join(_CLI_DIR, fname)
        with open(path, encoding="utf-8") as fh:
            for m in pat.finditer(fh.read()):
                names.add(m.group(1))
    return names


def test_cli_bash_modules_all_names_resolve() -> None:
    names = _consumer_names()
    # sanity: the inventory was compiled from these same references.
    assert names, "found no _shell.<name> references"
    missing = sorted(n for n in names if not hasattr(_shell_compat, n))
    assert not missing, f"app-layer consumers reference missing names: {missing}"


# --------------------------------------------------------------------------- 3
def test_bashfix_is_frozen_dataclass_with_exact_positional_order() -> None:
    assert dataclasses.is_dataclass(_shell_compat.BashFix)
    assert _shell_compat.BashFix.__dataclass_params__.frozen is True
    assert _shell_compat.BashFix.__dataclass_params__.eq is True
    order = [f.name for f in dataclasses.fields(_shell_compat.BashFix)]
    assert order == [
        "command",
        "replacements",
        "path_changes",
        "shell_wrappers",
        "nul_fixes",
        "unsupported",
        "cmd_conditional_fixes",
        # Appended after the original fields, so every pre-existing positional
        # construction (and the kimi-cli layer's keyword calls) stays valid.
        "native_flag_fixes",
    ]


def test_bashfix_defaults_are_empty_tuples() -> None:
    BashFix = _shell_compat.BashFix
    fx = BashFix("x")
    assert (
        fx.replacements
        == fx.path_changes
        == fx.shell_wrappers
        == fx.nul_fixes
        == fx.unsupported
        == fx.cmd_conditional_fixes
        == fx.native_flag_fixes
        == ()
    )
    # .changed excludes an unsupported-only refusal.
    assert BashFix("x").changed is False
    assert BashFix("x", unsupported=("goto",)).changed is False
    assert BashFix("x", nul_fixes=("nul",)).changed is True
    assert BashFix("x", native_flag_fixes=("//a",)).changed is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        fx.command = "y"  # type: ignore[misc]


# --------------------------------------------------------------------------- 4
def _load_shim_by_path() -> object:
    """Reproduce tests/test_bash.py's direct spec_from_file_location load."""
    name = "_shell_compat_facade_probe"
    spec = importlib.util.spec_from_file_location(name, _SHIM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Required so @dataclass can resolve the module namespace during exec.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_direct_file_load_points_at_repo_copy() -> None:
    module = _load_shim_by_path()
    assert os.path.normcase(os.path.abspath(module.__file__)) == os.path.normcase(
        os.path.abspath(_SHIM_PATH)
    )


def test_direct_file_load_delegates_work() -> None:
    module = _load_shim_by_path()
    # All four entry points must exist and run on the standalone-loaded module.
    assert callable(module.fix_bash_command)
    assert callable(module._process_unquoted)
    assert callable(module.fix_pwsh_command)
    assert callable(module.pwsh_transform)

    assert module.fix_bash_command("echo hello").command == "echo hello"
    assert module.fix_bash_command("echo hi > nul").command == "echo hi > /dev/null"
    assert module._process_unquoted("C:\\Users\\me") == "C:/Users/me"
    repaired = module.fix_pwsh_command("Write-Output 'unclosed")
    assert repaired is not None and repaired.command == "Write-Output 'unclosed'"
    out, warnings = module.pwsh_transform("$a ?? $b")
    assert isinstance(out, str) and isinstance(warnings, list)
    # _FALLBACK_BODIES / _build_region_mask also reachable through the
    # standalone-loaded module (tests/test_bash.py relies on both).
    assert "taskkill" in module._FALLBACK_BODIES
    assert list(module._build_region_mask("'x'"))


# --------------------------------------------------------------------------- 5
def test_fix_bash_command_passthrough_no_replacements() -> None:
    fx = _shell_compat.fix_bash_command("echo hello")
    assert fx.command == "echo hello"
    assert fx.replacements == ()
    assert fx.nul_fixes == ()
    assert fx.changed is False


def test_fix_bash_command_nul_rewrite() -> None:
    fx = _shell_compat.fix_bash_command("echo hi > nul")
    assert fx.command == "echo hi > /dev/null"
    assert fx.nul_fixes == ("nul",)
    assert fx.changed is True


def test_fix_pwsh_command_unclosed_quote_returns_pwshfix() -> None:
    result = _shell_compat.fix_pwsh_command("Write-Output 'unclosed")
    assert isinstance(result, _shell_compat.PwshFix)
    assert result.command == "Write-Output 'unclosed'"
    assert "unclosed single-quoted string" in result.warning
    # empty / whitespace input returns None (documented current behavior).
    assert _shell_compat.fix_pwsh_command("") is None
    assert _shell_compat.fix_pwsh_command("   ") is None


def test_pwsh_transform_returns_str_and_list() -> None:
    out, warnings = _shell_compat.pwsh_transform("$a ?? $b")
    assert isinstance(out, str)
    assert isinstance(warnings, list)
    assert out == "$(if ($null -ne $a) { $a } else { $b })"
    assert any("Line 1" in w for w in warnings)


# --------------------------------------------------------------------------- 6
def test_fallback_bodies_spot_checks() -> None:
    bodies = _shell_compat._FALLBACK_BODIES
    assert bodies["taskkill"].startswith("local __kimix_force=0")
    assert "/IM|/im" in bodies["taskkill"]
    assert "__kimix_delay" in bodies["top"]
    assert "__kimix_depth" in bodies["tree"]
    assert "__kimix_list" in bodies["pgrep"]
    # the guarded-definition prefix is built by the relocated constructor.
    definition = _shell_compat._fallback_definition("taskkill")
    assert definition.startswith("if ! command -v taskkill >/dev/null 2>&1; then ")
    assert "taskkill() {" in definition
    assert definition.endswith("; }; fi")


def test_unsupported_bodies_spot_checks() -> None:
    unsupported = _shell_compat._UNSUPPORTED_BODIES
    assert set(unsupported) == {"journalctl", "goto", "call :label", "exit /b"}
    assert unsupported["journalctl"].startswith("systemd's journal does not exist")
    assert unsupported["goto"].startswith("cmd.exe 'goto'/':label' jumps")
    assert unsupported["call :label"].startswith("cmd.exe 'call :label'")
    assert unsupported["exit /b"].startswith("cmd.exe 'exit /b [N]'")


def test_stub_aware_and_wrapper_tables_unchanged() -> None:
    assert _shell_compat._STUB_AWARE_FALLBACKS == frozenset({"pip3", "python3"})
    assert _shell_compat._FALLBACK_COMMAND_WRAPPERS == {
        "gtimeout": "timeout",
        "watch": "watch",
        "sudo": "sudo",
    }
    assert "--interval" in _shell_compat._WRAPPER_OPTIONS_WITH_VALUE["watch"]


# --------------------------------------------------------------------- package
def test_package_modules_exist_and_are_the_source() -> None:
    """The facade re-exports must point at the shell_compat submodules."""
    # Import the submodules by full path: ``shell_compat/__init__.py`` re-exports
    # names like ``pwsh_transform``/``fix_bash_command`` that shadow the same-named
    # submodules when accessed as attributes, so bind the modules explicitly.
    bash_core = importlib.import_module("kimix_native.shell_compat.bash_core")
    fallback_data = importlib.import_module(
        "kimix_native.shell_compat.fallback_data"
    )
    pwsh_fix = importlib.import_module("kimix_native.shell_compat.pwsh_fix")
    pwsh_transform = importlib.import_module(
        "kimix_native.shell_compat.pwsh_transform"
    )

    # a symbol that lives only in bash_core is reachable through the facade.
    #
    # Native acceleration: ``fix_bash_command`` / ``_process_unquoted`` /
    # ``bash_compatibility_prelude`` are the three scanner entry points the
    # facade WRAPS (they delegate to the compiled ``runtime_py.bash_fix``
    # kernel when the native runtime is loaded and to the pure-Python
    # reference otherwise -- see the native-acceleration section of
    # _shell_compat.py), so the facade name is a delegator while the reference
    # kernel stays the ``_py_*`` capture from bash_core.  bash_core itself is
    # never re-bound: the reference must stay reachable as the parity oracle.
    assert _shell_compat._py_fix_bash_command is bash_core.fix_bash_command
    assert _shell_compat._py_process_unquoted is bash_core._process_unquoted
    assert (
        _shell_compat._py_bash_compatibility_prelude
        is bash_core.bash_compatibility_prelude
    )
    assert _shell_compat.BashFix is bash_core.BashFix
    # a symbol that lives in fallback_data
    assert _shell_compat._FALLBACK_BODIES is fallback_data._FALLBACK_BODIES
    # PowerShell modules are distinct from the bash scanner
    assert _shell_compat.pwsh_transform is pwsh_transform.pwsh_transform
    assert _shell_compat.fix_pwsh_command is pwsh_fix.fix_pwsh_command


def test_facade_is_small_and_package_is_the_implementation() -> None:
    with open(_SHIM_PATH, encoding="utf-8") as fh:
        facade_lines = fh.read().count("\n")
    # Phase 1 target: a small facade, far shorter than the 8281-line original.
    assert facade_lines < 700, facade_lines
    shim_dir = os.path.join(_REPO_ROOT, "bin", "kimix_native", "shell_compat")
    for mod in ("__init__.py", "fallback_data.py", "bash_core.py", "pwsh_fix.py",
                "pwsh_transform.py"):
        assert os.path.isfile(os.path.join(shim_dir, mod)), mod


def test_no_import_star_in_facade() -> None:
    with open(_SHIM_PATH, encoding="utf-8") as fh:
        text = fh.read()
    assert "import *" not in text


# --------------------------------------------------------- late-binding contract
def test_facade_module_level_patch_reaches_scanner(monkeypatch) -> None:
    """The one historical late-binding edge preserved across the namespace split.

    tests/test_bash.py monkeypatches ``_shell._windows_temp_dir`` (the facade
    handle) and expects ``fix_bash_command``'s internal ``/tmp`` rewrite to see
    it.  After Phase 1 the scanner call-site lives in ``bash_core``'s globals,
    so the facade must remain the authoritative namespace for that name (via the
    documented ``_windows_temp_dir_from_namespace`` resolver).  This locks that:
    patching the facade name must change the scanner output, and restoring it
    must reproduce the unpatched value (byte-for-byte neutral when unpatched).
    """
    import kimix_native.shell_compat.bash_core as bash_core

    sentinel = "C:/Users/John Doe/AppData/Local/Temp"
    original = _shell_compat._windows_temp_dir
    assert bash_core._windows_temp_dir is original  # facade + core share one def

    monkeypatch.setattr(_shell_compat, "_windows_temp_dir", lambda: sentinel)
    fx = _shell_compat.fix_bash_command("echo /tmp/x")
    assert fx.command == f'echo "{sentinel}/x"'

    monkeypatch.setattr(_shell_compat, "_windows_temp_dir", original)
    real = _shell_compat.fix_bash_command("echo /tmp/x")
    assert real.command == f"echo {_shell_compat._windows_temp_dir()}/x"
