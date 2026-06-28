from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

from kimi_cli.wire.types import TextPart, ThinkPart, ToolCall, ToolCallPart

import kimix.base as base

prompt_mod = importlib.import_module("kimix.utils.prompt")


@dataclass
class FakeStatus:
    context_usage: float
    context_tokens: int


class FakeSession:
    def __init__(self, context_usage: float = 0.125, context_tokens: int = 1024) -> None:
        self.status = FakeStatus(context_usage=context_usage, context_tokens=context_tokens)
        self.cancelled = False
        self._cancel_event = None
        self._tmp_data = {}

    async def prompt(self, _prompt: str, *, merge_wire_messages: bool = False) -> Any:
        del merge_wire_messages
        yield TextPart(text="prompt output")

    def cancel(self) -> None:
        self.cancelled = True


def _capture_base_stream(monkeypatch: Any) -> list[str]:
    chunks: list[str] = []

    def print_func(*values: object, sep: str = " ", end: str = "\n", **_kwargs: Any) -> None:
        chunks.append(sep.join(str(value) for value in values) + end)

    monkeypatch.setattr(base, "_stream", base.PrintStream(print_func=print_func))
    monkeypatch.setattr(base, "_text_buffer", None)
    monkeypatch.setattr(base, "_quiet", False)
    monkeypatch.setattr(base, "_colorful_print", True)
    return chunks


def test_print_agent_json_prints_black_usage_when_text_switches_to_thinking(monkeypatch: Any) -> None:
    chunks = _capture_base_stream(monkeypatch)
    session = FakeSession(context_usage=0.125, context_tokens=1024)

    base.print_agent_json(TextPart(text="hello"), session)
    base.print_agent_json(TextPart(text=" world"), session)
    base.print_agent_json(ThinkPart(think="hmm"), session)

    output = "".join(chunks)

    assert output.count("Context usage: 12.5% (1024 tokens)") == 1
    assert "\x1b[38;5;245m==================== Context usage: 12.5% (1024 tokens) ========================\n\x1b[0m" in output
    assert "hello world" in output
    assert "\x1b[96m[Think] hmm\x1b[0m" in output


def test_print_agent_json_groups_tool_parts_before_tool_to_text_transition(monkeypatch: Any) -> None:
    chunks = _capture_base_stream(monkeypatch)
    session = FakeSession(context_usage=0.5, context_tokens=4096)
    tool_call = ToolCall(
        id="call-1",
        function=ToolCall.FunctionBody(name="Run", arguments='{"command": "pytest"}'),
    )

    base.print_agent_json(tool_call, session)
    base.print_agent_json(ToolCallPart(arguments_part='{"more": true}'), session)
    base.print_agent_json(TextPart(text="done"), session)

    output = "".join(chunks)

    assert output.count("Context usage: 50.0% (4096 tokens)") == 1
    assert "\x1b[38;5;245m==================== Context usage: 50.0% (4096 tokens) ========================\n\x1b[0m" in output
    assert "⚡ Run" in output
    assert "done" in output


def test_prompt_async_passes_session_to_print_agent_json(monkeypatch: Any) -> None:
    import asyncio

    calls: list[tuple[object, object, object]] = []
    session = FakeSession()

    def fake_print_agent_json(wire_msg: object, passed_session: object, output_function: object, format_output: bool = False) -> None:
        calls.append((wire_msg, passed_session, output_function, format_output))

    monkeypatch.setattr(prompt_mod, "print_agent_json", fake_print_agent_json)
    monkeypatch.setattr(prompt_mod.base._stream, "colorful_print_word", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_mod.base._stream, "print_word", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_mod, "_print_usage", lambda *args, **kwargs: None)

    asyncio.run(prompt_mod.prompt_async("hello", session=session))

    assert len(calls) == 1
    assert isinstance(calls[0][0], TextPart)
    assert calls[0][1] is session
    assert calls[0][2] is None
    assert calls[0][3] is False


def test_print_agent_json_format_output_buffers_text_until_mode_change(monkeypatch: Any) -> None:
    chunks = _capture_base_stream(monkeypatch)
    session = FakeSession()

    base.print_agent_json(TextPart(text="hello "), session, format_output=True)
    base.print_agent_json(TextPart(text="world"), session, format_output=True)
    assert "hello world" not in "".join(chunks)

    base.print_agent_json(ThinkPart(think="hmm"), session, format_output=True)
    output = "".join(chunks)
    assert "hello world" in output
    assert "[Think] hmm" in output


def test_print_agent_json_format_output_flushes_remaining_text_at_end(monkeypatch: Any) -> None:
    chunks = _capture_base_stream(monkeypatch)
    session = FakeSession()

    base.print_agent_json(TextPart(text="hello"), session, format_output=True)
    assert "hello" not in "".join(chunks)

    base.print_agent_json_flush_text()
    assert "hello" in "".join(chunks)


def test_print_agent_json_format_output_still_calls_output_function(monkeypatch: Any) -> None:
    monkeypatch.setattr(base, "_text_buffer", None)
    session = FakeSession()
    received: list[str] = []

    def output_function(text: str, _msg_type: object) -> None:
        received.append(text)

    base.print_agent_json(TextPart(text="chunk1"), session, output_function=output_function, format_output=True)
    base.print_agent_json(TextPart(text="chunk2"), session, output_function=output_function, format_output=True)

    assert received == ["chunk1", "chunk2"]
