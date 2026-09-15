"""Tests for the ``compact`` tool (kimix.tools.context).

Regression target: compaction is now hard-gated on context usage. When usage
is below the 30% floor the tool must refuse (ToolError) and never touch the
context; at/above the floor it proceeds to compact normally.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from kimi_agent_sdk import ToolError, ToolOk
from kimi_cli.soul.kimisoul import KimiSoul

from kimix.tools.context import MIN_CONTEXT_USAGE, CompactParams, compact


def _mock_soul(context_usage: float) -> MagicMock:
    soul = MagicMock(spec=KimiSoul)
    status = MagicMock()
    status.context_usage = context_usage
    status.step_count = 100  # far past any cooldown
    soul.status = status
    # ``custom_data`` is not part of the KimiSoul spec; give a real dict so the
    # cooldown bookkeeping path is exercised deterministically.
    soul.custom_data = {}
    soul.compact_context = AsyncMock()
    return soul


@pytest.fixture()
def tool() -> compact:
    return compact()


async def test_reject_below_floor(
    tool: compact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Usage under 30% -> ToolError and no compaction performed."""
    soul = _mock_soul(context_usage=0.20)
    monkeypatch.setattr(
        "kimix.tools.context.get_current_soul_or_none", lambda: soul
    )

    result = await tool(CompactParams(mode="retentive"))

    assert isinstance(result, ToolError)
    assert "not high enough" in result.message
    assert "20%" in result.message
    soul.compact_context.assert_not_awaited()


async def test_boundary_at_floor_allows_compaction(
    tool: compact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Usage exactly at the floor (30%) is allowed and compacts."""
    soul = _mock_soul(context_usage=MIN_CONTEXT_USAGE)
    monkeypatch.setattr(
        "kimix.tools.context.get_current_soul_or_none", lambda: soul
    )

    result = await tool(CompactParams(mode="retentive"))

    assert isinstance(result, ToolOk)
    soul.compact_context.assert_awaited_once()


async def test_high_usage_compacts(
    tool: compact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Normal high-usage flow returns ToolOk and updates cooldown state."""
    soul = _mock_soul(context_usage=0.80)
    monkeypatch.setattr(
        "kimix.tools.context.get_current_soul_or_none", lambda: soul
    )

    result = await tool(CompactParams(mode="balanced"))

    assert isinstance(result, ToolOk)
    soul.compact_context.assert_awaited_once()
    assert soul.custom_data["last_compact_step"] == 100


async def test_zero_usage_rejected(
    tool: compact, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh/empty session (0% usage) is rejected by the floor guard."""
    soul = _mock_soul(context_usage=0.0)
    monkeypatch.setattr(
        "kimix.tools.context.get_current_soul_or_none", lambda: soul
    )

    result = await tool(CompactParams(mode="retentive"))

    assert isinstance(result, ToolError)
    soul.compact_context.assert_not_awaited()
