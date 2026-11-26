"""Collapsed reasoning display for ``show_thinking_stream = false``.

With streaming disabled the prompt loop must not dump the raw reasoning text:
it prints ``thinking...`` once, one liveness dot every few seconds while the
model is still thinking, and a one-line ``done, <n> tokens`` summary when the
block ends.  ``output_function`` consumers (SSE feeds, CoT extraction) keep
receiving every reasoning chunk — the setting is display-only.
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from kimi_cli.config import Config
from kimi_cli.utils.tokens import count_tokens
from kosong.message import TextPart, ThinkPart

import kimix.base as base
import kimix.ui.printing as printing_mod
import kimix.ui.stream as stream_mod
from kimix.ui.printing import MessageType

prompt_mod = importlib.import_module("kimix.utils.prompt")


@dataclass
class FakeStatus:
    context_usage: float = 0.0
    context_tokens: int = 0


class FakeSession:
    """Minimal ``Session`` double playing a scripted wire-message stream."""

    def __init__(self, parts: list[Any], *, show_thinking_stream: bool | None = None) -> None:
        self.status = FakeStatus()
        self.cancelled = False
        self._cancel_event = None
        self._tmp_data: dict[str, Any] = {}
        self._parts = parts
        if show_thinking_stream is not None:
            # Mirrors the live path kimix.utils.prompt resolves the setting from:
            # session._cli.soul.agent.runtime.config
            self._cli = SimpleNamespace(
                soul=SimpleNamespace(
                    agent=SimpleNamespace(
                        runtime=SimpleNamespace(
                            config=SimpleNamespace(show_thinking_stream=show_thinking_stream)
                        )
                    )
                )
            )

    async def prompt(self, _prompt_str: str, *, merge_wire_messages: bool = False):
        del merge_wire_messages
        for part in self._parts:
            yield part

    def cancel(self) -> None:
        self.cancelled = True


def _capture_stream(monkeypatch: Any) -> list[str]:
    """Redirect the shared PrintStream into a list of raw (ANSI-carrying) chunks."""
    chunks: list[str] = []

    def print_func(*values: object, sep: str = " ", end: str = "\n", **_kwargs: Any) -> None:
        chunks.append(sep.join(str(value) for value in values) + end)

    new_stream = base.PrintStream(print_func=print_func)
    monkeypatch.setattr(base, "_stream", new_stream)
    monkeypatch.setattr(stream_mod, "_stream", new_stream)
    monkeypatch.setattr(stream_mod, "_text_buffer", None)
    monkeypatch.setattr(base, "_quiet", False)
    monkeypatch.setattr(base, "_colorful_print", True)
    monkeypatch.setattr(prompt_mod, "_print_usage", lambda *args, **kwargs: None)
    return chunks


def _plain(chunks: list[str]) -> str:
    return printing_mod._strip_ansi("".join(chunks))


async def _run_attempts(
    session: FakeSession,
    output_function: Any = None,
    *,
    format_output: bool = False,
) -> None:
    await prompt_mod._run_prompt_attempts(
        session,
        "hi",
        output_function=output_function,
        cancel_callable=None,
        merge_wire_messages=False,
        info_print=False,
        format_output=format_output,
        deadline=None,
    )
    # Let a cancelled heartbeat task settle before the test loop closes.
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Config wiring
# ---------------------------------------------------------------------------


def test_config_defaults_to_collapsed_reasoning() -> None:
    assert Config.model_fields["show_thinking_stream"].default is False
    assert Config().show_thinking_stream is False


def test_resolve_reads_live_config() -> None:
    assert prompt_mod._resolve_show_thinking_stream(
        FakeSession([], show_thinking_stream=True)
    ) is True
    assert prompt_mod._resolve_show_thinking_stream(
        FakeSession([], show_thinking_stream=False)
    ) is False


def test_resolve_falls_back_to_custom_config() -> None:
    class _Session:
        def get_custom_config(self) -> dict[str, Any]:
            return {"show_thinking_stream": True}

    assert prompt_mod._resolve_show_thinking_stream(_Session()) is True


def test_resolve_defaults_to_off_for_unknown_sessions() -> None:
    class _Bare:
        pass

    class _NonBool:
        _cli = SimpleNamespace(
            soul=SimpleNamespace(
                agent=SimpleNamespace(
                    runtime=SimpleNamespace(
                        config=SimpleNamespace(show_thinking_stream="yes")
                    )
                )
            )
        )

    assert prompt_mod._resolve_show_thinking_stream(_Bare()) is False
    # A non-bool config value must never re-enable full reasoning streaming.
    assert prompt_mod._resolve_show_thinking_stream(_NonBool()) is False


# ---------------------------------------------------------------------------
# Indicator unit tests
# ---------------------------------------------------------------------------


def test_indicator_collapses_reasoning_and_reports_tokens(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    indicator = prompt_mod._ThinkingIndicator(FakeSession([]), None, enabled=True)
    reasoning = "step one. " * 40

    assert indicator.absorb(ThinkPart(think=reasoning)) is True
    assert indicator.active is True
    indicator.finish()
    assert indicator.active is False

    plain = _plain(chunks)
    assert plain == f"thinking... done, {count_tokens(reasoning):,} tokens\n"
    assert "step one." not in plain


def test_indicator_passes_non_reasoning_messages_through(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    indicator = prompt_mod._ThinkingIndicator(FakeSession([]), None, enabled=True)

    assert indicator.absorb(TextPart(text="hi")) is False
    assert indicator.absorb(ThinkPart(think="hmm")) is True
    # Closing a block on the next visible message keeps the summary in order.
    assert indicator.absorb(TextPart(text="bye")) is False
    assert indicator.active is False

    plain = _plain(chunks)
    assert plain.startswith("thinking...")
    assert f"done, {count_tokens('hmm'):,} tokens\n" in plain


def test_indicator_ignores_encrypted_reasoning(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    indicator = prompt_mod._ThinkingIndicator(FakeSession([]), None, enabled=True)

    assert indicator.absorb(ThinkPart(think="secret", encrypted="sig")) is False
    assert indicator.active is False
    assert chunks == []


def test_indicator_disabled_is_a_noop(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    indicator = prompt_mod._ThinkingIndicator(FakeSession([]), None, enabled=False)

    assert indicator.absorb(ThinkPart(think="streamed normally")) is False
    assert indicator.active is False
    assert chunks == []


def test_indicator_finish_is_idempotent(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    indicator = prompt_mod._ThinkingIndicator(FakeSession([]), None, enabled=True)

    indicator.finish()
    indicator.finish()
    assert chunks == []


@pytest.mark.asyncio
async def test_indicator_prints_one_dot_per_interval(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    monkeypatch.setattr(prompt_mod, "_THINKING_DOT_INTERVAL", 0.02)
    indicator = prompt_mod._ThinkingIndicator(FakeSession([]), None, enabled=True)

    indicator.absorb(ThinkPart(think="thinking hard"))
    await asyncio.sleep(0.1)
    dots_while_thinking = indicator._dots
    indicator.finish()
    await asyncio.sleep(0.05)

    assert 1 <= dots_while_thinking <= prompt_mod._THINKING_DOTS_PER_LINE
    # The heartbeat must stop as soon as the block ends.
    assert indicator._dots == dots_while_thinking
    plain = _plain(chunks)
    assert plain.count(".") == 3 + dots_while_thinking  # "thinking..." + one dot per tick
    assert "done," in plain


@pytest.mark.asyncio
async def test_indicator_starts_a_new_line_every_dot_batch(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    monkeypatch.setattr(prompt_mod, "_THINKING_DOT_INTERVAL", 0.005)
    monkeypatch.setattr(prompt_mod, "_THINKING_DOTS_PER_LINE", 2)
    indicator = prompt_mod._ThinkingIndicator(FakeSession([]), None, enabled=True)

    indicator.absorb(ThinkPart(think="long reasoning"))
    await asyncio.sleep(0.06)
    indicator.finish()
    await asyncio.sleep(0.05)

    plain = _plain(chunks)
    lines = [line for line in plain.splitlines() if line.strip()]
    # One short "thinking" group per batch of dots — never one endless line.
    assert plain.count("thinking...") == len(lines)
    assert len(lines) == 1 + (indicator._dots - 1) // 2
    assert all(len(line) <= len("thinking... done, 1,234 tokens") for line in lines)


# ---------------------------------------------------------------------------
# Prompt-loop integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_collapses_reasoning_when_stream_disabled(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    received: list[tuple[str, MessageType]] = []

    def output_function(text: str, msg_type: MessageType) -> None:
        received.append((text, msg_type))

    reasoning = "secret chain of thought " * 20
    session = FakeSession(
        [
            ThinkPart(think=reasoning[:120]),
            ThinkPart(think=reasoning[120:]),
            TextPart(text="final answer"),
        ],
        show_thinking_stream=False,
    )

    await _run_attempts(session, output_function=output_function)

    plain = _plain(chunks)
    assert "secret chain of thought" not in plain
    assert plain.count("thinking...") == 1
    assert f" done, {count_tokens(reasoning):,} tokens\n" in plain
    assert "final answer" in plain
    # The summary is printed before the visible answer.
    assert plain.index("done,") < plain.index("final answer")
    # Data consumers are not affected by the display setting.
    assert [t for t, ty in received if ty == MessageType.Thinking] == [
        reasoning[:120],
        reasoning[120:],
    ]


@pytest.mark.asyncio
async def test_prompt_flushes_buffered_text_before_the_indicator(monkeypatch: Any) -> None:
    """Buffered assistant text must be rendered before ``thinking...`` appears."""
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [
            TextPart(text="buffered answer"),
            ThinkPart(think="reasoning"),
            TextPart(text="tail"),
        ],
        show_thinking_stream=False,
    )

    await _run_attempts(session, format_output=True)

    plain = _plain(chunks)
    assert "buffered answer" in plain
    assert plain.index("buffered answer") < plain.index("thinking...")


@pytest.mark.asyncio
async def test_prompt_streams_reasoning_when_enabled(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [ThinkPart(think="visible thought"), TextPart(text="answer")],
        show_thinking_stream=True,
    )

    await _run_attempts(session)

    plain = _plain(chunks)
    assert "[Think] visible thought" in plain
    assert "thinking..." not in plain
    assert "done," not in plain


@pytest.mark.asyncio
async def test_prompt_keeps_loop_detection_while_reasoning_is_collapsed(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("KIMIX_LOOP_DETECTION_ENABLED", "1")
    monkeypatch.setenv("KIMIX_LOOP_WORD_THRESHOLD", "5")
    chunks = _capture_stream(monkeypatch)

    session = FakeSession(
        [ThinkPart(think="again ")] * 60,
        show_thinking_stream=False,
    )

    await _run_attempts(session)

    plain = _plain(chunks)
    assert session.cancelled
    assert "Loop detected; cancelling session." in plain
    assert plain.count("thinking...") == 1
    assert "again again" not in plain
