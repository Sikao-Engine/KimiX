"""Regression tests: sub-agent sessions must be anonymous (ephemeral).

A session created by the ``subagent`` tool (``src/kimix/tools/agent/__init__.py``)
is scratch space owned by the tool: the tool creates it, and closes it again on
``close_session`` (the default), on LRU eviction or on ``interrupt_agent``.  Its
directory must therefore not survive the run — the SDK only removes
``<work dir>/.kimix_cache/<session id>`` for *anonymous* sessions
(``kimi_agent_sdk._session.Session.close``), so the tool has to ask for an
anonymous session with ``anonymous=True``.

Without that flag every delegation leaves a ``.kimix_cache/<uuid>`` directory
behind forever, and ``kimix.server.session_manager`` indexes those directories
as if they were user sessions (``CliSession.list``).
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kaos.path import KaosPath

from kimix.tools.agent import (
    Agent,
    SubAgentParams,
    _children_by_parent,
    _get_agent_session,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def parent_session(tmp_path: Path) -> SimpleNamespace:
    """Minimal parent session: a work dir, custom config and custom data."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return SimpleNamespace(
        id="parent-1",
        work_dir=KaosPath(str(work_dir)),
        custom_config={"chat_provider": None},
        custom_data={},
    )


@pytest.fixture(autouse=True)
def _clean_agent_registries():
    """Keep the module-level agent registries from leaking between tests.

    ``kimix.tools.agent`` tracks live sub-agent sessions process-wide (that is
    how a parent addresses them by id), so a test that deliberately leaves a
    child open must not be visible to the next one.
    """
    import kimix.tools.agent as agent_module

    def _reset() -> None:
        agent_module._agent_entries.clear()
        agent_module._agent_sessions.clear()
        agent_module._children_by_parent.clear()
        agent_module._child_parent.clear()
        agent_module._pending_messages.clear()

    _reset()
    yield
    _reset()


@pytest.fixture
def isolated_share_dir(monkeypatch, tmp_path: Path) -> Path:
    """Isolate ``kimi_cli`` metadata/share dir so tests never touch the real one."""
    share_dir = tmp_path / "share"
    share_dir.mkdir()

    def _get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("kimi_cli.share.get_share_dir", _get_share_dir)
    monkeypatch.setattr("kimi_cli.metadata.get_share_dir", _get_share_dir)
    return share_dir


class _FakeSoul:
    """Stand-in for ``KimiSoul`` with just what ``Session.close`` touches."""

    def __init__(self) -> None:
        self.agent = SimpleNamespace(toolset=None)

    async def close(self) -> None:
        return None


class _FakeCLI:
    """Stand-in for ``KimiCLI`` that keeps the *real* ``CliSession``."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.soul = _FakeSoul()


def _patch_real_session_creation(monkeypatch, tmp_path: Path) -> None:
    """Run the real session machinery without an LLM, config or tool loading.

    ``kimi_agent_sdk.Session.create/resume`` still build a real ``CliSession``
    (and therefore a real ``.kimix_cache/<id>`` directory); only ``KimiCLI``
    (which would need a provider and load every tool) is stubbed, exactly like
    ``tests/test_sdk_sessions_dir.py`` does.
    """
    from kimi_cli.config import Config

    import kimi_agent_sdk._session as sdk_session

    async def fake_cli_create(cli_session: Any, **kwargs: Any) -> _FakeCLI:
        return _FakeCLI(cli_session)

    monkeypatch.setattr(sdk_session.KimiCLI, "create", fake_cli_create)

    # No provider/model needed: hand the session machinery an empty Config.
    monkeypatch.setattr(
        "kimix.utils.session._create_config",
        lambda provider_dict=None: (Config(), provider_dict),
    )


def _cache_dir(work_dir: KaosPath, session_id: str) -> Path:
    return Path(str(work_dir)) / ".kimix_cache" / session_id


# ---------------------------------------------------------------------------
# The tool must *ask* for an anonymous session
# ---------------------------------------------------------------------------


def test_session_dir_falls_back_to_process_cwd() -> None:
    """Without a work dir the SDK stores sessions under the process CWD."""
    from kimix.tools.agent import _session_dir

    session = SimpleNamespace(id="no-work-dir", custom_data={})
    assert _session_dir(session, "abc") == Path.cwd() / ".kimix_cache" / "abc"


async def test_fresh_subagent_session_is_anonymous(
    parent_session: SimpleNamespace, monkeypatch
) -> None:
    """A brand new sub-agent session must request ``anonymous=True``."""
    sub_session = MagicMock()
    sub_session.id = "sub-123"
    sub_session.get_custom_config.return_value = {}

    with (
        patch(
            "kimix.tools.agent._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock),
        patch("kimix.tools.agent.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = sub_session
        result = await Agent(parent_session)(SubAgentParams(prompt="do X"))

    assert not result.is_error
    assert mock_create.await_args.kwargs["session_id"]
    assert mock_create.await_args.kwargs["anonymous"] is True


async def test_resumed_existing_session_keeps_its_directory(
    parent_session: SimpleNamespace,
) -> None:
    """Resuming a session that already exists on disk must not delete it.

    Such a directory was not created by this call (a saved sub-agent
    conversation, or a session the caller owns), so the tool must leave it
    alone instead of opting it into anonymous deletion.
    """
    work_dir: KaosPath = parent_session.work_dir
    session_id = "saved-subagent"
    _cache_dir(work_dir, session_id).mkdir(parents=True)

    sub_session = MagicMock()
    sub_session.id = session_id
    sub_session.get_custom_config.return_value = {}

    with (
        patch(
            "kimix.tools.agent._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock),
        patch("kimix.tools.agent.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = sub_session
        result = await Agent(parent_session)(
            SubAgentParams(prompt="continue", session_id=session_id)
        )

    assert not result.is_error
    assert mock_create.await_args.kwargs["anonymous"] is False


# ---------------------------------------------------------------------------
# End-to-end: the session directory is really gone after the run
# ---------------------------------------------------------------------------


async def test_subagent_session_dir_deleted_after_finish(
    parent_session: SimpleNamespace,
    isolated_share_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Integration: finishing a sub-agent removes its ``.kimix_cache`` dir.

    Uses the real ``_create_session_async`` / ``close_session_async`` pair, so
    this exercises the actual SDK deletion path that a live run goes through.
    """
    _patch_real_session_creation(monkeypatch, tmp_path)

    # Keep the tool instance alive: ``Agent.__del__`` closes whatever is still
    # in the store, and the deletion must come from the tool's own close.
    agent = Agent(parent_session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        result = await agent(SubAgentParams(prompt="do X"))

    assert not result.is_error
    session_id = result.extras["session_id"]
    session_dir = _cache_dir(parent_session.work_dir, session_id)
    assert not session_dir.exists(), (
        f"sub-agent session dir {session_dir} survived the run"
    )


async def test_subagent_session_dir_removed_while_a_file_is_locked(
    parent_session: SimpleNamespace,
    isolated_share_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Integration: a file still held at close time does not leak the dir.

    Windows scenario the SDK has to absorb: the session's ``context.db`` is
    still locked (aiosqlite worker thread winding down, anti-virus scan, a tool
    that has not released the file). ``shutil.rmtree(..., ignore_errors=True)``
    then fails silently and, before the SDK retried/escalated, the whole
    sub-agent directory — plus its half-deleted contents — stayed behind.
    """
    _patch_real_session_creation(monkeypatch, tmp_path)

    from kimix.utils.session import _create_session_async as real_create_session

    handles: list[Any] = []

    async def create_and_lock(**kwargs: Any) -> Any:
        session = await real_create_session(**kwargs)
        locked = _cache_dir(parent_session.work_dir, kwargs["session_id"]) / "context.db"
        handle = open(locked, "rb")  # no FILE_SHARE_DELETE on Windows
        handles.append(handle)
        threading.Timer(0.3, handle.close).start()
        return session

    monkeypatch.setattr("kimix.tools.agent._create_session_async", create_and_lock)

    agent = Agent(parent_session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        result = await agent(SubAgentParams(prompt="do X"))

    assert not result.is_error
    session_dir = _cache_dir(parent_session.work_dir, result.extras["session_id"])
    assert not session_dir.exists(), (
        f"sub-agent session dir {session_dir} survived a transiently locked file"
    )
    time.sleep(0.4)  # let the releaser finish before the test tears down
    assert all(handle.closed for handle in handles)


async def test_subagent_session_dir_kept_while_session_is_alive(
    parent_session: SimpleNamespace,
    isolated_share_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """``close_session=False`` keeps the session (and its dir) usable."""
    from kimix.tools.agent import _get_store

    _patch_real_session_creation(monkeypatch, tmp_path)

    # Hold the tool instance: ``Agent.__del__`` would close the kept session.
    agent = Agent(parent_session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        result = await agent(
            SubAgentParams(prompt="do X", close_session=False)
        )

    assert not result.is_error
    session_id = result.extras["session_id"]
    session_dir = _cache_dir(parent_session.work_dir, session_id)
    assert session_dir.exists()
    entry = _get_store(parent_session).get(session_id)
    assert entry is not None
    assert entry.session._anonymous is True

    # Once closed, the scratch directory goes away.
    from kimix.utils import close_session_async

    await close_session_async(entry.session)
    assert not session_dir.exists()


async def test_resuming_a_closed_subagent_leaves_no_directory_behind(
    parent_session: SimpleNamespace,
    isolated_share_dir: Path,
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Resuming a closed sub-agent id must not resurrect a permanent dir.

    The id stays a valid handle (a fresh conversation is started when the
    scratch directory is gone, and queued messages are still listed), but the
    recreated session is anonymous as well — otherwise the first
    ``subagent(session_id=...)`` after a close would leak a directory forever.
    """
    _patch_real_session_creation(monkeypatch, tmp_path)

    agent = Agent(parent_session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        first = await agent(SubAgentParams(prompt="do X"))
        session_id = first.extras["session_id"]
        assert not _cache_dir(parent_session.work_dir, session_id).exists()

        second = await agent(
            SubAgentParams(prompt="follow up", session_id=session_id)
        )

    assert not second.is_error
    assert second.extras["session_id"] == session_id
    assert not _cache_dir(parent_session.work_dir, session_id).exists()


async def test_inherit_context_copy_is_anonymous(
    parent_session: SimpleNamespace, monkeypatch
) -> None:
    """The inherited (copied) session is scratch too, so it must be anonymous.

    The anonymity decision is taken *before* the parent directory is copied to
    the new session id, so the copied directory is deleted on close.
    """
    sub_session = MagicMock()
    sub_session.id = "inherited-sub"
    sub_session.get_custom_config.return_value = {}

    async def fake_inherit(_self: Any, target_session_id: str) -> None:
        # The real helper creates the target directory via Session.copy.
        _cache_dir(parent_session.work_dir, target_session_id).mkdir(parents=True)

    with (
        patch(
            "kimix.tools.agent._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch.object(Agent, "_inherit_parent_context", fake_inherit),
        patch.object(Agent, "_reset_inherited_system_prompt", new_callable=AsyncMock),
        patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock),
        patch("kimix.tools.agent.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = sub_session
        result = await Agent(parent_session)(
            SubAgentParams(prompt="do X", inherit_context=True)
        )

    assert not result.is_error
    session_id = mock_create.await_args.kwargs["session_id"]
    assert session_id == result.extras["session_id"]
    # The copy really landed on disk for this session id ...
    assert _cache_dir(parent_session.work_dir, session_id).exists()
    # ... yet the session is anonymous, so it is removed when it closes.
    assert mock_create.await_args.kwargs["anonymous"] is True

# ---------------------------------------------------------------------------
# Cascade: closing/destroying the parent tears down its sub-agent sessions
# ---------------------------------------------------------------------------


async def _real_parent_session(tmp_path: Path, monkeypatch, session_id: str = "parent-1"):
    """A real SDK session; tools receive its CLI session, like production does."""
    from kimi_agent_sdk import Session as SdkSession

    _patch_real_session_creation(monkeypatch, tmp_path)
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    parent = await SdkSession.create(
        work_dir=KaosPath(str(work_dir)), session_id=session_id
    )
    # The Agent tool reads its provider overrides off the CLI session config.
    parent._cli.session.custom_config["chat_provider"] = None
    return parent


async def test_parent_close_deletes_all_subagent_sessions(
    isolated_share_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """Closing the parent closes (and deletes) every sub-agent it spawned.

    The sub-agent ids stay usable for agent-to-agent messaging for as long as
    the parent lives — but nothing of theirs may outlive the parent session.
    """
    from kimix.utils import close_session_async

    parent = await _real_parent_session(tmp_path, monkeypatch)
    work_dir = parent._cli.session.work_dir

    agent = Agent(parent._cli.session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        first = await agent(SubAgentParams(prompt="a", close_session=False))
        second = await agent(SubAgentParams(prompt="b", close_session=False))

    child_ids = [first.extras["session_id"], second.extras["session_id"]]
    for child_id in child_ids:
        assert _cache_dir(work_dir, child_id).exists()
        # A2A: the parent can still address each child by id.
        assert _get_agent_session(child_id) is not None

    await close_session_async(parent)

    for child_id in child_ids:
        assert not _cache_dir(work_dir, child_id).exists(), (
            f"sub-agent session dir of {child_id} outlived the parent session"
        )
        assert _get_agent_session(child_id) is None
    assert not _children_by_parent.get("parent-1")


async def test_parent_clear_deletes_all_subagent_sessions(
    isolated_share_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """Wiping the parent conversation also wipes its sub-agent sessions."""
    from kimix.utils import clear_session_async

    parent = await _real_parent_session(tmp_path, monkeypatch)
    work_dir = parent._cli.session.work_dir

    agent = Agent(parent._cli.session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        result = await agent(SubAgentParams(prompt="a", close_session=False))
    child_id = result.extras["session_id"]
    assert _cache_dir(work_dir, child_id).exists()

    await clear_session_async(parent)

    assert not _cache_dir(work_dir, child_id).exists()


async def test_parent_close_keeps_a_named_child_session_dir(
    isolated_share_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """A durable (explicitly resumed) sub-agent session is closed, not deleted."""
    from kimix.utils import close_session_async

    parent = await _real_parent_session(tmp_path, monkeypatch)
    work_dir = parent._cli.session.work_dir
    named_id = "saved-subagent"
    _cache_dir(work_dir, named_id).mkdir(parents=True)  # pre-existing durable dir

    agent = Agent(parent._cli.session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        result = await agent(
            SubAgentParams(prompt="continue", session_id=named_id, close_session=False)
        )
    assert result.extras["session_id"] == named_id
    child = _get_agent_session(named_id)
    assert child is not None

    await close_session_async(parent)

    assert child._closed is True
    assert _cache_dir(work_dir, named_id).exists(), (
        "a durable session the tool did not create must not be deleted"
    )


async def test_child_closed_normally_leaves_the_parent_registry(
    isolated_share_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """A finished child is forgotten, so the cascade does not touch it again."""
    from kimix.utils import close_session_async

    parent = await _real_parent_session(tmp_path, monkeypatch)

    agent = Agent(parent._cli.session)
    with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
        result = await agent(SubAgentParams(prompt="a"))  # close_session=True

    child_id = result.extras["session_id"]
    assert child_id not in _children_by_parent.get("parent-1", set())
    assert _get_agent_session(child_id) is None

    # No children left: closing the parent is a plain no-op cascade.
    await close_session_async(parent)
    assert not _children_by_parent.get("parent-1")


def test_throwaway_agent_instance_keeps_parent_children(
    isolated_share_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """A transient ``Agent`` instance must not reap the parent's sub-agents.

    ``AgentRespond`` builds a throwaway ``Agent`` per call (and the whole
    toolset is rebuilt on ``/clear``), so a destroyed tool object says nothing
    about the parent session's lifetime.  Its ``__del__`` used to pop the
    session's sub-agent store and close every session in it, which silently
    killed the parent's live sub-agents.
    """
    import gc

    from kimix.tools.agent import _get_store

    holder: dict[str, Any] = {}

    async def spawn() -> str:
        parent = await _real_parent_session(tmp_path, monkeypatch)
        agent = Agent(parent._cli.session)
        holder["agent"] = agent  # the principal toolset instance stays alive
        holder["parent"] = parent
        with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
            result = await agent(SubAgentParams(prompt="a", close_session=False))
        return result.extras["session_id"]

    child_id = asyncio.run(spawn())
    parent = holder["parent"]
    work_dir = parent._cli.session.work_dir
    child_dir = _cache_dir(work_dir, child_id)
    assert child_dir.exists()

    helper = Agent(parent._cli.session)  # what AgentRespond constructs per call
    del helper
    gc.collect()

    assert child_dir.exists(), "a throwaway tool instance deleted a live sub-agent"
    assert _get_agent_session(child_id) is not None
    assert _get_store(parent._cli.session).get(child_id) is not None


def test_shutdown_path_cascades_to_subagent_sessions(
    isolated_share_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """The *sync* ``close_session`` (CLI exit / interpreter shutdown) cascades too.

    ``kimix.utils.session.close_session`` is what the CLI and the
    ``threading._register_atexit`` shutdown hook call, so this is the path a
    normal ``/exit`` takes.
    """
    import asyncio

    from kimix.utils import close_session

    async def spawn() -> tuple[Any, str]:
        parent = await _real_parent_session(tmp_path, monkeypatch)
        agent = Agent(parent._cli.session)
        with patch("kimix.tools.agent.utils.prompt_async", new_callable=AsyncMock):
            result = await agent(SubAgentParams(prompt="a", close_session=False))
        return parent, result.extras["session_id"]

    parent, child_id = asyncio.run(spawn())
    work_dir = parent._cli.session.work_dir
    child_dir = _cache_dir(work_dir, child_id)
    assert child_dir.exists()

    close_session(parent)

    assert not child_dir.exists(), (
        f"sub-agent session dir {child_dir} outlived the (sync) parent close"
    )
