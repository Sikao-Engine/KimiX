"""Tests for ``job_output`` waiting on a background subagent session.

Passing a subagent session id as ``job_id`` makes ``job_output`` block
(with ``wait: true``) until the background run owning that session
settles.  These probes cover:

* a subagent session id that never existed -> the regular not-found error;
* an already-settled run -> returns immediately with the recorded state;
* a running store entry without a tracked task -> reports "still running"
  instead of hanging or pretending the run finished;
* a non-blocking read of a running run -> reports "still running";
* a running background run with ``wait: true`` -> the call blocks until the
  run finishes, then reports ``completed`` and leaves the session resumable;
* a timed-out wait -> returns ``[status: running]`` and leaves the run alive;
* an interrupt arriving *during* the wait -> the wait ends with ``closed``;
* the session-id -> task registry is cleaned up when the run settles.

Style mirrors ``test_subagent_batch.py``: patch ``kimix.utils`` session
primitives and drive ``Agent`` / ``TaskOutput`` directly.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import kimi_cli.tools.subagent as agent_mod
import pytest
from kimi_cli.tools.background import TaskOutput, TaskOutputParams
from kimi_cli.tools.subagent import (
    Agent,
    SubAgentParams,
    _background_run_tasks,
    _get_store,
)
from kimi_cli.tools.subagent.store import AgentSessionEntry

from kimix.base import MessageType


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
async def test_job_output_unknown_session_errors() -> None:
    parent = _parent()
    result = await TaskOutput(parent)(TaskOutputParams(job_id="never-seen"))
    assert result.is_error is True


def test_job_output_params_aliases() -> None:
    assert TaskOutputParams(**{"task_id": "t-1"}).job_id == "t-1"
    assert TaskOutputParams(job_id="t-2").job_id == "t-2"
    assert TaskOutputParams(**{"block": True}).wait is True


# ---------------------------------------------------------------------------
# Settled / edge states
# ---------------------------------------------------------------------------
async def test_job_output_settled_run_returns_immediately() -> None:
    parent = _parent()
    store = _get_store(parent)
    store.put(_entry(_sub_session(), "done-1", state="completed"))

    result = await TaskOutput(parent)(
        TaskOutputParams(job_id="done-1", wait=True)
    )

    assert result.is_error is False
    assert result.extras is not None
    assert result.extras["session_id"] == "done-1"
    assert result.extras["status"] == "completed"
    assert "done-1" in result.output


async def test_job_output_running_entry_without_task_reports_running() -> None:
    """A running entry with no tracked task must not hang or fake completion."""
    parent = _parent()
    _get_store(parent).put(_entry(_sub_session(), "orphan-1", state="running"))

    result = await TaskOutput(parent)(
        TaskOutputParams(job_id="orphan-1", wait=True)
    )

    assert result.is_error is False
    assert result.brief == "Subagent still running"
    assert "still running" in result.output


# ---------------------------------------------------------------------------
# Blocking behaviour on a live background run
# ---------------------------------------------------------------------------
async def test_job_output_nonblocking_read_of_running_subagent() -> None:
    """Without ``wait`` a running subagent read reports "still running"."""
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

        read_result = await TaskOutput(parent)(TaskOutputParams(job_id=session_id))

    assert read_result.is_error is False
    assert read_result.brief == "Subagent still running"
    assert "still running" in read_result.output
    release.set()


async def test_job_output_blocks_until_background_run_finishes() -> None:
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
            TaskOutput(parent)(TaskOutputParams(job_id=session_id, wait=True))
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


async def test_job_output_timed_out_wait_leaves_run_alive() -> None:
    """A wait shorter than the run returns [status: running] without killing it."""
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

        timed = await TaskOutput(parent)(
            TaskOutputParams(job_id=session_id, wait=True, timeout=1)
        )
        assert timed.is_error is False
        assert "[status: running]" in timed.output
        # The run is untouched and still tracked.
        assert session_id in _background_run_tasks

        release.set()
        final = await asyncio.wait_for(
            TaskOutput(parent)(TaskOutputParams(job_id=session_id, wait=True)),
            timeout=5,
        )

    assert final.is_error is False
    assert final.extras is not None
    assert final.extras["status"] == "completed"


async def test_job_output_interrupted_mid_wait_reports_closed() -> None:
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
            TaskOutput(parent)(TaskOutputParams(job_id=session_id, wait=True))
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
