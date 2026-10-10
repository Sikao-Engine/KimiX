"""Regression tests for the interrupt_agent / background-run close race.

When ``interrupt_agent`` closes a background subagent whose ``_execute`` is
still finishing, the late ``_update_store`` must not resurrect the closed
session, and the parent notice must say the run was interrupted rather than
claiming a clean completion with a bogus "(no text output)" final message.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kimi_cli.tools.subagent as agent_mod
from kimix.base import MessageType
from kimi_cli.tools.subagent import (
    Agent,
    AgentClose,
    AgentCloseParams,
    SubAgentParams,
    _forget_child_session,
    _get_store,
    _register_agent_session,
    _unregister_agent_session,
)
from kimi_cli.tools.subagent.store import AgentSessionEntry, AgentSessionStore


@pytest.fixture
def mock_session() -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    session.custom_config = {}
    return session


@pytest.fixture
def mock_sub_session() -> MagicMock:
    session = MagicMock()
    session.id = "sub-123"
    session.get_custom_config.return_value = {}
    session.close = AsyncMock()
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


class _FakeSteer:
    """Stand-in for ``kimi_cli.soul.steer.Steer`` recording pushed content."""

    def __init__(self) -> None:
        self.pushed: list[str] = []

    async def push(self, content: str) -> bool:
        self.pushed.append(content)
        return True


def _fake_sdk_session(session_id: str) -> MagicMock:
    """A stand-in SDK session exposing ``_cli.soul`` + ``_cli.session.id``."""
    sdk = MagicMock()
    cli = MagicMock()
    cli_session = MagicMock()
    cli_session.id = session_id
    cli.session = cli_session
    cli.soul = MagicMock()
    sdk._cli = cli
    return sdk


async def _prepare(
    mock_session: MagicMock, mock_sub_session: MagicMock, session_id: str
) -> Any:
    """Build a ``_PreparedRun`` for a background run of *session_id*."""
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
    assert store.was_closed("t3")
    assert store.was_closed("t4")


# ---------------------------------------------------------------------------
# (a) Externally-closed background run does not resurrect the store entry
# ---------------------------------------------------------------------------
async def test_interrupted_background_run_does_not_resurrect(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}
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
        with patch(
            "kimix.utils.prompt_async", side_effect=gated_prompt
        ), patch(
            "kimix.utils.close_session_async", new_callable=AsyncMock
        ):
            agent = Agent(mock_session)
            # Mimic ``_launch_background``: register the running entry, then
            # run detached.
            store = _get_store(mock_session)
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
            task = asyncio.create_task(agent._run_background(prepared))
            await started.wait()
            assert store.get("race-id") is not None  # listed while running

            agent_close = AgentClose(mock_session)
            close_result = await agent_close(
                AgentCloseParams(session_id="race-id")
            )
            assert not close_result.is_error
            assert store.get("race-id") is None
            assert store.was_closed("race-id")

            # Let the interrupted run finish; its late _update_store must not
            # resurrect the entry.
            finish.set()
            await task
    finally:
        _unregister_agent_session("race-id")
        _forget_child_session("race-id")

    store = _get_store(mock_session)
    assert store.get("race-id") is None
    assert all(e["session_id"] != "race-id" for e in store.list_active())
    assert store.was_closed("race-id")


# ---------------------------------------------------------------------------
# (b) Fresh resume of the same session id after close registers normally
# ---------------------------------------------------------------------------
async def test_fresh_resume_after_close_registers_normally(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}
    store = _get_store(mock_session)
    store.put(_entry(mock_sub_session, "resume-id"))
    store.close("resume-id")
    assert store.was_closed("resume-id")

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ):
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ):
                agent = Agent(mock_session)
                result = await agent(
                    SubAgentParams(
                        prompt="again",
                        session_id="resume-id",
                        close_session=False,
                        run_in_background=False,
                    )
                )

    assert not result.is_error
    entry = store.get("resume-id")
    assert entry is not None  # registered again, not blocked by the tombstone
    assert entry.state == "completed"
    assert not store.was_closed("resume-id")


# ---------------------------------------------------------------------------
# (c) Notice wording: interrupted vs. normal completion
# ---------------------------------------------------------------------------
async def _run_to_notice(
    mock_session: MagicMock,
    mock_sub_session: MagicMock,
    session_id: str,
    prompt_impl: Callable[..., Awaitable[None]],
    interrupt: bool,
    gates: "tuple[asyncio.Event, asyncio.Event] | None" = None,
) -> _FakeSteer:
    """Run one background subagent to settlement; return the parent steer."""
    mock_session.custom_config = {"chat_provider": None}
    mock_session.id = "parent-1"
    parent_sdk = _fake_sdk_session("parent-1")
    _register_agent_session("parent-1", parent_sdk)
    fake = _FakeSteer()
    try:
        prepared = await _prepare(mock_session, mock_sub_session, session_id)
        with patch(
            "kimix.utils.prompt_async", side_effect=prompt_impl
        ), patch(
            "kimix.utils.close_session_async", new_callable=AsyncMock
        ), patch(
            "kimi_cli.soul.steer.Steer.from_session", return_value=fake
        ):
            agent = Agent(mock_session)
            store = _get_store(mock_session)
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
            asyncio.create_task(agent._run_background(prepared))
            if interrupt:
                # Wait until the run is inside prompt_async, then close it.
                assert gates is not None
                prompt_started, prompt_release = gates
                await _wait_for(lambda: prompt_started.is_set())
                agent_close = AgentClose(mock_session)
                close_result = await agent_close(
                    AgentCloseParams(session_id=session_id)
                )
                assert not close_result.is_error
                prompt_release.set()
            await _wait_for(lambda: len(fake.pushed) > 0)
    finally:
        _unregister_agent_session("parent-1")
        _unregister_agent_session(session_id)
        _forget_child_session(session_id)
    return fake


async def test_notice_interrupted_run_not_reported_as_completed(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    prompt_started = asyncio.Event()
    prompt_release = asyncio.Event()

    async def gated_prompt(**kwargs: Any) -> None:
        prompt_started.set()
        await prompt_release.wait()
        out_fn = kwargs.get("output_function")
        if callable(out_fn):
            out_fn("partial work", MessageType.Text)

    fake = await _run_to_notice(
        mock_session, mock_sub_session, "notice-race-id", gated_prompt,
        interrupt=True, gates=(prompt_started, prompt_release),
    )

    assert len(fake.pushed) == 1
    notice = fake.pushed[0]
    assert "was interrupted" in notice
    assert "Partial output:" in notice
    assert "partial work" in notice
    assert "completed" not in notice
    assert "(no text output)" not in notice
    assert len(notice) < 2000


async def test_notice_normal_completion_unchanged(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    async def clean_prompt(**kwargs: Any) -> None:
        out_fn = kwargs.get("output_function")
        if callable(out_fn):
            out_fn("all done", MessageType.Text)

    fake = await _run_to_notice(
        mock_session, mock_sub_session, "notice-ok-id", clean_prompt,
        interrupt=False,
    )

    assert len(fake.pushed) == 1
    notice = fake.pushed[0]
    assert "'notice-ok-id' completed. Final message:" in notice
    assert "all done" in notice
    assert "was interrupted" not in notice
