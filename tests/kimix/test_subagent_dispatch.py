"""Single-dispatch and concurrent-call tests for the ``subagent`` tool.

Every tool call accepts exactly one string prompt and creates or resumes exactly
one sub-agent.  Multiple tasks require separate tool calls; independent calls
may still run concurrently.  These probes cover:

* scalar parameter validation and the single-run result shape;
* one session creation and one prompt per call;
* preparation failure before any session is created;
* **parallel dispatch through separate calls**, including the one-run-per-session
  guard and LRU eviction protection for running sessions.

Style mirrors ``test_agent_gate.py``: patch ``kimix.utils._create_session_async``
/ ``prompt_async`` / ``close_session_async`` and drive ``Agent`` directly.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import kimi_cli.tools.subagent as agent_mod
import pytest
from kaos.path import KaosPath
from kimi_cli.tools.subagent import (
    Agent,
    SubAgentParams,
    _active_runs,
    _get_store,
)
from kimi_cli.tools.subagent.store import AgentSessionEntry
from pydantic import ValidationError

from kimix.base import MessageType


def _parent(session_id: str = "parent-1", work_dir: Any = None) -> MagicMock:
    session = MagicMock()
    session.custom_data = {}
    session.custom_config = {"chat_provider": None}
    session.id = session_id
    session.work_dir = work_dir
    session._cli = None
    return session


def _sub_session(session_id: str = "sub-123") -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.get_custom_config.return_value = {}
    session.close = AsyncMock()
    session._closed = False
    return session


async def _pump(n: int = 60) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def _capturing_prompt(seen: list[tuple[str, Any]], text: str = "done"):
    """Record ``(prompt_str, session)`` for every run and emit one text part."""

    async def _mock(*, prompt_str: str, session: Any = None, output_function: Any = None, **kwargs: Any) -> None:
        seen.append((prompt_str, session))
        if output_function is not None:
            output_function(text, MessageType.Text)

    return _mock


def _capturing_prompt_texts(captured: list[str], text: str = "done"):
    seen: list[tuple[str, Any]] = []
    inner = _capturing_prompt(seen, text)

    async def _mock(*, prompt_str: str, **kwargs: Any) -> None:
        captured.append(prompt_str)
        await inner(prompt_str=prompt_str, **kwargs)

    return _mock


@pytest.fixture(autouse=True)
def _clean_agent_registries():
    def _reset() -> None:
        agent_mod._agent_entries.clear()
        agent_mod._agent_sessions.clear()
        agent_mod._children_by_parent.clear()
        agent_mod._child_parent.clear()
        agent_mod._active_runs.clear()

    _reset()
    yield
    _reset()


# ---------------------------------------------------------------------------
# 1. Param validation
# ---------------------------------------------------------------------------
def test_param_single_string_still_valid() -> None:
    p = SubAgentParams(prompt="x")
    assert p.prompt == "x"
    assert p.session_id is None
    # ``task`` alias still resolves to ``prompt``.
    assert SubAgentParams(**{"task": "y"}).prompt == "y"


@pytest.mark.parametrize("prompt", [[], ["one"], ["one", "two"]])
def test_param_list_prompt_is_rejected(prompt: list[str]) -> None:
    with pytest.raises(ValidationError, match="Input should be a valid string"):
        SubAgentParams(prompt=prompt)
    with pytest.raises(ValidationError, match="Input should be a valid string"):
        SubAgentParams(**{"task": prompt})


@pytest.mark.parametrize("session_id", [[], ["s1"], ["s1", None]])
def test_param_list_session_id_is_rejected(session_id: list[str | None]) -> None:
    with pytest.raises(ValidationError, match="Input should be a valid string"):
        SubAgentParams(prompt="one", session_id=session_id)


def test_param_str_stays_str() -> None:
    assert SubAgentParams.model_validate_json('{"prompt": "x"}').prompt == "x"


def test_param_dump_key_set_has_no_background_flag() -> None:
    keys = set(SubAgentParams(prompt="x").model_dump())
    assert keys == {
        "prompt",
        "session_id",
        "close_session",
        "return_history",
        "history_format",
        "inherit_context",
    }
    # The background mode is gone: neither the flag nor the old fields survive.
    assert "run_in_background" not in keys
    assert "description" not in keys
    assert "context_files" not in keys
    assert "context_data" not in keys


def test_param_schema_exposes_no_array_prompt_or_session() -> None:
    properties = SubAgentParams.model_json_schema()["properties"]
    assert properties["prompt"]["type"] == "string"
    assert properties["session"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_tool_description_requires_one_subagent_per_call() -> None:
    assert "exactly one subagent" in Agent.description
    assert "separate tool calls" in Agent.description


# ---------------------------------------------------------------------------
# 2. One call creates and prompts one sub-agent
# ---------------------------------------------------------------------------
async def test_single_string_extras_unchanged() -> None:
    parent = _parent("compat-parent")
    captured: list[str] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt_texts(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.return_value = _sub_session()
        result = await Agent(parent)(SubAgentParams(prompt="solo"))

    assert result.is_error is False
    assert captured == ["solo"]
    assert mc.await_count == 1
    assert "session_id" in result.extras
    assert "session_ids" not in result.extras
    assert result.extras["status"] == "closed"
    assert f"Session ID: {result.extras['session_id']}" in result.output
    assert _active_runs == {}


# ---------------------------------------------------------------------------
# 3. Prep-phase failure happens before session creation
# ---------------------------------------------------------------------------
async def test_missing_prompt_file_launches_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(tmp_path)
    parent = _parent("abort-parent", work_dir=KaosPath(str(work)))

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", new_callable=AsyncMock) as mp,
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as close,
    ):
        result = await Agent(parent)(SubAgentParams(prompt="@missing.md"))

    assert result.is_error is True
    assert result.brief == "Failed to create sub-agent session"
    assert "prompt file not found" in result.message
    mc.assert_not_awaited()
    mp.assert_not_awaited()
    close.assert_not_awaited()
    assert _active_runs == {}


# ---------------------------------------------------------------------------
# 4. Parallel dispatch: one session, one run
# ---------------------------------------------------------------------------
async def test_parallel_calls_with_same_session_id_reject_the_loser(
    tmp_path: Path,
) -> None:
    """Two concurrent calls on one session id: one runs, one fails clearly.

    This is the shape that used to break: both preparations succeed, then the
    second prompt raises ``"Session is already running"``, ``prompt_async``
    cancels the session (aborting the sibling) and the finishing run closes it,
    so the loser reported an opaque ``"Session is closed"``.  Now the loser is
    rejected before any prompt starts.
    """
    parent = _parent("shared-parent", work_dir=KaosPath(str(tmp_path)))
    existing = _sub_session("shared-id")
    _get_store(parent).put(
        AgentSessionEntry(
            session=existing,
            session_id="shared-id",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=0,
            is_active=True,
            state="completed",
        )
    )
    started = asyncio.Event()
    release = asyncio.Event()
    prompts: list[str] = []

    async def gated_prompt(*, prompt_str: str, output_function: Any = None, **kw: Any) -> None:
        prompts.append(prompt_str)
        started.set()
        await release.wait()
        if output_function is not None:
            output_function("done", MessageType.Text)

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock),
        patch("kimix.utils.prompt_async", side_effect=gated_prompt),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        agent = Agent(parent)
        first = asyncio.create_task(
            agent(SubAgentParams(prompt="first", session_id="shared-id", close_session=False))
        )
        await asyncio.wait_for(started.wait(), timeout=5)
        second = await agent(
            SubAgentParams(prompt="second", session_id="shared-id", close_session=False)
        )
        release.set()
        first_result = await asyncio.wait_for(first, timeout=5)

    # The second call never prompted: it named the conflict instead.
    assert prompts == ["first"]
    assert second.is_error is True
    assert "currently running" in second.message
    assert "Session is closed" not in second.message
    # The first run completed normally and its session is untouched.
    assert first_result.is_error is False
    assert existing._closed is False


async def test_parallel_calls_with_fresh_ids_all_succeed(tmp_path: Path) -> None:
    """N concurrent ``subagent`` calls with fresh ids never close each other."""
    parent = _parent("parallel-parent", work_dir=KaosPath(str(tmp_path)))
    created: list[MagicMock] = []

    def fake_create(**kw: Any) -> MagicMock:
        session = _sub_session(kw["session_id"])
        created.append(session)
        return session

    async def prompt(*, prompt_str: str, output_function: Any = None, **kw: Any) -> None:
        await asyncio.sleep(0)
        if output_function is not None:
            output_function(f"result:{prompt_str}", MessageType.Text)

    with (
        patch("kimix.utils._create_session_async", side_effect=fake_create),
        patch("kimix.utils.prompt_async", side_effect=prompt),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock) as close,
    ):
        agent = Agent(parent)
        results = await asyncio.gather(
            *(agent(SubAgentParams(prompt=f"task-{i}")) for i in range(4))
        )

    assert [r.is_error for r in results] == [False] * 4
    ids = [r.extras["session_id"] for r in results]
    assert len(set(ids)) == 4
    # Every freshly created session was closed exactly once, by its own run.
    assert len(created) == 4
    assert close.await_count == 4
    assert _active_runs == {}


async def test_parallel_dispatch_never_evicts_a_running_session(
    tmp_path: Path,
) -> None:
    """LRU eviction skips sessions a run is driving.

    A resumed session stays listed while its next run is in progress, so a wide
    dispatch filling the store used to be able to evict — and close — the very
    session another run was prompting.
    """
    parent = _parent("evict-parent", work_dir=KaosPath(str(tmp_path)))
    store = _get_store(parent)
    store.MAX_SESSIONS = 2

    running = _sub_session("running-id")
    store.put(
        AgentSessionEntry(
            session=running,
            session_id="running-id",
            created_at=time.time(),
            last_accessed=time.time(),  # least recently used
            conversation_history=[],
            total_turns=0,
            is_active=True,
            state="completed",
        )
    )
    idle = _sub_session("idle-id")
    store.put(
        AgentSessionEntry(
            session=idle,
            session_id="idle-id",
            created_at=time.time(),
            last_accessed=time.time() + 10,
            conversation_history=[],
            total_turns=0,
            is_active=True,
            state="completed",
        )
    )

    # ``running-id`` is claimed by an in-flight run.
    claim = agent_mod._claim_session_run("running-id")
    with patch("kimix.utils.close_session_async", new_callable=AsyncMock) as close:
        await store.evict_lru_if_needed()

    # The busy (!) session survives; the idle LRU one is evicted instead.
    assert store.get("running-id") is not None
    assert store.get("idle-id") is None
    close.assert_awaited_once_with(idle)

    agent_mod._release_session_run("running-id", claim)
    with patch("kimix.utils.close_session_async", new_callable=AsyncMock) as close:
        store.MAX_SESSIONS = 1
        await store.evict_lru_if_needed()
    # Nothing else was there, and the released id is no longer protected.
    assert close.await_count == 1
    assert store.get("running-id") is None


async def test_eviction_stops_when_every_session_is_busy(
    tmp_path: Path,
) -> None:
    """No idle victim -> the store grows instead of closing a live session."""
    parent = _parent("allbusy-parent", work_dir=KaosPath(str(tmp_path)))
    store = _get_store(parent)
    store.MAX_SESSIONS = 1
    for sid in ("busy-a", "busy-b"):
        store.put(
            AgentSessionEntry(
                session=_sub_session(sid),
                session_id=sid,
                created_at=time.time(),
                last_accessed=time.time(),
                conversation_history=[],
                total_turns=0,
                is_active=True,
                state="running",
            )
        )
        agent_mod._claim_session_run(sid)

    with patch("kimix.utils.close_session_async", new_callable=AsyncMock) as close:
        await store.evict_lru_if_needed()

    close.assert_not_awaited()
    assert set(store.entries) == {"busy-a", "busy-b"}


async def test_store_without_a_busy_hook_still_evicts_the_lru() -> None:
    """Control probe: without the claim the LRU session *is* evicted.

    The tool's store asks the module whether a run drives an id; a bare store
    (no hook) has no such knowledge, which is exactly the pre-fix behaviour.
    """
    store = agent_mod.AgentSessionStore()  # no is_busy hook
    store.MAX_SESSIONS = 2
    for offset, sid in enumerate(("lru-id", "other-id")):
        store.put(
            AgentSessionEntry(
                session=_sub_session(sid),
                session_id=sid,
                created_at=time.time(),
                last_accessed=time.time() + offset,
                conversation_history=[],
                total_turns=0,
                is_active=True,
                state="completed",
            )
        )
    claim = agent_mod._claim_session_run("lru-id")  # a run drives the LRU id

    with patch("kimix.utils.close_session_async", new_callable=AsyncMock):
        await store.evict_lru_if_needed()

    # Blind to the claim, it closes the session the run is prompting.
    assert store.get("lru-id") is None
    assert store.get("other-id") is not None
    agent_mod._release_session_run("lru-id", claim)
