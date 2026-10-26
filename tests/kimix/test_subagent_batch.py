"""Multi-dispatch tests for the ``subagent`` tool.

The ``subagent`` tool accepts ``prompt`` as ``str | list[str]`` and
``session_id`` as ``str | list[str | None] | None``.  A list prompt dispatches
N subagents in one call, and every run blocks until its own result file is
written (there is no background mode).  These probes cover:

* param validation (count match, bare-string rejection, duplicate ids, schema
  key set after removing ``description``/``context_files``/``context_data``);
* batch dispatch (N concurrent prompts, N distinct output files, one result per
  run) and the single-string path's unchanged extras shape;
* partial / all-fail aggregation;
* mixed resume (existing id + fresh) in one batch;
* preparation-phase failure (``@missing.md``) aborting before any prompt;
* **parallel dispatch**, the case that used to fail: several concurrent
  ``subagent`` calls (or a batch) never drive one session twice, never close a
  session another run is using, and never leak a closed session's opaque
  ``SessionStateError`` into the parent's result.

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
async def test_param_single_string_still_valid() -> None:
    p = SubAgentParams(prompt="x")
    assert p.prompt == "x"
    assert p.session_id is None
    # ``task`` alias still resolves to ``prompt``.
    assert SubAgentParams(**{"task": "y"}).prompt == "y"


async def test_param_list_prompt_and_session_id_rules() -> None:
    # None session_id -> all fresh.
    assert SubAgentParams(prompt=["a", "b"]).session_id is None
    # Matching-length id list (mix of resume + None) is valid.
    p = SubAgentParams(prompt=["a", "b"], session_id=["s1", None])
    assert p.session_id == ["s1", None]
    # One-element list allowed; behaves like batch-of-1.
    assert SubAgentParams(prompt=["a"], session_id=[None]).prompt == ["a"]


async def test_param_empty_list_prompt_raises() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        SubAgentParams(prompt=[])


async def test_param_mismatched_count_raises() -> None:
    with pytest.raises(ValidationError, match="count"):
        SubAgentParams(prompt=["a", "b"], session_id=["only"])
    with pytest.raises(ValidationError):
        SubAgentParams(prompt=["a", "b"], session_id=["s1", "s2", "s3"])


async def test_param_bare_string_session_with_list_raises() -> None:
    with pytest.raises(ValidationError, match="ambiguous"):
        SubAgentParams(prompt=["a", "b"], session_id="s1")


async def test_param_duplicate_ids_raise() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        SubAgentParams(prompt=["a", "b"], session_id=["dup", "dup"])


async def test_param_list_session_with_string_prompt_raises() -> None:
    with pytest.raises(ValidationError, match="not a list"):
        SubAgentParams(prompt="single", session_id=["s1", None])


async def test_param_str_stays_str_in_union() -> None:
    # A JSON string that is just text must never coerce to a list.
    assert SubAgentParams.model_validate_json('{"prompt": "x"}').prompt == "x"


async def test_param_dump_key_set_has_no_background_flag() -> None:
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


async def test_split_params_shapes() -> None:
    # Single string -> one run, singular session_id preserved.
    runs = SubAgentParams(prompt="x", session_id="s1")._split_params()
    assert len(runs) == 1
    assert runs[0].prompt == "x"
    assert runs[0].session_id == "s1"
    # List -> N runs sharing the scalar options.
    runs = SubAgentParams(
        prompt=["a", "b", "c"],
        session_id=[None, "s2", None],
        close_session=False,
        return_history=True,
        history_format="markdown",
        inherit_context=True,
    )._split_params()
    assert [r.prompt for r in runs] == ["a", "b", "c"]
    assert [r.session_id for r in runs] == [None, "s2", None]
    assert all(r.close_session is False for r in runs)
    assert all(r.return_history is True for r in runs)
    assert all(r.history_format == "markdown" for r in runs)
    assert all(r.inherit_context is True for r in runs)


# ---------------------------------------------------------------------------
# 2. Batch dispatch
# ---------------------------------------------------------------------------
async def test_batch_dispatches_every_prompt(tmp_path: Path) -> None:
    parent = _parent("fg-parent", work_dir=KaosPath(str(tmp_path)))
    seen: list[tuple[str, Any]] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt(seen)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(
            SubAgentParams(prompt=["one", "two", "three"])
        )

    assert result.is_error is False
    assert result.extras["status"] == "completed"
    ids = result.extras["session_ids"]
    assert len(ids) == 3
    assert len(set(ids)) == 3  # distinct sessions
    # Every prompt reached its own session.
    assert {p for p, _ in seen} == {"one", "two", "three"}
    assert len({id(s) for _, s in seen}) == 3
    for sid in ids:
        assert f"Session ID: {sid}" in result.output
    # Per-run output files exist and are distinct.
    files = result.extras["output_files"]
    assert len(files) == 3
    assert len(set(files)) == 3
    for f in files:
        assert Path(f).exists() or (tmp_path / Path(f)).exists()
    # Every run released its claim, so the ids can be resumed later.
    assert _active_runs == {}


async def test_batch_runs_concurrently(tmp_path: Path) -> None:
    """All runs of a batch are in flight at the same time, not serialized."""
    parent = _parent("conc-parent", work_dir=KaosPath(str(tmp_path)))
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def gated_prompt(*, prompt_str: str, output_function: Any = None, **kw: Any) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            if in_flight >= 3:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=5)
        finally:
            in_flight -= 1
        if output_function is not None:
            output_function("ok", MessageType.Text)

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=gated_prompt),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(SubAgentParams(prompt=["a", "b", "c"]))

    assert result.is_error is False
    assert peak == 3  # all three prompts overlapped


# ---------------------------------------------------------------------------
# 3. Partial failure
# ---------------------------------------------------------------------------
async def test_batch_partial_failure(tmp_path: Path) -> None:
    parent = _parent("partial-parent", work_dir=KaosPath(str(tmp_path)))

    async def flaky(*, prompt_str: str, output_function: Any = None, **kwargs: Any) -> None:
        if prompt_str == "boom":
            raise RuntimeError("child exploded")
        if output_function is not None:
            output_function("ok", MessageType.Text)

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=flaky),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(SubAgentParams(prompt=["fine", "boom", "also"]))

    assert result.is_error is False  # partial success still a ToolOk
    assert result.extras["status"] == "partial"
    ids = result.extras["session_ids"]
    assert len(ids) == 3
    failures = result.extras["failures"]
    assert len(failures) == 1
    assert failures[0]["index"] == 1
    assert failures[0]["session_id"] == ids[1]
    assert "child exploded" in failures[0]["message"]
    # The successful ids are still returned (not lost).
    assert ids[0] in result.output and ids[2] in result.output
    # A failed run must not keep its session claimed.
    assert _active_runs == {}


# ---------------------------------------------------------------------------
# 4. All-fail -> ToolError keeps all ids
# ---------------------------------------------------------------------------
async def test_batch_all_fail_is_error(tmp_path: Path) -> None:
    parent = _parent("allfail-parent", work_dir=KaosPath(str(tmp_path)))

    async def always_raises(*, prompt_str: str, **kwargs: Any) -> None:
        raise RuntimeError(f"nope:{prompt_str}")

    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=always_raises),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(SubAgentParams(prompt=["a", "b"]))

    assert result.is_error is True
    assert len(result.extras["session_ids"]) == 2
    assert "nope:a" in result.message and "nope:b" in result.message


# ---------------------------------------------------------------------------
# 5. Mixed resume in one batch
# ---------------------------------------------------------------------------
async def test_batch_mixed_resume_reuses_existing_session() -> None:
    parent = _parent("mix-parent")
    existing = _sub_session("keep-me")
    _get_store(parent).put(
        AgentSessionEntry(
            session=existing,
            session_id="keep-me",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=1,
            is_active=True,
            state="completed",
        )
    )
    captured: list[str] = []
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt_texts(captured)),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.side_effect = lambda **kw: _sub_session(kw["session_id"])
        result = await Agent(parent)(
            SubAgentParams(
                prompt=["resume it", "fresh one"],
                session_id=["keep-me", None],
            )
        )

    assert result.is_error is False
    ids = result.extras["session_ids"]
    assert ids[0] == "keep-me"  # resumed
    assert ids[1] != "keep-me"  # fresh uuid
    # Only the fresh run created a session; the resumed id was reused.
    created_ids = [c.kwargs["session_id"] for c in mc.await_args_list]
    assert "keep-me" not in created_ids
    assert ids[1] in created_ids


# ---------------------------------------------------------------------------
# 6. Compat: single string unchanged
# ---------------------------------------------------------------------------
async def test_single_string_extras_unchanged() -> None:
    parent = _parent("compat-parent")
    with (
        patch("kimix.utils._create_session_async", new_callable=AsyncMock) as mc,
        patch("kimix.utils.prompt_async", side_effect=_capturing_prompt_texts([])),
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mc.return_value = _sub_session()
        result = await Agent(parent)(SubAgentParams(prompt="solo"))

    assert result.is_error is False
    # Singular ``session_id`` key retained; ``session_ids`` NOT present.
    assert "session_id" in result.extras
    assert "session_ids" not in result.extras
    assert result.extras["status"] == "closed"
    assert f"Session ID: {result.extras['session_id']}" in result.output
    assert _active_runs == {}


# ---------------------------------------------------------------------------
# 7. Prep-phase failure aborts the whole batch before any prompt
# ---------------------------------------------------------------------------
async def test_batch_prep_failure_launches_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "here.md").write_text("present", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    parent = _parent("abort-parent", work_dir=KaosPath(str(work)))

    created: list[str] = []
    closed: list[Any] = []

    async def fake_create(**kw: Any) -> MagicMock:
        created.append(kw["session_id"])
        return _sub_session(kw["session_id"])

    async def fake_close(session: Any) -> None:
        closed.append(session)

    with (
        patch("kimix.utils._create_session_async", side_effect=fake_create),
        patch("kimix.utils.prompt_async", new_callable=AsyncMock) as mp,
        patch("kimix.utils.close_session_async", side_effect=fake_close),
    ):
        result = await Agent(parent)(
            SubAgentParams(prompt=["@here.md", "@missing.md"])
        )

    assert result.is_error is True
    assert result.brief == "Failed to create sub-agent session"
    # The offending prompt index is named.
    assert "prompt 1" in result.message
    # Nothing ran.
    mp.assert_not_awaited()
    # The successful sibling's fresh session was rolled back, leaving no live
    # or resumable sub-agent behind.
    assert len(created) == 1
    assert len(closed) == 1
    assert _get_store(parent).get(created[0]) is None
    assert created[0] not in agent_mod._agent_sessions
    assert created[0] not in agent_mod._agent_entries
    # Nor is its run claim left behind.
    assert created[0] not in _active_runs


# ---------------------------------------------------------------------------
# 8. Parallel dispatch: one session, one run
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


async def test_store_without_a_busy_hook_still_evicts_the_lru(
    tmp_path: Path,
) -> None:
    """Control probe: without the claim the LRU session *is* evicted.

    The tool's store asks the module whether a run drives an id; a bare store
    (no hook) has no such knowledge, which is exactly the pre-fix behaviour.
    """
    parent = _parent("control-parent", work_dir=KaosPath(str(tmp_path)))
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


async def test_batch_duplicate_session_id_is_rejected_up_front() -> None:
    """A batch may not target one session twice — validation, not a hang."""
    with pytest.raises(ValidationError, match="duplicate"):
        SubAgentParams(prompt=["a", "b"], session_id=["same", "same"])
