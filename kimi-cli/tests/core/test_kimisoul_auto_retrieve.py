"""Tests for KimiSoul auto-retrieval with working & recency memory."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from kimi_cli.soul.kimisoul import KimiSoul


def _make_soul_for_auto_retrieve() -> KimiSoul:
    """Minimal KimiSoul bypassing __init__, just enough for _maybe_auto_retrieve_history."""
    soul = object.__new__(KimiSoul)

    loop_control = MagicMock()
    loop_control.auto_retrieve_history = True
    loop_control.auto_retrieve_history_threshold = 5.0
    loop_control.auto_retrieve_working_memory = True
    loop_control.auto_retrieve_working_memory_threshold = 5.0
    loop_control.auto_retrieve_recency_memory = True
    loop_control.auto_retrieve_recency_memory_threshold = 4.0
    loop_control.auto_retrieve_recency_weight = 1.0
    loop_control.auto_retrieve_max_injections_per_turn = 3
    loop_control.auto_retrieve_max_tokens_per_turn = 2_000
    soul._loop_control = loop_control

    runtime = MagicMock()
    runtime.llm = None
    soul._runtime = runtime

    soul._current_step_no = 1
    soul._current_turn_user_text = "a long enough query"
    soul._last_auto_retrieved_turn_id = None
    soul._recently_retrieved_turn_ids = set()

    return soul


def _make_history_index_turns(
    turns: list[dict[str, Any]],
) -> MagicMock:
    """Build a mock HistoryIndex that returns the given turns from search_with_recency."""
    index = MagicMock()
    index._turns = turns

    def _search_with_recency(query: str, **kwargs: Any) -> list[dict[str, Any]]:
        _ = query
        _ = kwargs
        return list(turns)

    index.search_with_recency = _search_with_recency
    return index


async def test_auto_retrieve_returns_empty_when_disabled() -> None:
    soul = _make_soul_for_auto_retrieve()
    soul._loop_control.auto_retrieve_history = False
    soul._loop_control.auto_retrieve_working_memory = False
    soul._loop_control.auto_retrieve_recency_memory = False
    soul._history_index = MagicMock()

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert result == []


async def test_auto_retrieve_returns_empty_on_non_first_step() -> None:
    soul = _make_soul_for_auto_retrieve()
    soul._current_step_no = 2
    soul._history_index = MagicMock()

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert result == []


async def test_auto_retrieve_returns_empty_for_short_query() -> None:
    soul = _make_soul_for_auto_retrieve()
    soul._current_turn_user_text = "short"
    soul._history_index = MagicMock()

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert result == []


async def test_auto_retrieve_long_term_memory_injects_compacted() -> None:
    soul = _make_soul_for_auto_retrieve()
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "old compacted question",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result) == 1
    assert result[0].type == "auto_retrieved_history"
    assert "old compacted question" in result[0].content


async def test_auto_retrieve_working_memory_injects_non_compacted() -> None:
    soul = _make_soul_for_auto_retrieve()
    soul._loop_control.auto_retrieve_history = False
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "older working memory question",
            "is_compacted": False,
            "score": 10.0,
            "boosted_score": 10.0,
        },
        {
            "turn_id": 1,
            "timestamp": 0,
            "role": "assistant",
            "text": "recent assistant answer one",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 0.0,
        },
        {
            "turn_id": 2,
            "timestamp": 0,
            "role": "user",
            "text": "recent user question two",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 0.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result) == 1
    assert result[0].type == "working_memory"
    assert "older working memory question" in result[0].content


async def test_auto_retrieve_recency_memory_injects_boosted_recent() -> None:
    soul = _make_soul_for_auto_retrieve()
    soul._loop_control.auto_retrieve_history = False
    soul._loop_control.auto_retrieve_working_memory = False
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "recently discussed topic",
            "is_compacted": False,
            "score": 2.0,
            "boosted_score": 8.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result) == 1
    assert result[0].type == "recency_memory"
    assert "recently discussed topic" in result[0].content


async def test_auto_retrieve_dedup_prevents_duplicate_injection() -> None:
    soul = _make_soul_for_auto_retrieve()
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "duplicate turn",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result1 = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result1) == 1

    result2 = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert result2 == []


async def test_auto_retrieve_respects_max_injections() -> None:
    soul = _make_soul_for_auto_retrieve()
    soul._loop_control.auto_retrieve_max_injections_per_turn = 1
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "compacted",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
        {
            "turn_id": 1,
            "timestamp": 0,
            "role": "user",
            "text": "working",
            "is_compacted": False,
            "score": 9.0,
            "boosted_score": 9.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result) == 1


async def test_auto_retrieve_skips_recent_context() -> None:
    soul = _make_soul_for_auto_retrieve()
    soul._loop_control.auto_retrieve_history = False
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "older working memory",
            "is_compacted": False,
            "score": 10.0,
            "boosted_score": 10.0,
        },
        {
            "turn_id": 1,
            "timestamp": 0,
            "role": "assistant",
            "text": "most recent assistant one",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 0.0,
        },
        {
            "turn_id": 2,
            "timestamp": 0,
            "role": "user",
            "text": "most recent user two",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 0.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    # The most recent 2 turns should be excluded from working memory
    assert len(result) == 1
    assert result[0].type == "working_memory"
    assert "older working memory" in result[0].content


async def test_auto_retrieve_updates_recently_retrieved_set() -> None:
    soul = _make_soul_for_auto_retrieve()
    turns = [
        {
            "turn_id": 42,
            "timestamp": 0,
            "role": "user",
            "text": "a turn",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    assert 42 not in soul._recently_retrieved_turn_ids  # pyright: ignore[reportPrivateUsage]
    await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert 42 in soul._recently_retrieved_turn_ids  # pyright: ignore[reportPrivateUsage]
    assert soul._last_auto_retrieved_turn_id == 42  # pyright: ignore[reportPrivateUsage]


async def test_auto_retrieve_token_budget_not_exceeded() -> None:
    """All 3 small injections fit within the default 2_000 token budget."""
    soul = _make_soul_for_auto_retrieve()
    turns = [
        # Long-term memory candidate
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "a",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
        # Working memory candidate (not among the last 2 non-compacted)
        {
            "turn_id": 1,
            "timestamp": 0,
            "role": "assistant",
            "text": "b",
            "is_compacted": False,
            "score": 9.0,
            "boosted_score": 0.0,
        },
        # Recent context (excluded from working memory)
        {
            "turn_id": 2,
            "timestamp": 0,
            "role": "user",
            "text": "c",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 0.0,
        },
        # Recency memory candidate
        {
            "turn_id": 3,
            "timestamp": 0,
            "role": "assistant",
            "text": "d",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 8.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result) == 3


async def test_auto_retrieve_token_budget_skips_excess_injections() -> None:
    """Token budget prevents the second injection when the first consumes most of the budget."""
    soul = _make_soul_for_auto_retrieve()
    # Budget of 50 allows the first injection (~33 tokens) but not a second (~32 more).
    soul._loop_control.auto_retrieve_max_tokens_per_turn = 50
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "a",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
        # Working memory candidate (not among the last 2 non-compacted)
        {
            "turn_id": 1,
            "timestamp": 0,
            "role": "assistant",
            "text": "b",
            "is_compacted": False,
            "score": 9.0,
            "boosted_score": 0.0,
        },
        # Recent context (excluded from working memory)
        {
            "turn_id": 2,
            "timestamp": 0,
            "role": "user",
            "text": "c",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 0.0,
        },
        # Recent context (excluded from working memory)
        {
            "turn_id": 3,
            "timestamp": 0,
            "role": "assistant",
            "text": "d",
            "is_compacted": False,
            "score": 0.0,
            "boosted_score": 0.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    # Only the first injection should fit within the 50-token budget
    assert len(result) == 1
    assert result[0].type == "auto_retrieved_history"


async def test_auto_retrieve_single_injection_exceeds_budget() -> None:
    """If a single injection exceeds the budget, it is skipped entirely."""
    soul = _make_soul_for_auto_retrieve()
    soul._loop_control.auto_retrieve_max_tokens_per_turn = 10
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "this text is definitely longer than ten tokens",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert result == []
    # Turn should NOT be tracked as recently retrieved since it was skipped
    assert 0 not in soul._recently_retrieved_turn_ids  # pyright: ignore[reportPrivateUsage]


async def test_auto_retrieve_count_cap_still_works() -> None:
    """Even with a high token budget, max_injections_per_turn is still enforced."""
    soul = _make_soul_for_auto_retrieve()
    soul._loop_control.auto_retrieve_max_tokens_per_turn = 10_000
    soul._loop_control.auto_retrieve_max_injections_per_turn = 2
    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "one",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
        {
            "turn_id": 1,
            "timestamp": 0,
            "role": "assistant",
            "text": "two",
            "is_compacted": False,
            "score": 9.0,
            "boosted_score": 9.0,
        },
        {
            "turn_id": 2,
            "timestamp": 0,
            "role": "user",
            "text": "three",
            "is_compacted": False,
            "score": 8.0,
            "boosted_score": 8.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result) == 2


async def test_auto_retrieve_passes_model_name_to_count_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """count_tokens should receive self.model_name as the model argument."""
    soul = _make_soul_for_auto_retrieve()
    soul._runtime.llm = MagicMock()
    soul._runtime.llm.chat_provider.model_name = "gpt-test-model"

    captured: dict[str, Any] = {}

    def _fake_count_tokens(text: str, model: str | None = None) -> int:
        captured["text"] = text
        captured["model"] = model
        return 1

    monkeypatch.setattr(
        "kimi_cli.soul.kimisoul.count_tokens",
        _fake_count_tokens,
    )

    turns = [
        {
            "turn_id": 0,
            "timestamp": 0,
            "role": "user",
            "text": "x",
            "is_compacted": True,
            "score": 10.0,
            "boosted_score": 10.0,
        },
    ]
    soul._history_index = _make_history_index_turns(turns)

    result = await soul._maybe_auto_retrieve_history()  # pyright: ignore[reportPrivateUsage]
    assert len(result) == 1
    assert captured.get("model") == "gpt-test-model"
