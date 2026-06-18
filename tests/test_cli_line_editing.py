from __future__ import annotations

import builtins
from types import SimpleNamespace
from typing import Any

from kimix.cli_impl.utils import _input
from kimix.cli_impl.core import _enable_line_editing


def test_enable_line_editing_imports_readline(monkeypatch):
    imported: list[str] = []
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "readline":
            imported.append(name)
            return SimpleNamespace()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    _enable_line_editing()

    assert imported == ["readline"]


def test_enable_line_editing_ignores_missing_readline(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any):
        if name == "readline":
            raise ImportError("readline unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    _enable_line_editing()


def test_input_prints_prompt_before_reading_empty_prompt(monkeypatch, capsys):
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return "typed"

    monkeypatch.setattr(builtins, "input", fake_input)

    assert _input("\n>>>>>>>>> Enter your prompt or command:\n", []) == "typed"

    assert prompts == [""]
    assert capsys.readouterr().out == "\n>>>>>>>>> Enter your prompt or command:\n"
