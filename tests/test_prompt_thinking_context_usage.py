"""``Context usage`` banners close the assistant output, they never open a block.

The banner is a *block-switch* marker (text → thinking → tool-calling), printed
only once the assistant's printed output is done: switching *into* reasoning is
silent and switching *into* text is silent as well.  A thinking → text →
tool-calling step therefore shows a single banner, right before the tool call,
instead of one per boundary — the duplicate the CLI used to print as
``banner → text → banner``.  ``show_thinking_stream = false`` collapses the
reasoning body to a ``thinking...`` indicator that renders ``ThinkPart``
messages itself instead of going through ``print_agent_json``; it must still
record the reasoning block type, otherwise the following tool switch — the only
place the banner is printed — would be swallowed too.
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


def _tool_call(
    name: str = "Run",
    arguments: str = '{"command": "pytest"}',
    call_id: str = "call-1",
) -> ToolCall:
    return ToolCall(id=call_id, function=ToolCall.FunctionBody(name=name, arguments=arguments))


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
# Prompt loop: the banner lands after reasoning, in both display modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_text_tool_step_prints_a_single_banner_after_text(monkeypatch: Any) -> None:
    """The reported duplicate: ``banner → text → banner`` around one step.

    Reasoning and text both belong to the assistant's printed output, so none of
    them may be *opened* by a banner: the only banner of a thinking → text →
    tool-calling step closes the text block and lands right before the tool call.
    """
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [
            TextPart(text="alpha text"),
            ThinkPart(think="secret chain of thought"),
            TextPart(text="Now the commit gate (plan freeze, write scope, syntax, unit re-run):"),
            _tool_call(),
        ],
        show_thinking_stream=False,
    )
    session.status.context_usage = 0.42
    session.status.context_tokens = 4200

    await _run_attempts(session)

    plain = _plain(chunks)
    banners = _usage_banners(plain)
    # Exactly one banner for the whole step.
    assert len(banners) == 1
    assert "Context usage: 42.0% (4200 tokens)" in banners[0]
    # The reasoning body itself stays hidden, and no banner opens it.
    assert "secret chain of thought" not in plain
    # Indicator → summary → text → banner → ⚡ tool.
    assert (
        plain.index("thinking...")
        < plain.index("done,")
        < plain.index("Now the commit gate")
        < plain.index(banners[0])
        < plain.index("⚡")
    )


@pytest.mark.asyncio
async def test_usage_banner_count_matches_streamed_reasoning_mode(monkeypatch: Any) -> None:
    parts = [
        TextPart(text="alpha text"),
        ThinkPart(think="r1"),
        ThinkPart(think="r2"),
        TextPart(text="omega text"),
        _tool_call(),
    ]
    counts: dict[bool, int] = {}
    positions: dict[bool, str] = {}
    for show_thinking_stream in (True, False):
        chunks = _capture_stream(monkeypatch)
        session = FakeSession(parts, show_thinking_stream=show_thinking_stream)
        session.status.context_usage = 0.5
        session.status.context_tokens = 2048
        await _run_attempts(session)
        plain = _plain(chunks)
        counts[show_thinking_stream] = len(_usage_banners(plain))
        positions[show_thinking_stream] = plain

    # One banner for the whole step: continuing the same reasoning block adds
    # nothing, and completing the text block does not add a second one either.
    # Collapsing must not change it.
    assert counts[True] == 1
    assert counts[False] == counts[True]
    for plain in positions.values():
        assert plain.index("alpha text") < plain.index("omega text") < plain.index("Context usage:")


@pytest.mark.asyncio
async def test_banner_follows_indicator_after_buffered_text_is_flushed(
    monkeypatch: Any,
) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [
            TextPart(text="buffered answer"),
            ThinkPart(think="reasoning"),
            TextPart(text="tail"),
            _tool_call(),
        ],
        show_thinking_stream=False,
    )
    session.status.context_usage = 0.25
    session.status.context_tokens = 512

    await _run_attempts(session, format_output=True)

    plain = _plain(chunks)
    # Block-switch order: flushed text → ``thinking...`` → summary → tail →
    # banner → tool call.
    assert (
        plain.index("buffered answer")
        < plain.index("thinking...")
        < plain.index("done,")
        < plain.index("tail")
        < plain.index("Context usage:")
        < plain.index("⚡")
    )


@pytest.mark.asyncio
async def test_tool_call_after_collapsed_reasoning_prints_its_banner(
    monkeypatch: Any,
) -> None:
    """thinking → tool-calling is a block switch too, hidden reasoning or not."""
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [ThinkPart(think="plan the command"), _tool_call()],
        show_thinking_stream=False,
    )
    session.status.context_usage = 0.75
    session.status.context_tokens = 8192
    stream_mod.print_transition_usage(session, MessageType.Text)

    await _run_attempts(session)

    plain = _plain(chunks)
    banners = _usage_banners(plain)
    assert len(banners) == 1  # thinking → tool-calling only
    assert "plan the command" not in plain
    # The banner lands after the reasoning summary and before the ⚡ header,
    # i.e. nothing opened the reasoning block with it.
    assert plain.index("done,") < plain.index(banners[0]) < plain.index("⚡")


# ---------------------------------------------------------------------------
# Indicator unit level
# ---------------------------------------------------------------------------

def test_indicator_opens_thinking_block_without_banner(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([], show_thinking_stream=False)
    session.status.context_usage = 0.125
    session.status.context_tokens = 1024
    # A pre-existing text block turns the switch into thinking into a real one.
    stream_mod.print_transition_usage(session, MessageType.Text)

    indicator = prompt_mod._ThinkingIndicator(session, None, enabled=True)
    assert indicator.absorb(ThinkPart(think="secret")) is True
    indicator.finish()

    plain = _plain(chunks)
    # Opening a reasoning block prints no banner — only the indicator itself.
    assert "Context usage:" not in plain
    assert plain.index("thinking...") < plain.index("done,")
    recorded = getattr(session, stream_mod._PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR)
    assert recorded is MessageType.Thinking
    # Recording the block type is what makes the next *tool* switch print the
    # banner.  Entering text after the reasoning stays silent too: the banner
    # belongs to the end of the assistant output, not to each boundary.
    stream_mod.print_transition_usage(session, MessageType.Text)
    plain = _plain(chunks)
    assert "Context usage:" not in plain
    stream_mod.print_transition_usage(session, MessageType.ToolCalling)
    plain = _plain(chunks)
    assert plain.count("Context usage: 12.5% (1024 tokens)") == 1
    assert plain.index("done,") < plain.index("Context usage:")


def test_indicator_never_prints_banner_while_reasoning(monkeypatch: Any) -> None:
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

    plain = _plain(chunks)
    assert "Context usage:" not in plain
    assert plain.count("thinking...") == 1


def test_print_transition_usage_ignores_missing_session_or_type(monkeypatch: Any) -> None:
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([])

    stream_mod.print_transition_usage(None, MessageType.Thinking)
    stream_mod.print_transition_usage(session, None)

    assert chunks == []
    assert not hasattr(session, stream_mod._PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR)


def test_switch_into_thinking_never_prints_a_banner(monkeypatch: Any) -> None:
    """The banner only ever closes the assistant output, never opens a block."""
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([])
    session.status.context_usage = 0.5
    session.status.context_tokens = 4096

    stream_mod.print_transition_usage(session, MessageType.Text)
    stream_mod.print_transition_usage(session, MessageType.Thinking)
    assert _plain(chunks) == ""
    assert getattr(session, stream_mod._PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR) is MessageType.Thinking

    # ...the reasoning → text switch right after it stays silent (text is not
    # opened by a banner)...
    stream_mod.print_transition_usage(session, MessageType.Text)
    assert _plain(chunks) == ""

    # ...and only the following text → tool-calling switch prints one.
    stream_mod.print_transition_usage(session, MessageType.ToolCalling)
    banners = _usage_banners(_plain(chunks))
    assert len(banners) == 1
    assert "Context usage: 50.0% (4096 tokens)" in banners[0]


def test_text_block_is_never_opened_by_a_banner(monkeypatch: Any) -> None:
    """Switching into text is silent: the banner closes text, it never opens it."""
    chunks = _capture_stream(monkeypatch)
    session = FakeSession([])
    session.status.context_usage = 0.42
    session.status.context_tokens = 4200

    # A thinking → text step reports its usage only once the text is done.
    stream_mod.print_transition_usage(session, MessageType.Thinking)
    stream_mod.print_transition_usage(session, MessageType.Text)
    assert _plain(chunks) == ""
    assert getattr(session, stream_mod._PRINT_AGENT_JSON_MESSAGE_TYPE_ATTR) is MessageType.Text

    stream_mod.print_transition_usage(session, MessageType.ToolCalling)
    banners = _usage_banners(_plain(chunks))
    assert len(banners) == 1
    assert "Context usage: 42.0% (4200 tokens)" in banners[0]


def test_private_transition_usage_name_still_resolves() -> None:
    assert stream_mod._print_transition_usage is stream_mod.print_transition_usage


@pytest.mark.asyncio
async def test_thinking_only_turn_prints_no_banner(monkeypatch: Any) -> None:
    """A turn whose only block is reasoning never shows a banner.

    The banner closes the assistant's printed output, so a reasoning-only turn
    has nothing to close: printing one beforehand was the duplicate the CLI
    showed as ``banner → thinking... done`` on every reasoning-first step.
    """
    chunks = _capture_stream(monkeypatch)
    session = FakeSession(
        [ThinkPart(think="just reasoning")],
        show_thinking_stream=False,
    )
    session.status.context_usage = 0.6
    session.status.context_tokens = 6144
    # A previous turn's text block makes the switch into reasoning a real one.
    stream_mod.print_transition_usage(session, MessageType.Text)

    await _run_attempts(session)

    plain = _plain(chunks)
    assert "Context usage:" not in plain
    assert "thinking..." in plain
    assert "done," in plain


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
    # ...a display switch is still silent (it would open a text block)...
    stream_mod.print_transition_usage(session, MessageType.Text)
    assert chunks == []
    # ...so the *next* tool switch renders the banner, with the real numbers.
    stream_mod.print_transition_usage(session, MessageType.ToolCalling)

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
    stream_mod.print_transition_usage(session, MessageType.ToolCalling)

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
