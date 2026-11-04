"""Guards for the VS Code editor setup in ``.vscode/settings.json``.

The autopep8 extension ships bundled libraries that only work up to Python 3.14:
``bundled/typing_extensions.py`` reads ``typing.no_type_check_decorator``, which was
removed in 3.15, so ``import pygls`` -> ``import cattrs`` raises ``AttributeError`` and
the formatter server never starts. This platform moved the interpreter to 3.15, so the
workspace has to resolve the formatter stack from ``.venv`` via
``autopep8.importStrategy = "fromEnvironment"``.

These tests fail loudly if that wiring regresses (wrong/removed setting) or if the
underlying packages regress (``uv sync`` dropping them, or pygls 2.x sneaking back in -
2.x removed ``pygls.server.LanguageServer``, which the extension's server imports).
"""

from __future__ import annotations

import json
import subprocess
import sys
import typing
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH = REPO_ROOT / ".vscode" / "settings.json"


def _strip_jsonc_comments(text: str) -> str:
    """Remove ``//`` and ``/* */`` comments, leaving string literals untouched."""
    out: list[str] = []
    i = 0
    in_string = False
    while i < len(text):
        char = text[i]
        if in_string:
            out.append(char)
            if char == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if text.startswith("//", i):
            newline = text.find("\n", i)
            i = len(text) if newline == -1 else newline
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _load_settings() -> dict[str, object]:
    assert SETTINGS_PATH.is_file(), f"missing {SETTINGS_PATH}"
    return json.loads(_strip_jsonc_comments(SETTINGS_PATH.read_text(encoding="utf-8")))


def _resolve_workspace_vars(value: str) -> str:
    return value.replace("${workspaceFolder}", str(REPO_ROOT))


def _venv_python() -> Path:
    bindir = "Scripts" if sys.platform == "win32" else "bin"
    exe = "python.exe" if sys.platform == "win32" else "python"
    return REPO_ROOT / ".venv" / bindir / exe


# ---------------------------------------------------------------------------
# settings.json wiring
# ---------------------------------------------------------------------------


def test_settings_file_is_valid_jsonc() -> None:
    """settings.json parses once JSONC comments are stripped."""
    settings = _load_settings()
    assert isinstance(settings, dict)
    assert settings, "settings.json must configure the formatter stack"


def test_import_strategy_reads_packages_from_environment() -> None:
    """The bundled libs cannot run on 3.15, so the strategy must be fromEnvironment."""
    settings = _load_settings()
    assert settings.get("autopep8.importStrategy") == "fromEnvironment"


def test_import_strategy_matches_interpreter_era() -> None:
    """While ``no_type_check_decorator`` is gone, bundled imports are unusable."""
    bundled_stack_usable = hasattr(typing, "no_type_check_decorator")
    if bundled_stack_usable:
        return
    assert _load_settings()["autopep8.importStrategy"] == "fromEnvironment"


def test_interpreter_points_at_workspace_venv() -> None:
    """The extension resolves packages from the workspace venv, so pin it there."""
    value = _load_settings()["python.defaultInterpreterPath"]
    assert isinstance(value, str)
    assert value.startswith("${workspaceFolder}")
    resolved = Path(_resolve_workspace_vars(value))
    assert resolved == _venv_python(), resolved
    assert resolved.is_file(), f"interpreter missing: {resolved}"


def test_autopep8_interpreter_overrides_stale_selection() -> None:
    """The server is spawned with this exact interpreter (beats workspace storage)."""
    configured = _load_settings()["autopep8.interpreter"]
    assert isinstance(configured, list)
    assert [Path(_resolve_workspace_vars(str(item))) for item in configured] == [_venv_python()]


def test_interpreter_matches_python_version_file() -> None:
    """``.python-version`` and the configured interpreter agree on major.minor."""
    expected = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    interpreter = Path(
        _resolve_workspace_vars(str(_load_settings()["python.defaultInterpreterPath"]))
    )
    result = subprocess.run(
        [
            str(interpreter),
            "-c",
            "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert result.stdout.strip() == ".".join(expected.split(".")[:2])


def test_autopep8_is_the_default_python_formatter() -> None:
    """Format Document / format-on-save route through autopep8."""
    python_section = _load_settings()["[python]"]
    assert isinstance(python_section, dict)
    assert python_section.get("editor.defaultFormatter") == "ms-python.autopep8"


def test_args_match_project_line_length() -> None:
    """autopep8's max line length mirrors [tool.ruff] line-length = 100."""
    args = _load_settings()["autopep8.args"]
    assert isinstance(args, list)
    assert "--max-line-length=100" in args


# ---------------------------------------------------------------------------
# formatter stack health (what "fromEnvironment" actually resolves)
# ---------------------------------------------------------------------------


def test_autopep8_importable_and_formats() -> None:
    """The env's autopep8 must import and produce pep8 output on this Python."""
    import autopep8

    fixed = autopep8.fix_code("x  =  1\n", options={"max_line_length": 100})
    assert fixed.strip() == "x = 1"


def test_pygls_supports_the_extensions_server_api() -> None:
    """The extension's lsp_server.py calls ``server.LanguageServer`` (pygls 1.x API)."""
    import pygls.server

    assert hasattr(pygls.server, "LanguageServer"), "pygls 2.x removed LanguageServer"


def test_extension_server_imports_succeed() -> None:
    """Import what bundled/tool/lsp_server.py imports, in its own sys.path order."""
    import pygls.server  # noqa: F401
    import pygls.uris  # noqa: F401
    import pygls.workspace  # noqa: F401
    from lsprotocol import types as lsp  # noqa: F401
