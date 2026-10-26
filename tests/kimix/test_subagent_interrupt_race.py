"""Regression tests for the ``interrupt_agent`` / run-close race.

When ``interrupt_agent`` closes a sub-agent session whose ``_execute`` is still
finishing, the late ``_update_store`` must not resurrect the closed session.
These probes drive the run directly (``_execute``) and cover:

* the ``AgentSessionStore`` tombstones that make the race detectable;
* an interrupted run leaving its session closed and unlisted;
* a fresh resume of the same id registering normally afterwards;
* a session closed *before* the run starts reporting a clear interruption
  instead of the SDK's opaque ``SessionStateError("Session is closed")``;
* the per-session run claim being released, so the id can be reused.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kaos.path import KaosPath
from kimi_cli.tools.subagent import (
    Agent,
    AgentClose,
    AgentCloseParams,
    SubAgentParams,
    _active_runs,
    _forget_child_session,
    _get_store,
    _unregister_agent_session,
)
from kimi_cli.tools.subagent.store import AgentSessionEntry, AgentSessionStore

from kimix.base import MessageType


@pytest.fixture(autouse=True)
def _clean_active_runs():
    _active_runs.clear()
    yield
    _active_runs.clear()


@pytest.fixture
def mock_session(tmp_path: Path) -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    session.custom_config = {"chat_provider": None}
    session.id = "parent-1"
    session.work_dir = KaosPath(str(tmp_path))
    return session


@pytest.fixture
def mock_sub_session() -> MagicMock:
    session = MagicMock()
    session.id = "sub-123"
    session.get_custom_config.return_value = {}
    session.close = AsyncMock()
    # ``_closed`` is compared with ``is True``: a bare MagicMock would read as
    # closed.
    session._closed = False
    return session


def _entry(session: MagicMock, session_id: str) -> AgentSessionEntry:
    return AgentSessionEntry(
        session=session,
        session_id=session_id,
        created_at=time.time(),
        last_accessed=time.time(),
        conversation_history=[],
        total_turns=0,
    )


async def _prepare(
    mock_session: MagicMock, mock_sub_session: MagicMock, session_id: str
) -> Any:
    """Build a ``_PreparedRun`` for a run of *session_id*."""
    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        agent = Agent(mock_session)
        return await agent._prepare_run(
            SubAgentParams(prompt="do X", session_id=session_id, close_session=False)
        )


async def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met within timeout")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# AgentSessionStore tombstones
# ---------------------------------------------------------------------------
async def test_store_tombstone_recorded_and_discarded(
    mock_sub_session: MagicMock,
) -> None:
    store = AgentSessionStore()
    store.put(_entry(mock_sub_session, "s1"))
    assert not store.was_closed("s1")
    assert store.close("s1") is True
    assert store.get("s1") is None
    assert store.was_closed("s1")
    # A fresh put revives the id: the stale tombstone must not block it.
    store.put(_entry(mock_sub_session, "s1"))
    assert not store.was_closed("s1")


async def test_store_tombstone_capped_fifo(mock_sub_session: MagicMock) -> None:
    store = AgentSessionStore()
    store.MAX_TOMBSTONES = 3
    for i in range(5):
        store.put(_entry(mock_sub_session, f"t{i}"))
        store.close(f"t{i}")
    # Only the 3 most recent tombstones survive (FIFO eviction).
    assert not store.was_closed("t0")
    assert not store.was_closed("t1")
    assert store.was_closed("t2")
    assert store.was_closed("t4")


# ---------------------------------------------------------------------------
# (a) An externally-closed run does not resurrect the store entry
# ---------------------------------------------------------------------------
async def test_interrupted_run_does_not_resurrect(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def gated_prompt(**kwargs: Any) -> None:
        started.set()
        await finish.wait()
        out_fn = kwargs.get("output_function")
        if callable(out_fn):
            out_fn("partial work", MessageType.Text)

    prepared = await _prepare(mock_session, mock_sub_session, "race-id")
    try:
        with patch("kimix.utils.prompt_async", side_effect=gated_prompt), patch(
            "kimix.utils.close_session_async", new_callable=AsyncMock
        ):
            agent = Agent(mock_session)
            store = _get_store(mock_session)
            # ``interrupt_agent`` can only close a session it knows about.
            store.put(
                AgentSessionEntry(
                    session=prepared.session,
                    session_id=prepared.session_id,
                    created_at=time.time(),
                    last_accessed=time.time(),
                    conversation_history=[],
                    total_turns=0,
                    is_active=True,
                    state="running",
                )
            )
            run = asyncio.create_task(agent._execute(prepared))
            await started.wait()
            assert store.get("race-id") is not None  # listed while running

            close_result = await AgentClose(mock_session)(
                AgentCloseParams(session_id="race-id")
            )
            assert not close_result.is_error
            assert store.get("race-id") is None
            assert store.was_closed("race-id")

            # Let the interrupted run finish; its late _update_store must not
            # resurrect the entry.
            finish.set()
            await run
    finally:
        _unregister_agent_session("race-id")
        _forget_child_session("race-id")

    store = _get_store(mock_session)
    assert store.get("race-id") is None
    assert all(e["session_id"] != "race-id" for e in store.list_active())
    assert store.was_closed("race-id")
    # The claim is released even on the interrupted path.
    assert "race-id" not in _active_runs


# ---------------------------------------------------------------------------
# (b) A fresh resume of the same session id after close registers normally
# ---------------------------------------------------------------------------
async def test_fresh_resume_after_close_registers_normally(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    store = _get_store(mock_session)
    store.put(_entry(mock_sub_session, "resume-id"))
    store.close("resume-id")
    assert store.was_closed("resume-id")

    with (
        patch(
            "kimix.utils._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch("kimix.utils.prompt_async", new_callable=AsyncMock),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = mock_sub_session
        result = await Agent(mock_session)(
            SubAgentParams(prompt="again", session_id="resume-id", close_session=False)
        )

    assert not result.is_error
    entry = store.get("resume-id")
    assert entry is not None  # registered again, not blocked by the tombstone
    assert entry.state == "completed"
    assert not store.was_closed("resume-id")


# ---------------------------------------------------------------------------
# (c) A session closed before the run starts reports the interruption
# ---------------------------------------------------------------------------
async def test_run_on_already_closed_session_reports_interruption(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """No prompt is attempted, and the message is not the SDK's opaque text."""
    prepared = await _prepare(mock_session, mock_sub_session, "closed-id")
    # Simulate an ``interrupt_agent`` landing between preparation and the run.
    mock_sub_session._closed = True
    try:
        with (
            patch("kimix.utils.prompt_async", new_callable=AsyncMock) as prompt,
            patch("kimix.utils.close_session_async", new_callable=AsyncMock),
        ):
            result = await Agent(mock_session)._execute(prepared)
    finally:
        _unregister_agent_session("closed-id")
        _forget_child_session("closed-id")

    prompt.assert_not_awaited()
    assert result.is_error is True
    assert result.brief == "sub-agent session closed"
    assert "closed before this run started" in result.message
    assert "Session is closed" not in result.message
    assert "closed-id" not in _active_runs


# ---------------------------------------------------------------------------
# (d) A run whose session is closed mid-prompt reports the interruption
# ---------------------------------------------------------------------------
class _SessionStateError(Exception):
    """Stand-in for ``kimi_agent_sdk.SessionStateError`` (same message)."""


async def test_run_closed_mid_prompt_is_reported_as_an_interruption(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """The child's ``Session is closed`` never reaches the parent verbatim.

    It used to be swallowed by ``prompt_async`` (``ensure_todo_finished=True``),
    which printed ``Prompt error (gracefully handled): Session is closed`` plus
    ``prompt failed.`` and handed the parent a bogus ``(no text output)``
    success.  Now the failure surfaces once, named for what it was.
    """

    async def closing_prompt(*, session: Any = None, **_kwargs: Any) -> None:
        # The session is torn down while the child prompt runs.
        session._closed = True
        raise _SessionStateError("Session is closed")

    prepared = await _prepare(mock_session, mock_sub_session, "closed-mid-id")
    try:
        with (
            patch("kimix.utils.prompt_async", side_effect=closing_prompt) as prompt,
            patch("kimix.utils.close_session_async", new_callable=AsyncMock),
        ):
            result = await Agent(mock_session)._execute(prepared)
    finally:
        _unregister_agent_session("closed-mid-id")
        _forget_child_session("closed-mid-id")

    # The child prompt is driven so its own failures propagate to this tool
    # instead of being turned into a silent success.
    assert prompt.await_args.kwargs["ensure_todo_finished"] is False
    assert result.is_error is True
    assert result.brief == "sub-agent task failed"
    assert "was closed while it was running" in result.message
    assert "Session is closed" not in result.message
    assert "(no text output)" not in result.output
    assert "closed-mid-id" not in _active_runs
