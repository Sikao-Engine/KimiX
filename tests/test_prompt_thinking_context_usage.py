"""``Context usage`` banners must survive collapsed reasoning.

The banner is a *block-switch* marker (text → thinking → tool-calling), not
reasoning text.  ``show_thinking_stream = false`` collapses the reasoning body
to a ``thinking...`` indicator that renders ``ThinkPart`` messages itself
instead of going through ``print_agent_json`` — which used to swallow the
banner for the switch *into* thinking, and, because the block type was never
recorded, the banner for the switch back *out* of it too.  Every block switch
must print its banner in both display modes.
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from kimi_cli.wire.types import ToolCall
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
    """Minimal session double playing a scripted wire-message stream."""

    def __init__(self, parts: list[Any], *, show_thinking_stream: bool | None = None) -> None:
        self.status = FakeStatus()
        self.cancelled = False
        self._cancel_event = None
        self._tmp_data: dict[str, Any] = {}
        self._parts = parts
        if show_thinking_stream is not None:
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


def _usage_banners(plain: str) -> list[str]:
    return [line for line in plain.splitlines() if "Context usage:" in line]


async def _run_attempts(session: FakeSession, *, format_output: bool = False) -> None:
    await prompt_mod._run_prompt_attempts(
        session,
        "hi",
        output_function=None,
        cancel_callable=None,
        merge_wire_messages=False,
        info_print=False,
        format_output=format_output,
        deadline=None,
    )
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Prompt loop: both sides of the collapsed thinking block keep their banner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collapsed_reasoning_still_prints_block_switch_banners(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [
            TextPart(text="alpha text"),
            ThinkPart(think="secret chain of thought"),
            TextPart(text="omega text"),
        ],
        show_thinking_stream=False,
    )
    session.status.context_usage = 0.42
    session.status.context_tokens = 4200

    await _run_attempts(session)

    plain = _plain(chunks)
    banners = _usage_banners(plain)
    # text → thinking and thinking → text: one banner each, exactly like the
    # fully streamed reasoning path.
    assert len(banners) == 2
    assert all("Context usage: 42.0% (4200 tokens)" in line for line in banners)
    # The reasoning body itself stays hidden, but the switches around it show.
    assert "secret chain of thought" not in plain
    assert plain.index(banners[0]) < plain.index("thinking...")
    # Both banners share their text, so locate the second one from the end.
    assert plain.index("done,") < plain.rindex(banners[1]) < plain.index("omega text")


@pytest.mark.asyncio
async def test_usage_banner_count_matches_streamed_reasoning_mode(monkeypatch: Any) -> None:
    parts = [
        TextPart(text="alpha text"),
        ThinkPart(think="r1"),
        ThinkPart(think="r2"),
        TextPart(text="omega text"),
    ]
    counts: dict[bool, int] = {}
    for show_thinking_stream in (True, False):
        chunks = _capture_stream(monkeypatch)
        session = FakeSession(parts, show_thinking_stream=show_thinking_stream)
        session.status.context_usage = 0.5
        session.status.context_tokens = 2048
        await _run_attempts(session)
        counts[show_thinking_stream] = len(_usage_banners(_plain(chunks)))

    # Two blocks (text, thinking) → 2 switches; collapsing must not change it.
    assert counts[True] == 2
    assert counts[False] == counts[True]


@pytest.mark.asyncio
async def test_banner_precedes_indicator_after_buffered_text_is_flushed(
    monkeypatch: Any,
) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [
            TextPart(text="buffered answer"),
            ThinkPart(think="reasoning"),
            TextPart(text="tail"),
        ],
        show_thinking_stream=False,
    )
    session.status.context_usage = 0.25
    session.status.context_tokens = 512

    await _run_attempts(session, format_output=True)

    plain = _plain(chunks)
    # Block-switch order: flushed text → banner → ``thinking...``.
    assert (
        plain.index("buffered answer")
        < plain.index("Context usage:")
        < plain.index("thinking...")
    )


@pytest.mark.asyncio
async def test_tool_call_after_collapsed_reasoning_prints_its_banner(
    monkeypatch: Any,
) -> None:
    """thinking → tool-calling is a block switch too, hidden reasoning or not."""
    chunks = _capture_stream(monkeypatch)
    tool_call = ToolCall(
        id="call-1",
        function=ToolCall.FunctionBody(name="Run", arguments='{"command": "pytest"}'),
    )
    session = FakeSession(
        [ThinkPart(think="plan the command"), tool_call],
        show_thinking_stream=False,
    )
    session.status.context_usage = 0.75
    session.status.context_tokens = 8192
    stream_mod.print_transition_usage(session, MessageType.Text)

    await _run_attempts(session)

    plain = _plain(chunks)
    banners = _usage_banners(plain)
    assert len(banners) == 2  # text → thinking, thinking → tool-calling
    assert "plan the command" not in plain
    # Residual name resolution aside, the banner lands before the ⚡ header.
    assert plain.index("done,") < plain.rindex(banners[-1]) < plain.index("⚡")


# ---------------------------------------------------------------------------
# Indicator unit level
# ---------------------------------------------------------------------------

def test_indicator_prints_banner_and_records_thinking_block(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([], show_thinking_stream=False)
    session.status.context_usage = 0.125
    session.status.context_tokens = 1024
    # A pre-existing text block is what makes the switch into thinking visible.
    stream_mod.print_transition_usage(session, MessageType.Text)

    indicator = prompt_mod._ThinkingIndicator(session, None, enabled=True)
    assert indicator.absorb(ThinkPart(think="secret")) is True
    indicator.finish()

    plain = _plain(chunks)
    assert plain.count("Context usage: 12.5% (1024 tokens)") == 1
    assert plain.index("Context usage:") < plain.index("thinking...")
    # Recording the block type is what makes the *next* switch print its banner.
    recorded = getattr(session, stream_mod._PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR)
    assert recorded is MessageType.Thinking


def test_indicator_banner_is_not_repeated_for_block_continuations(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([], show_thinking_stream=False)
    session.status.context_usage = 0.3
    session.status.context_tokens = 700
    stream_mod.print_transition_usage(session, MessageType.Text)

    indicator = prompt_mod._ThinkingIndicator(session, None, enabled=True)
    indicator.absorb(ThinkPart(think="r1"))
    indicator.absorb(ThinkPart(think="r2"))
    indicator.absorb(ThinkPart(think="r3"))
    indicator.finish()

    assert _plain(chunks).count("Context usage:") == 1


def test_print_transition_usage_ignores_missing_session_or_type(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([])

    stream_mod.print_transition_usage(None, MessageType.Thinking)
    stream_mod.print_transition_usage(session, None)

    assert chunks == []
    assert not hasattr(session, stream_mod._PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR)


def test_private_transition_usage_name_still_resolves() -> None:
    assert stream_mod._print_transition_usage is stream_mod.print_transition_usage


# ---------------------------------------------------------------------------
# Fresh session: no placeholder 0.0% (0 tokens) banner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_session_prints_no_zero_usage_banner(monkeypatch: Any) -> None:
    """The first block switches of a brand-new session must stay banner-free.

    Until the first LLM step reports its token count the session reports
    ``0.0% (0 tokens)``; printing that opened every session with a meaningless
    ``==================== Context usage: 0.0% (0 tokens) ====...`` line.
    """
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [
            TextPart(text="alpha text"),
            ThinkPart(think="secret chain of thought"),
            TextPart(text="omega text"),
        ],
        show_thinking_stream=False,
    )
    assert session.status.context_tokens == 0

    await _run_attempts(session)

    plain = _plain(chunks)
    assert _usage_banners(plain) == []
    assert "0.0% (0 tokens)" not in plain
    # The block switch itself is still tracked, so the next switch after the
    # provider reports usage prints its banner.
    assert getattr(session, stream_mod._PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR) is MessageType.Text


def test_banner_appears_once_usage_is_known(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([])
    # A fresh session: block switch with no measurement yet -> no banner.
    stream_mod.print_transition_usage(session, MessageType.Text)
    stream_mod.print_transition_usage(session, MessageType.Thinking)
    assert chunks == []

    # The first LLM step completes and reports real usage...
    session.status.context_usage = 0.5
    session.status.context_tokens = 4096
    # ...so the *next* switch renders the banner, with the real numbers.
    stream_mod.print_transition_usage(session, MessageType.Text)

    banners = _usage_banners(_plain(chunks))
    assert len(banners) == 1
    assert "Context usage: 50.0% (4096 tokens)" in banners[0]
    assert "0.0% (0 tokens)" not in _plain(chunks)


def test_usage_banner_skipped_for_unknown_negative_ratio(monkeypatch: Any) -> None:
    """``-1.0`` is the shared "unknown" sentinel: show no banner for it."""
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([])
    session.status.context_usage = -1.0
    session.status.context_tokens = 0

    stream_mod.print_transition_usage(session, MessageType.Text)
    stream_mod.print_transition_usage(session, MessageType.Thinking)

    assert chunks == []


def test_context_usage_known_handles_missing_or_broken_status() -> None:
    assert stream_mod.context_usage_known(FakeSession([])) is False

    known = FakeSession([])
    known.status.context_usage = 0.25
    known.status.context_tokens = 512
    assert stream_mod.context_usage_known(known) is True

    no_status = SimpleNamespace()
    assert stream_mod.context_usage_known(no_status) is False

    broken = SimpleNamespace(status=SimpleNamespace(context_usage="?", context_tokens="?"))
    assert stream_mod.context_usage_known(broken) is False

    unmeasured = SimpleNamespace(status=SimpleNamespace())
    assert stream_mod.context_usage_known(unmeasured) is False
