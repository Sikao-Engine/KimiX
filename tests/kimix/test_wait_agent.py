"""Blocking-wait tests for the ``wait_agent`` tool (``AgentWait``).

``wait_agent(session_id=...)`` blocks the calling turn until the background
subagent run owning that session id settles.  These probes cover:

* unknown session id -> ``ToolError`` (mirrors ``interrupt_agent``);
* an already-settled run -> returns immediately with the recorded state;
* a running background run -> the call blocks until the run finishes, then
  reports ``completed`` and leaves the session resumable;
* a running store entry without a tracked task -> reports "still running"
  instead of hanging or pretending the run finished;
* an interrupt arriving *during* the wait -> the wait ends with ``closed``;
* the session-id -> task registry is cleaned up when the run settles;
* ``session`` / ``session_id`` aliases resolve to ``agent_id``.

Style mirrors ``test_subagent_batch.py``: patch ``kimix.utils`` session
primitives and drive ``Agent`` / ``AgentWait`` directly.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kimi_cli.tools.subagent as agent_mod
from kimix.base import MessageType
from kimi_cli.tools.subagent import (
    Agent,
    AgentWait,
    AgentWaitParams,
    SubAgentParams,
    _background_run_tasks,
    _get_store,
)
from kimi_cli.tools.subagent.store import AgentSessionEntry


def _parent(session_id: str = "parent-1") -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    session.custom_config = {"chat_provider": None}
    session.id = session_id
    session.work_dir = None
    session._cli = None
    return session


def _sub_session(session_id: str = "sub-123") -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.get_custom_config.return_value = {}
    session.close = AsyncMock()
    return session


def _entry(session: Any, session_id: str, state: str = "running") -> AgentSessionEntry:
    return AgentSessionEntry(
        session=session,
        session_id=session_id,
        created_at=time.time(),
        last_accessed=time.time(),
        conversation_history=[],
        total_turns=0,
        is_active=True,
        state=state,
    )


async def _pump(n: int = 60) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


@pytest.fixture(autouse=True)
def _clean_agent_registries():
    def _reset() -> None:
        agent_mod._agent_entries.clear()
        agent_mod._agent_sessions.clear()
        agent_mod._children_by_parent.clear()
        agent_mod._child_parent.clear()
        agent_mod._background_run_tasks.clear()

    _reset()
    yield
    _reset()


def _gated_prompt(started: asyncio.Event, release: asyncio.Event, text: str = "answer"):
    async def _mock(*, prompt_str: str, output_function: Any = None, **kwargs: Any) -> None:
        started.set()
        await release.wait()
        if output_function is not None:
            output_function(text, MessageType.Text)

    return _mock


# ---------------------------------------------------------------------------
# Errors and param shape
# ---------------------------------------------------------------------------
async def test_wait_agent_unknown_session_errors() -> None:
    parent = _parent()
    result = await AgentWait(parent)(AgentWaitParams(agent_id="never-seen"))
    assert result.is_error is True
    assert result.message == "Session not found"
    assert result.brief == "Session not found"


def test_wait_agent_params_aliases() -> None:
    assert AgentWaitParams(**{"session": "s-1"}).agent_id == "s-1"
    assert AgentWaitParams(**{"session_id": "s-2"}).agent_id == "s-2"
    assert AgentWaitParams(agent_id="s-3").agent_id == "s-3"


# ---------------------------------------------------------------------------
# Settled / edge states
# ---------------------------------------------------------------------------
async def test_wait_agent_settled_run_returns_immediately() -> None:
    parent = _parent()
    store = _get_store(parent)
    store.put(_entry(_sub_session(), "done-1", state="completed"))

    result = await AgentWait(parent)(AgentWaitParams(agent_id="done-1"))

    assert result.is_error is False
    assert result.extras is not None
    assert result.extras["session_id"] == "done-1"
    assert result.extras["status"] == "completed"
    assert "done-1" in result.output


async def test_wait_agent_running_entry_without_task_reports_running() -> None:
    """A running entry with no tracked task must not hang or fake completion."""
    parent = _parent()
    _get_store(parent).put(_entry(_sub_session(), "orphan-1", state="running"))

    result = await AgentWait(parent)(AgentWaitParams(agent_id="orphan-1"))

    assert result.is_error is False
    assert result.brief == "Subagent still running"
    assert "still running" in result.output


# ---------------------------------------------------------------------------
# Blocking behaviour on a live background run
# ---------------------------------------------------------------------------
async def test_wait_agent_blocks_until_background_run_finishes() -> None:
    parent = _parent()
    started = asyncio.Event()
    release = asyncio.Event()
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_gated_prompt(started, release)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.return_value = _sub_session()
        result = await Agent(parent)(
            SubAgentParams(prompt="do X", run_in_background=True)
        )
        session_id = result.extras["session_id"]
        # The run is tracked by session id while it executes.
        assert session_id in _background_run_tasks
        await asyncio.wait_for(started.wait(), timeout=5)

        wait_task = asyncio.create_task(
            AgentWait(parent)(AgentWaitParams(agent_id=session_id))
        )
        await _pump()
        # The wait call blocks: the run has not been released yet.
        assert not wait_task.done()

        release.set()
        wait_result = await asyncio.wait_for(wait_task, timeout=5)

    assert wait_result.is_error is False
    assert wait_result.extras is not None
    assert wait_result.extras["status"] == "completed"
    assert session_id in wait_result.output
    # The session stays resumable after the wait (background contract).
    entry = _get_store(parent).get(session_id)
    assert entry is not None and entry.state == "completed" and entry.is_active
    # The session-id -> task registry is cleaned up once the run settles.
    await _pump()
    assert session_id not in _background_run_tasks


async def test_wait_agent_interrupted_mid_wait_reports_closed() -> None:
    """An interrupt arriving during the wait ends it with status ``closed``."""
    from kimi_cli.tools.subagent import AgentClose, AgentCloseParams

    parent = _parent()
    started = asyncio.Event()
    release = asyncio.Event()
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_gated_prompt(started, release)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.return_value = _sub_session()
        result = await Agent(parent)(
            SubAgentParams(prompt="do X", run_in_background=True)
        )
        session_id = result.extras["session_id"]
        await asyncio.wait_for(started.wait(), timeout=5)

        wait_task = asyncio.create_task(
            AgentWait(parent)(AgentWaitParams(agent_id=session_id))
        )
        await _pump()
        assert not wait_task.done()

        # Interrupt the run while the wait is blocked on it.
        close_result = await AgentClose(parent)(AgentCloseParams(agent_id=session_id))
        assert close_result.is_error is False
        release.set()
        wait_result = await asyncio.wait_for(wait_task, timeout=5)

    assert wait_result.is_error is False
    assert wait_result.extras is not None
    assert wait_result.extras["status"] == "closed"
