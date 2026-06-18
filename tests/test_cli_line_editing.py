from __future__ import annotations

import builtins
from types import SimpleNamespace
from typing import Any

from kimix.cli_impl import utils
from kimix.cli_impl.core import _enable_line_editing
from kimix.cli_impl.utils import _get_completion_candidates


def _reset_readline_cache() -> None:
    """Reset the readline import cache so _ensure_readline() re-runs imports."""
    utils._READLINE_MOD = None  # type: ignore[attr-defined]
    utils._READLINE_ATTEMPTED = False  # type: ignore[attr-defined]


def test_enable_line_editing_imports_readline(monkeypatch):
    _reset_readline_cache()
    imported: list[str] = []
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name in ("pyreadline3", "pyreadline", "readline"):
            imported.append(name)
            # Provide the standard readline API so the first candidate is accepted.
            return SimpleNamespace(
                set_completer=None,
                set_completer_delims=None,
                parse_and_bind=None,
                get_line_buffer=None,
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    _enable_line_editing()

    assert imported == ["readline"]


def test_enable_line_editing_falls_back_to_pyreadline3(monkeypatch):
    _reset_readline_cache()
    imported: list[str] = []
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name in ("pyreadline3", "pyreadline", "readline"):
            imported.append(name)
            if name == "readline":
                # readline is available but lacks the API, so we should fall back.
                return SimpleNamespace()
            return SimpleNamespace(
                set_completer=None,
                set_completer_delims=None,
                parse_and_bind=None,
                get_line_buffer=None,
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    _enable_line_editing()

    assert imported == ["readline", "pyreadline3"]


def test_enable_line_editing_ignores_missing_readline(monkeypatch):
    _reset_readline_cache()
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name in ("pyreadline3", "pyreadline", "readline"):
            raise ImportError(f"{name} unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    _enable_line_editing()


def test_completion_lists_all_commands():
    candidates = _get_completion_candidates("/")
    assert "/help" in candidates
    assert "/exit" in candidates
    assert "/cd" in candidates


def test_completion_filters_command_name():
    candidates = _get_completion_candidates("/c")
    assert "/cd" in candidates
    assert "/clear" in candidates
    assert "/help" not in candidates


def test_completion_no_colon_for_argless_commands():
    candidates = _get_completion_candidates("/cle")
    assert candidates == ["/clear"]


def test_completion_bool_values():
    candidates = _get_completion_candidates("/cot:")
    assert "/cot:on" in candidates
    assert "/cot:off" in candidates


def test_completion_non_slash_input_returns_empty():
    assert _get_completion_candidates("hello") == []
