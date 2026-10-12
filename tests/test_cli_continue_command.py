"""Tests for the ``/continue`` CLI command (``commands._cmd_continue``)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import kimix.cli_impl.commands as commands


def _stub_session(
    *,
    done: bool | None = None,
    todos: list[Any] | None = None,
    context_usage: float = 0.5,
) -> SimpleNamespace:
    """Session stub covering the root-session path of _get_session_todos."""
    session = SimpleNamespace(
        _cli=SimpleNamespace(
            session=SimpleNamespace(state=SimpleNamespace(todos=todos)),
            soul=SimpleNamespace(
                context=SimpleNamespace(history=[object()], token_count=10),
            ),
        ),
        status=SimpleNamespace(context_usage=context_usage, context_tokens=100),
    )
    if done is not None:
        session._done = done  # type: ignore[attr-defined]
    return session


class _PromptSpy:
    def __init__(self, exc: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.exc = exc

    def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        if self.exc is not None:
            raise self.exc


def test_cmd_continue_requires_active_session(monkeypatch, capsys) -> None:
    monkeypatch.setattr(commands, "get_default_session", lambda: None)
    result = commands._cmd_continue(["continue"], [])
    assert result == (None, False)
    assert "No active session" in capsys.readouterr().out


def test_cmd_continue_requires_non_empty_context(monkeypatch, capsys) -> None:
    spy = _PromptSpy()
    empty = _stub_session(context_usage=0.0)
    empty._cli.soul.context.token_count = 0
    empty._cli.soul.context.history = []
    monkeypatch.setattr(commands, "get_default_session", lambda: empty)
    monkeypatch.setattr(commands, "prompt", spy)
    result = commands._cmd_continue(["continue"], [])
    assert result == (None, False)
    assert "Context is empty" in capsys.readouterr().out
    assert spy.calls == []


def test_cmd_continue_skips_when_done_flag_true(monkeypatch, capsys) -> None:
    """The done flag wins even when the todo list has pending items."""
    spy = _PromptSpy()
    session = _stub_session(done=True, todos=[{"title": "x", "status": "pending"}])
    monkeypatch.setattr(commands, "get_default_session", lambda: session)
    monkeypatch.setattr(commands, "prompt", spy)
    result = commands._cmd_continue(["continue"], [])
    assert result == (None, False)
    assert "already marked as done" in capsys.readouterr().out
    assert spy.calls == []


def test_cmd_continue_skips_when_all_todos_done(monkeypatch, capsys) -> None:
    """Fallback: no _done attribute but all todos done -> skip."""
    spy = _PromptSpy()
    session = _stub_session(
        done=None,
        todos=[
            {"title": "a", "status": "done", "children": [{"title": "b", "status": "done"}]},
            {"title": "c", "status": "done"},
        ],
    )
    monkeypatch.setattr(commands, "get_default_session", lambda: session)
    monkeypatch.setattr(commands, "prompt", spy)
    result = commands._cmd_continue(["continue"], [])
    assert result == (None, False)
    assert "already marked as done" in capsys.readouterr().out
    assert spy.calls == []


def test_cmd_continue_prompts_when_done_flag_false_and_unfinished_todos(monkeypatch) -> None:
    spy = _PromptSpy()
    session = _stub_session(done=False, todos=[{"title": "x", "status": "pending"}])
    monkeypatch.setattr(commands, "get_default_session", lambda: session)
    monkeypatch.setattr(commands, "prompt", spy)
    result = commands._cmd_continue(["continue"], [])
    assert result == (None, False)
    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["session"] is session
    assert call["format_output"] is True
    assert isinstance(call["prompt_str"], str) and len(call["prompt_str"]) > 0


def test_cmd_continue_prompts_when_no_todos_and_flag_false(monkeypatch) -> None:
    spy = _PromptSpy()
    session = _stub_session(done=None, todos=None)
    monkeypatch.setattr(commands, "get_default_session", lambda: session)
    monkeypatch.setattr(commands, "prompt", spy)
    result = commands._cmd_continue(["continue"], [])
    assert result == (None, False)
    assert len(spy.calls) == 1


def test_cmd_continue_handles_prompt_exception(monkeypatch, capsys) -> None:
    spy = _PromptSpy(exc=RuntimeError("backend down"))
    session = _stub_session(done=False, todos=None)
    monkeypatch.setattr(commands, "get_default_session", lambda: session)
    monkeypatch.setattr(commands, "prompt", spy)
    result = commands._cmd_continue(["continue"], [])
    assert result == (None, False)
    assert "Continue failed" in capsys.readouterr().out


def test_continue_command_registered() -> None:
    assert commands._command_map["continue"] is commands._cmd_continue


def test_continue_prompt_is_terse() -> None:
    """The synthetic continue message must stay a cheap nudge, not an essay.

    It is appended to the session context on every /continue, so its token
    cost is permanent. Guard against regression to the previous ~40-word
    prompt.
    """
    prompt = commands._CONTINUE_PROMPT
    assert isinstance(prompt, str) and prompt.strip()
    assert len(prompt) <= 160, f"continue prompt grew too large: {len(prompt)} chars"
    assert len(prompt.split()) <= 25, f"continue prompt grew too wordy: {len(prompt.split())} words"
