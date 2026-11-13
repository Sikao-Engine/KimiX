"""Rendering tests for the broadcast batch injected into a session's context.

The soul steers one text block per poll, so the renderer owns the "content must
not blow up the context" rule: whatever the other agents published, the block
stays inside its token budget (newest messages win, the rest is deferred) and a
single oversized message is cut with an explicit marker.
"""

from __future__ import annotations

import time

from kimi_cli.tools.broadcast.bus import BroadcastMessage
from kimi_cli.tools.broadcast.format import (
    ELLIPSIS,
    OMITTED_NOTE,
    TRUNCATION_RESERVE_TOKENS,
    format_broadcast_batch,
    format_timestamp,
    truncate_to_tokens,
)
from kimi_cli.utils.tokens import count_tokens


def _message(offset_ms: int, text: str) -> BroadcastMessage:
    return BroadcastMessage(time=1_700_000_000_000 + offset_ms, content=text)


def test_empty_batch_renders_nothing() -> None:
    assert format_broadcast_batch([], token_budget=1000, count_tokens=count_tokens) is None


def test_non_positive_budget_renders_nothing() -> None:
    rows = [_message(0, "hi")]
    assert format_broadcast_batch(rows, token_budget=0, count_tokens=count_tokens) is None
    assert format_broadcast_batch(rows, token_budget=-5, count_tokens=count_tokens) is None


def test_batch_is_rendered_in_chronological_order_with_a_header() -> None:
    rows = [_message(1_000, "first"), _message(2_000, "second")]
    text = format_broadcast_batch(rows, token_budget=1_000, count_tokens=count_tokens)
    assert text is not None
    lines = text.splitlines()
    assert "2 message(s)" in lines[0]
    assert lines[1].endswith("first")
    assert lines[2].endswith("second")
    assert lines[1].startswith(f"[{format_timestamp(rows[0].time)}] ")


def test_timestamp_formatting_is_second_resolution_local_time() -> None:
    stamp = format_timestamp(1_700_000_000_000)
    assert time.strptime(stamp, "%Y-%m-%d %H:%M:%S") is not None
    assert len(stamp) == 19


def test_rendered_block_stays_inside_the_token_budget() -> None:
    rows = [_message(index, f"message number {index} " + "x" * 200) for index in range(20)]
    budget = 300
    text = format_broadcast_batch(rows, token_budget=budget, count_tokens=count_tokens)
    assert text is not None
    assert count_tokens(text) <= budget


def test_newest_messages_survive_a_tight_budget() -> None:
    rows = [_message(index, f"payload-{index}") for index in range(50)]
    budget = 120
    text = format_broadcast_batch(rows, token_budget=budget, count_tokens=count_tokens)
    assert text is not None
    assert "payload-49" in text
    assert "payload-0" not in text
    omitted_lines = [line for line in text.splitlines() if line == OMITTED_NOTE.format(count=0)]
    assert omitted_lines or "... " in text
    # The note counts the dropped tail of the batch, never the kept messages.
    assert "earlier message(s) omitted" in text


def test_oversized_single_message_is_truncated_with_a_marker() -> None:
    rows = [_message(0, "A" * 20_000)]
    text = format_broadcast_batch(rows, token_budget=200, count_tokens=count_tokens)
    assert text is not None
    assert count_tokens(text) <= 200
    assert ELLIPSIS in text
    assert "A" * 20_000 not in text


def test_no_header_room_means_no_injection() -> None:
    rows = [_message(index, "x") for index in range(5)]
    text = format_broadcast_batch(rows, token_budget=8, count_tokens=count_tokens)
    assert text is None
    assert 8 <= TRUNCATION_RESERVE_TOKENS  # reserve is what makes tiny budgets useless


def test_truncate_to_tokens_keeps_short_text_intact() -> None:
    assert truncate_to_tokens("short", 100, count_tokens) == "short"


def test_truncate_to_tokens_respects_the_budget() -> None:
    text = "word " * 5_000
    for budget in (1, 5, 40, 400):
        cut = truncate_to_tokens(text, budget, count_tokens)
        assert count_tokens(cut) <= budget
        assert cut == "" or cut.endswith(ELLIPSIS)


def test_truncate_to_tokens_zero_budget_returns_empty() -> None:
    assert truncate_to_tokens("anything", 0, count_tokens) == ""


def test_newlines_in_content_do_not_break_the_layout() -> None:
    rows = [_message(0, "line one\nline two")]
    text = format_broadcast_batch(rows, token_budget=1_000, count_tokens=count_tokens)
    assert text is not None
    assert "line one\nline two" in text
