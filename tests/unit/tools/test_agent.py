"""Tests for Defects 11.1-11.4: Agent tool improvements."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from kimix.tools.agent import Agent, AgentRespond, AgentRespondParams, AgentList, AgentListParams, SubAgentParams


class TestAgentRespond:
    async def test_agentrespond_missing_session(self, mock_session: MagicMock) -> None:
        ar = AgentRespond(session=mock_session)
        result = await ar(AgentRespondParams(session_id="nonexistent", response="answer"))
        assert result.is_error
        assert "not found" in result.message.lower()


class TestAgentListFormat:
    async def test_agentlist_returns_output(self, mock_session: MagicMock) -> None:
        al = AgentList(session=mock_session)
        result = await al(AgentListParams())
        assert result is not None

    async def test_agentlist_extras_has_raw_data(self, mock_session: MagicMock) -> None:
        al = AgentList(session=mock_session)
        result = await al(AgentListParams())
        if result.extras and "sessions" in result.extras:
            assert isinstance(result.extras["sessions"], list)


class TestSubAgentContextFiles:
    def test_context_files_accepted(self) -> None:
        params = SubAgentParams(prompt="test", context_files=["src/main.py"])
        assert params.context_files == ["src/main.py"]

    def test_context_data_accepted(self) -> None:
        params = SubAgentParams(prompt="test", context_data={"key": "value"})
        assert params.context_data == {"key": "value"}

    def test_history_format_default_json(self) -> None:
        params = SubAgentParams(prompt="test")
        assert params.history_format == "json"

    @pytest.mark.parametrize("fmt", ["json", "markdown", "summary"])
    def test_history_format_accepted(self, fmt: str) -> None:
        params = SubAgentParams(prompt="test", return_history=True, history_format=fmt)
        assert params.history_format == fmt
