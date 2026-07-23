"""Tests for Defects 3.1-3.4: TaskOutput improvements."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from kimix.tools.background import TaskOutput, TaskOutputParams


# ── Defect 3.1: block → wait rename ─────────────────────────────────────


class TestTaskOutputWaitRename:
    def test_new_name_wait_works(self) -> None:
        params = TaskOutputParams(task_id="task_1", wait=True)
        assert params.wait is True

    def test_old_name_block_still_works(self) -> None:
        params = TaskOutputParams(task_id="task_1", block=False)
        assert params.wait is False

    def test_default_is_true(self) -> None:
        params = TaskOutputParams(task_id="task_1")
        assert params.wait is True


# ── Defect 3.3: Structured task list ────────────────────────────────────


class TestTaskOutputListFormat:
    async def test_list_returns_markdown_or_empty(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        result = await to(TaskOutputParams(task_id=None))
        assert "|" in result.output or "No running" in result.output

    async def test_list_includes_extras(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        result = await to(TaskOutputParams(task_id=None))
        assert hasattr(result, 'extras')
        if result.extras and "tasks" in result.extras:
            for task in result.extras["tasks"]:
                assert "task_id" in task
                assert "kind" in task
                assert "status" in task


# ── Defect 3.4: Action parameter / kill ─────────────────────────────────


class TestTaskOutputActionKill:
    async def test_action_kill_requires_task_id(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        result = await to(TaskOutputParams(action="kill"))
        assert result.is_error
        assert "task_id" in result.message.lower()

    async def test_action_kill_missing_task_not_found(self, mock_session: MagicMock) -> None:
        to = TaskOutput(session=mock_session)
        result = await to(TaskOutputParams(action="kill", task_id="nonexistent"))
        assert result.is_error

    def test_legacy_kill_bool_maps_to_action(self) -> None:
        params = TaskOutputParams(task_id="t1", kill=True)
        assert params.action == "kill"
