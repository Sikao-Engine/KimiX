"""Tests for the conversational Agent system."""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kaos.path import KaosPath

from kimix.base import MessageType
from kimi_cli.tools.subagent import (
    Agent,
    AgentClose,
    AgentCloseParams,
    AgentList,
    AgentListParams,
    SubAgentParams,
    _AgentConversationCollector,
    _background_tasks_for,
    _get_agent_session,
    _get_store,
    _register_agent_session,
    _register_entry,
    _resolve_prompt,
    _unregister_agent_session,
    _unregister_entry,
    wait_for_background_agents,
)
from kimi_cli.tools.subagent.store import (
    AgentSessionEntry,
    AgentSessionStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# AgentSessionStore tests
# ---------------------------------------------------------------------------
async def test_store_get_put(mock_sub_session: MagicMock) -> None:
    store = AgentSessionStore()
    assert store.get("nonexistent") is None

    entry = AgentSessionEntry(
        session=mock_sub_session,
        session_id="s1",
        created_at=time.time(),
        last_accessed=time.time(),
        conversation_history=[],
        total_turns=0,
    )
    store.put(entry)
    assert store.get("s1") is entry


async def test_store_close(mock_sub_session: MagicMock) -> None:
    store = AgentSessionStore()
    entry = AgentSessionEntry(
        session=mock_sub_session,
        session_id="s1",
        created_at=time.time(),
        last_accessed=time.time(),
        conversation_history=[],
        total_turns=0,
    )
    store.put(entry)
    assert store.close("s1") is True
    assert store.get("s1") is None
    assert store.close("s1") is False


async def test_store_list_active(mock_sub_session: MagicMock) -> None:
    store = AgentSessionStore()
    entry = AgentSessionEntry(
        session=mock_sub_session,
        session_id="s1",
        created_at=time.time(),
        last_accessed=time.time(),
        conversation_history=[],
        total_turns=3,
    )
    store.put(entry)
    active = store.list_active()
    assert len(active) == 1
    assert active[0]["session_id"] == "s1"
    assert active[0]["total_turns"] == 3
    assert active[0]["state"] == "running"


async def test_store_lru_eviction(mock_sub_session: MagicMock) -> None:
    store = AgentSessionStore()
    store.MAX_SESSIONS = 3

    for i in range(4):
        entry = AgentSessionEntry(
            session=mock_sub_session,
            session_id=f"s{i}",
            created_at=time.time(),
            last_accessed=time.time() + i,
            conversation_history=[],
            total_turns=0,
        )
        store.put(entry)

    assert len(store.entries) == 4
    with patch(
        "kimix.utils.close_session_async", new_callable=AsyncMock
    ) as mock_close:
        await store.evict_lru_if_needed()
        assert mock_close.await_count == 2

    assert len(store.entries) == 2
    assert store.get("s0") is None  # oldest
    assert store.get("s1") is None
    assert store.get("s2") is not None
    assert store.get("s3") is not None


async def test_store_notifies_when_a_session_leaves(
    mock_sub_session: MagicMock,
) -> None:
    """Releases are reported so no registry keeps a closed session around."""
    closed: list[str] = []
    store = AgentSessionStore(on_close=closed.append)
    for i in range(4):
        store.put(
            AgentSessionEntry(
                session=mock_sub_session,
                session_id=f"s{i}",
                created_at=time.time(),
                last_accessed=time.time() + i,
                conversation_history=[],
                total_turns=0,
            )
        )

    assert store.close("s3") is True
    assert closed == ["s3"]

    store.MAX_SESSIONS = 3
    with patch(
        "kimix.utils.close_session_async", new_callable=AsyncMock
    ):
        await store.evict_lru_if_needed()

    assert store.get("s0") is None  # oldest, evicted to get back under the cap
    assert sorted(closed) == ["s0", "s3"]


async def test_store_notification_failure_does_not_break_eviction(
    mock_sub_session: MagicMock,
) -> None:
    def boom(session_id: str) -> None:
        raise RuntimeError("notify boom")

    store = AgentSessionStore(on_close=boom)
    store.MAX_SESSIONS = 2
    for i in range(3):
        store.put(
            AgentSessionEntry(
                session=mock_sub_session,
                session_id=f"b{i}",
                created_at=time.time(),
                last_accessed=time.time() + i,
                conversation_history=[],
                total_turns=0,
            )
        )

    with patch(
        "kimix.utils.close_session_async", new_callable=AsyncMock
    ):
        await store.evict_lru_if_needed()

    assert len(store.entries) == 1  # eviction completed despite the callback


# ---------------------------------------------------------------------------
# _AgentConversationCollector tests
# ---------------------------------------------------------------------------
async def test_collector_text_only() -> None:
    col = _AgentConversationCollector()
    col.finalize_user_turn("hello")
    col.consume("world", MessageType.Text)
    text = col.finalize_assistant_turn()
    assert text == "world"
    assert len(col.turns) == 2
    assert col.turns[0].role == "user"
    assert col.turns[1].role == "assistant"
    assert col.turns[1].metadata == {"type": "text"}


async def test_collector_thinking_excluded_from_output() -> None:
    col = _AgentConversationCollector()
    col.consume("text1", MessageType.Text)
    col.consume("think1", MessageType.Thinking)
    col.consume("text2", MessageType.Text)
    text = col.finalize_assistant_turn()
    assert text == "text1text2"
    assert any(t.metadata == {"type": "thinking"} for t in col.turns)


async def test_collector_tool_call_and_result() -> None:
    col = _AgentConversationCollector()
    col.consume("ToolA args", MessageType.ToolCalling)
    col.consume("[ToolResult] ok", MessageType.ToolResult)
    text = col.finalize_assistant_turn()
    assert text == ""
    roles = [t.role for t in col.turns]
    assert roles == ["tool", "tool"]
    assert col.turns[0].metadata == {"type": "tool_call"}
    assert col.turns[1].metadata == {"type": "tool_result"}


async def test_collector_empty_output() -> None:
    col = _AgentConversationCollector()
    col.finalize_user_turn("prompt")
    text = col.finalize_assistant_turn()
    assert text == ""


# ---------------------------------------------------------------------------
# Agent tool tests
# ---------------------------------------------------------------------------
async def test_agent_recursive_guard(mock_session: MagicMock) -> None:
    mock_session.custom_config = {"is_sub_agent": True}
    agent = Agent(mock_session)
    result = await agent(SubAgentParams(prompt="test", run_in_background=False))
    assert result.is_error
    assert "Recursive sub-agent call detected" in result.message


async def test_agent_new_session(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt:
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ):
                agent = Agent(mock_session)
                result = await agent(SubAgentParams(prompt="do X", run_in_background=False))

    assert not result.is_error
    assert result.extras is not None
    assert "session_id" in result.extras
    assert result.extras["status"] == "closed"
    assert result.extras["turn_count"] >= 1
    mock_create.assert_awaited_once()
    mock_prompt.assert_awaited_once()


async def test_agent_keep_alive_stores_session(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ):
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ) as mock_close:
                agent = Agent(mock_session)
                result = await agent(SubAgentParams(prompt="do X", close_session=False, run_in_background=False))

    assert not result.is_error
    assert result.extras["status"] == "continued"
    store = _get_store(mock_session)
    assert store.get(result.extras["session_id"]) is not None
    mock_close.assert_not_awaited()


async def test_agent_reuse_session(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}
    store = _get_store(mock_session)
    store.put(
        AgentSessionEntry(
            session=mock_sub_session,
            session_id="reuse-id",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=0,
        )
    )

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ):
            agent = Agent(mock_session)
            result = await agent(
                SubAgentParams(prompt="follow up", session_id="reuse-id", close_session=False, run_in_background=False)
            )

    assert not result.is_error
    assert result.extras["session_id"] == "reuse-id"
    mock_create.assert_not_awaited()


async def test_agent_close_session_param(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}
    store = _get_store(mock_session)
    store.put(
        AgentSessionEntry(
            session=mock_sub_session,
            session_id="close-id",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=0,
        )
    )

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ):
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ) as mock_close:
                agent = Agent(mock_session)
                result = await agent(
                    SubAgentParams(
                        prompt="do X", session_id="close-id", close_session=True, run_in_background=False)
                )

    assert not result.is_error
    assert result.extras["status"] == "closed"
    assert store.get("close-id") is None
    mock_close.assert_awaited_once()


async def test_agent_return_history(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}

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
                result = await agent(SubAgentParams(prompt="do X", return_history=True, run_in_background=False))

    assert not result.is_error
    assert "conversation_history" in result.extras
    history = result.extras["conversation_history"]
    assert isinstance(history, list)
    assert any(h["role"] == "user" for h in history)


async def test_agent_error_path(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt:
            mock_prompt.side_effect = RuntimeError("boom")
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ) as mock_close:
                agent = Agent(mock_session)
                result = await agent(SubAgentParams(prompt="do X", close_session=False, run_in_background=False))

    assert result.is_error
    assert "boom" in result.message
    assert result.extras["status"] == "closed"
    mock_close.assert_awaited_once()


async def test_agent_error_saves_prompt_file(
    mock_session: MagicMock, mock_sub_session: MagicMock, tmp_path: Path
) -> None:
    """A failed sub-agent run saves the effective prompt for retry."""
    mock_session.custom_config = {"chat_provider": None}
    saved_files: list[Path] = []

    def fake_create_script_file(content: str, ext: str = ".md") -> str:
        target = tmp_path / f"saved_{len(saved_files)}{ext}"
        target.write_text(content, encoding="utf-8")
        saved_files.append(target)
        return str(target)

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt:
            mock_prompt.side_effect = RuntimeError("boom")
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ) as mock_close:
                with patch(
                    "kimi_cli.tools.subagent._create_script_file",
                    side_effect=fake_create_script_file,
                ):
                    agent = Agent(mock_session)
                    result = await agent(
                        SubAgentParams(prompt="do X", close_session=False, run_in_background=False)
                    )

    assert result.is_error
    assert "boom" in result.message
    assert "[prompt saved to" in result.message
    assert "prompt=@" in result.message
    expected_display = str(tmp_path / "saved_0.md").replace("\\", "/")
    assert result.extras["prompt_file"] == expected_display
    # The saved file contains the exact prompt string sent to prompt_async.
    sent_prompt = mock_prompt.await_args.kwargs["prompt_str"]
    assert saved_files[0].read_text(encoding="utf-8") == sent_prompt
    mock_close.assert_awaited_once()


async def test_agent_prompt_from_file(
    mock_session: MagicMock, mock_sub_session: MagicMock, tmp_path: Path
) -> None:
    """prompt=@path reads the task text from the referenced file."""
    task_file = tmp_path / "task.md"
    task_file.write_text("do the file task", encoding="utf-8")
    mock_session.custom_config = {"chat_provider": None}
    mock_session.work_dir = KaosPath(str(tmp_path))

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt:
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ):
                agent = Agent(mock_session)
                result = await agent(
                    SubAgentParams(prompt="@task.md", close_session=False, run_in_background=False)
                )

    assert not result.is_error
    prompt_str = mock_prompt.await_args.kwargs["prompt_str"]
    assert "do the file task" in prompt_str
    assert "@task.md" not in prompt_str


async def test_agent_prompt_file_missing(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """prompt=@missing.md fails with a clear prompt-file error."""
    mock_session.custom_config = {"chat_provider": None}

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        agent = Agent(mock_session)
        result = await agent(SubAgentParams(prompt="@missing.md", run_in_background=False))

    assert result.is_error
    assert "prompt file not found" in result.message


async def test_resolve_prompt_cwd_fallback(monkeypatch, tmp_path: Path) -> None:
    """Relative @path falls back to the process CWD when not under base_dir.

    The error-path retry hint points at the shared temp folder
    (``.kimix_cache/tmp_<pid>/<n>.md``), which is CWD-relative; the fallback
    keeps that retry working when the session work dir differs from CWD.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "task.md").write_text("from cwd", encoding="utf-8")

    assert _resolve_prompt("@task.md", work_dir) == "from cwd"
    with pytest.raises(FileNotFoundError):
        _resolve_prompt("@nope.md", work_dir)


async def test_agent_long_prompt_offloads_to_temp_file(
    mock_session: MagicMock, mock_sub_session: MagicMock, tmp_path: Path
) -> None:
    """Very long prompts are offloaded to a temp file the sub-agent reads."""
    mock_session.custom_config = {"chat_provider": None}
    prompt_text = "x" * (100 * 1024 + 1)
    calls: list[tuple[str, str]] = []
    fake_path = tmp_path / "saved.md"

    def fake_create_script_file(content: str, ext: str = ".md") -> str:
        calls.append((content, ext))
        return str(fake_path)

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt:
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ):
                with patch(
                    "kimi_cli.tools.subagent._create_script_file",
                    side_effect=fake_create_script_file,
                ):
                    agent = Agent(mock_session)
                    result = await agent(SubAgentParams(prompt=prompt_text, run_in_background=False))

    assert not result.is_error
    assert calls == [(prompt_text, ".md")]
    prompt_str = mock_prompt.await_args.kwargs["prompt_str"]
    assert "Please read the task from `" in prompt_str
    assert "` and execute it." in prompt_str


async def test_agent_lru_eviction(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}
    store = _get_store(mock_session)
    store.MAX_SESSIONS = 2

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ):
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ) as mock_close:
                agent = Agent(mock_session)
                for i in range(3):
                    mock_sub = MagicMock()
                    mock_sub.id = f"sub-{i}"
                    mock_sub.get_custom_config.return_value = {}
                    mock_sub.close = AsyncMock()
                    mock_create.return_value = mock_sub
                    await agent(
                        SubAgentParams(prompt=f"task {i}", close_session=False, run_in_background=False)
                    )

    assert len(store.entries) == 2
    mock_close.assert_awaited_once()


# ---------------------------------------------------------------------------
# inherit_context tests (mirrors CLI /store + /load session-copy logic)
# ---------------------------------------------------------------------------
async def test_agent_inherit_context_copies_parent_session(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """inherit_context=True copies the parent session dir into the new sub id."""
    mock_session.custom_config = {"chat_provider": None}
    mock_session.id = "parent-1"
    mock_session.work_dir = KaosPath(".")

    with patch(
        "kimi_cli.tools.subagent.Session.copy", new_callable=AsyncMock
    ) as mock_copy:
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
                        SubAgentParams(prompt="do X", inherit_context=True, run_in_background=False)
                    )

    assert not result.is_error
    sub_id = result.extras["session_id"]
    mock_copy.assert_awaited_once()
    copied_work_dir, copied_parent, copied_target = mock_copy.await_args.args
    assert copied_work_dir == KaosPath(".")
    assert copied_parent == "parent-1"
    assert copied_target == sub_id
    # The sub-agent session resumes from the copied session id (like /load).
    assert mock_create.await_args.kwargs["session_id"] == sub_id
    assert mock_create.await_args.kwargs["resume"] is True


async def test_agent_inherit_context_with_explicit_session_id(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """The parent context is copied into the explicitly requested session id."""
    mock_session.custom_config = {"chat_provider": None}
    mock_session.id = "parent-1"
    mock_session.work_dir = KaosPath(".")

    with patch(
        "kimi_cli.tools.subagent.Session.copy", new_callable=AsyncMock
    ) as mock_copy:
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
                            prompt="do X",
                            session_id="fresh-sub",
                            inherit_context=True, run_in_background=False)
                    )

    assert not result.is_error
    assert result.extras["session_id"] == "fresh-sub"
    mock_copy.assert_awaited_once_with(
        KaosPath("."), "parent-1", "fresh-sub"
    )
    assert mock_create.await_args.kwargs["session_id"] == "fresh-sub"


async def test_agent_inherit_context_ignored_on_reuse(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """Reusing an active sub-agent session skips the context copy."""
    mock_session.custom_config = {"chat_provider": None}
    store = _get_store(mock_session)
    store.put(
        AgentSessionEntry(
            session=mock_sub_session,
            session_id="reuse-id",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=0,
        )
    )

    with patch(
        "kimi_cli.tools.subagent.Session.copy", new_callable=AsyncMock
    ) as mock_copy:
        with patch(
            "kimix.utils._create_session_async", new_callable=AsyncMock
        ) as mock_create:
            with patch(
                "kimix.utils.prompt_async", new_callable=AsyncMock
            ):
                agent = Agent(mock_session)
                result = await agent(
                    SubAgentParams(
                        prompt="follow up",
                        session_id="reuse-id",
                        inherit_context=True,
                        close_session=False, run_in_background=False)
                )

    assert not result.is_error
    assert result.extras["session_id"] == "reuse-id"
    mock_copy.assert_not_awaited()
    mock_create.assert_not_awaited()


async def test_agent_inherit_context_without_parent_id_errors(
    mock_session: MagicMock,
) -> None:
    """A parent with no resolvable session id cannot donate its context."""
    mock_session.custom_config = {"chat_provider": None}
    mock_session.id = ""
    mock_session._cli = None

    with patch(
        "kimi_cli.tools.subagent.Session.copy", new_callable=AsyncMock
    ) as mock_copy:
        agent = Agent(mock_session)
        result = await agent(SubAgentParams(prompt="do X", inherit_context=True, run_in_background=False))

    assert result.is_error
    assert "Cannot inherit parent context" in result.message
    mock_copy.assert_not_awaited()


async def test_agent_inherit_context_resets_system_prompt(
    mock_session: MagicMock,
) -> None:
    """The inherited parent system prompt is replaced with the sub-agent's."""
    agent = Agent(mock_session)

    sub_agent = MagicMock()
    sub_agent.get_system_prompt.return_value = "sub-agent prompt"
    sub_agent.system_prompt_cached = "parent prompt"
    context = MagicMock()
    context.write_system_prompt = AsyncMock()
    soul = MagicMock()
    soul.agent = sub_agent
    soul.context = context
    cli = MagicMock()
    cli.soul = soul
    sub_session = MagicMock()
    sub_session._cli = cli

    await agent._reset_inherited_system_prompt(sub_session)

    assert sub_agent.system_prompt_cached is None
    context.write_system_prompt.assert_awaited_once_with("sub-agent prompt")


async def test_agent_inherit_context_reset_skips_when_soul_missing(
    mock_session: MagicMock,
) -> None:
    """System prompt reset is best-effort: skips without soul internals."""
    agent = Agent(mock_session)
    sub_session = MagicMock()
    sub_session._cli = None
    await agent._reset_inherited_system_prompt(sub_session)


async def test_agent_inherit_context_copies_real_session_dir(
    monkeypatch, tmp_path: Path
) -> None:
    """Integration: the parent session dir is really copied (Session.copy)."""
    from kimi_cli.metadata import Metadata, save_metadata
    from kimi_cli.session import Session as CliSession
    from kimi_cli.soul.context_db import ContextDB
    from kosong.message import Message

    share_dir = tmp_path / "share"
    share_dir.mkdir()
    monkeypatch.setattr("kimi_cli.share.get_share_dir", lambda: share_dir)
    monkeypatch.setattr("kimi_cli.metadata.get_share_dir", lambda: share_dir)

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    work = KaosPath(str(work_dir)).canonical()

    metadata = Metadata()
    wd_meta = metadata.new_work_dir_meta(work)
    save_metadata(metadata)

    parent_id = "parent-1"
    parent_dir = wd_meta.sessions_dir / parent_id
    parent_dir.mkdir(parents=True, exist_ok=True)

    db = ContextDB(parent_dir / "context.db")
    await db.initialize()
    await db.append_messages(
        [
            Message(role="user", content="hello parent"),
            Message(role="assistant", content="hi there"),
        ]
    )
    await db.close()

    parent = MagicMock()
    parent.id = parent_id
    parent.work_dir = work
    agent = Agent(parent)

    target_id = "sub-copy"
    await agent._inherit_parent_context(target_id)

    copied = await CliSession.find(work, target_id)
    assert copied is not None

    copied_db = ContextDB(parent_dir.parent / target_id / "context.db")
    await copied_db.initialize()
    try:
        messages = await copied_db.get_messages()
    finally:
        await copied_db.close()
    texts: list[str] = []
    for m in messages:
        if isinstance(m.content, str):
            texts.append(m.content)
        else:
            texts.extend(
                part.text for part in m.content
                if getattr(part, "type", None) == "text"
            )
    assert texts == ["hello parent", "hi there"]



# ---------------------------------------------------------------------------
# Companion tool tests
# ---------------------------------------------------------------------------
async def test_agent_list(mock_session: MagicMock, mock_sub_session: MagicMock) -> None:
    store = _get_store(mock_session)
    store.put(
        AgentSessionEntry(
            session=mock_sub_session,
            session_id="list-id",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=1,
        )
    )
    agent_list = AgentList(mock_session)
    result = await agent_list(AgentListParams())
    assert not result.is_error
    assert "list-id" in result.output


async def test_agent_close(mock_session: MagicMock, mock_sub_session: MagicMock) -> None:
    store = _get_store(mock_session)
    store.put(
        AgentSessionEntry(
            session=mock_sub_session,
            session_id="close-id",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=1,
        )
    )
    with patch(
        "kimix.utils.close_session_async", new_callable=AsyncMock
    ) as mock_close:
        agent_close = AgentClose(mock_session)
        result = await agent_close(AgentCloseParams(session_id="close-id"))

    assert not result.is_error
    assert store.get("close-id") is None
    mock_close.assert_awaited_once()


async def test_agent_close_not_found(mock_session: MagicMock) -> None:
    agent_close = AgentClose(mock_session)
    result = await agent_close(AgentCloseParams(session_id="missing"))
    assert result.is_error
    assert "Session not found" in result.message


# ---------------------------------------------------------------------------
# Steer helpers (background completion notices)
# ---------------------------------------------------------------------------
class _FakeSteer:
    """Stand-in for ``kimi_cli.soul.steer.Steer`` recording pushed content."""

    def __init__(self, delivered: bool = True) -> None:
        self.delivered = delivered
        self.pushed: list[str] = []

    async def push(self, content: str) -> bool:
        self.pushed.append(content)
        return self.delivered


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


async def test_agent_resolve_session_registers_parent_and_child(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    mock_session.custom_config = {"chat_provider": None}
    mock_session.id = "parent-1"

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
                result = await agent(SubAgentParams(prompt="do X", close_session=False, run_in_background=False))

    assert not result.is_error
    child_id = result.extras["session_id"]
    sub_config = mock_sub_session.get_custom_config()
    assert sub_config["is_sub_agent"] is True
    assert _get_agent_session(child_id) is mock_sub_session
    # Close-path cleanup removes the registration.
    _unregister_agent_session(child_id)
    assert _get_agent_session(child_id) is None


# ---------------------------------------------------------------------------
# Work-dir inheritance + context_files base (reflection fix)
# ---------------------------------------------------------------------------


async def test_agent_work_dir_inherited_by_sub_session(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """Sub-agent sessions must inherit the parent's working directory so the
    system prompt WORK DIR and relative paths resolve against the same repo."""
    from kaos.path import KaosPath

    mock_session.work_dir = KaosPath(str(Path.cwd()))
    mock_session.custom_config = {"chat_provider": None}

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
                await agent(SubAgentParams(prompt="do X", run_in_background=False))

    assert mock_create.await_args is not None
    _, kwargs = mock_create.await_args
    assert kwargs.get("work_dir") == KaosPath(str(Path.cwd()))


async def test_agent_work_dir_sdk_wrapped_session(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """SDK-wrapped sessions (session._cli.session.work_dir) resolve too."""
    from kaos.path import KaosPath

    inner = MagicMock()
    inner.work_dir = KaosPath(str(Path.cwd()))
    cli = MagicMock()
    cli.session = inner
    mock_session.work_dir = None
    mock_session._cli = cli
    mock_session.custom_config = {"chat_provider": None}

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
                await agent(SubAgentParams(prompt="do X", run_in_background=False))

    assert mock_create.await_args is not None
    _, kwargs = mock_create.await_args
    assert kwargs.get("work_dir") == KaosPath(str(Path.cwd()))


async def test_agent_context_files_resolve_against_work_dir(
    mock_session: MagicMock, mock_sub_session: MagicMock, tmp_path
) -> None:
    """context_files must resolve against the parent's work_dir, not the
    session cache dir (self._session.dir)."""
    from kaos.path import KaosPath

    marker = tmp_path / "marker.txt"
    marker.write_text("hello marker", encoding="utf-8")
    mock_session.work_dir = KaosPath(str(tmp_path))
    mock_session.custom_config = {"chat_provider": None}
    # Ensure the session dir differs from the work dir: a file that exists in
    # the session dir but not the work dir must NOT be picked up.
    session_dir = tmp_path / "session_cache"
    session_dir.mkdir(exist_ok=True)
    mock_session.dir = str(session_dir)

    captured: list[str] = []

    async def fake_prompt(prompt_str, **kwargs):
        captured.append(prompt_str)
        return None

    with patch(
        "kimix.utils._create_session_async", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = mock_sub_session
        with patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock, side_effect=fake_prompt
        ):
            with patch(
                "kimix.utils.close_session_async", new_callable=AsyncMock
            ):
                agent = Agent(mock_session)
                await agent(
                    SubAgentParams(prompt="do X", context_files=["marker.txt"], run_in_background=False)
                )

    assert captured, "prompt_async should have been called"
    assert "<file path='marker.txt'>\nhello marker\n</file>" in captured[0]


async def test_agent_work_dir_none_falls_back_to_cwd(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """No resolvable work_dir on the parent must not crash the create call."""
    mock_session.work_dir = None
    mock_session._cli = None
    mock_session.custom_config = {"chat_provider": None}

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
                result = await agent(SubAgentParams(prompt="do X", run_in_background=False))

    assert not result.is_error
    assert mock_create.await_args is not None
    _, kwargs = mock_create.await_args
    assert kwargs.get("work_dir") is None


# ---------------------------------------------------------------------------
# Background subagent lifecycle (run_in_background=true, the default)
# ---------------------------------------------------------------------------


async def _pump_background(n: int = 50) -> None:
    """Let detached background tasks run to completion.

    Every ``await asyncio.sleep(0)`` yields one event-loop iteration; the
    mocked ``prompt_async`` completes without further scheduling, so a few
    dozen iterations are far more than the task chain needs.
    """
    for _ in range(n):
        await asyncio.sleep(0)


def _capturing_prompt(captured: list[str], text: str = "done"):
    """A ``prompt_async`` stand-in recording the prompt and emitting text."""

    async def _mock(*, prompt_str, session, output_function, **kwargs):
        captured.append(prompt_str)
        if output_function:
            output_function(text, MessageType.Text)

    return _mock


async def test_agent_background_returns_durable_id_immediately(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """The default (background) call must not wait for the subagent result."""
    mock_session.custom_config = {"chat_provider": None}
    prompt_started = asyncio.Event()

    async def _blocking_prompt(*args, **kwargs):
        prompt_started.set()
        await asyncio.Event().wait()  # never completes on its own

    with (
        patch(
            "kimix.utils._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt,
        patch("kimix.utils.close_session_async", new_callable=AsyncMock),
    ):
        mock_create.return_value = mock_sub_session
        mock_prompt.side_effect = _blocking_prompt
        agent = Agent(mock_session)
        result = await agent(SubAgentParams(prompt="do X"))

    assert not result.is_error
    assert result.extras is not None
    assert result.extras["status"] == "running"
    session_id = result.extras["session_id"]
    assert session_id
    assert session_id in result.output

    # The tool returned BEFORE the subagent ran: registered as running, and
    # the LLM has not been asked yet.
    assert not prompt_started.is_set()
    mock_prompt.assert_not_awaited()
    # The detached run is tracked on the owning session (not globally).
    assert len(_background_tasks_for(mock_session)) == 1
    store = _get_store(mock_session)
    entry = store.get(session_id)
    assert entry is not None
    assert entry.state == "running"
    assert entry.is_active

    # Clean up the deliberately never-finishing background task.
    for task in list(_background_tasks_for(mock_session)):
        task.cancel()
    await _pump_background()


async def test_agent_background_completes_and_stays_addressable(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """A finished background subagent stays listed and resumable by default."""
    mock_session.custom_config = {"chat_provider": None}
    captured: list[str] = []

    with (
        patch(
            "kimix.utils._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt,
        patch(
            "kimix.utils.close_session_async", new_callable=AsyncMock
        ) as mock_close,
    ):
        mock_create.return_value = mock_sub_session
        mock_prompt.side_effect = _capturing_prompt(captured, "answer")
        agent = Agent(mock_session)
        result = await agent(SubAgentParams(prompt="do X"))
        session_id = result.extras["session_id"]
        await _pump_background()

    assert captured, "background run never executed the prompt"
    store = _get_store(mock_session)
    entry = store.get(session_id)
    assert entry is not None, "completed background subagent left the store"
    assert entry.state == "completed"
    assert entry.is_active
    # Default background runs keep the session open for later turns.
    mock_close.assert_not_awaited()
    assert _get_agent_session(session_id) is mock_sub_session

    # list_agents still reports the completed subagent (durable id recall).
    listing = await AgentList(mock_session)(AgentListParams())
    assert not listing.is_error
    assert session_id in listing.output
    assert '"completed"' in listing.output


async def test_agent_background_explicit_close_session_closes(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """close_session=true on a background run closes it on completion."""
    mock_session.custom_config = {"chat_provider": None}

    with (
        patch(
            "kimix.utils._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt,
        patch(
            "kimix.utils.close_session_async", new_callable=AsyncMock
        ) as mock_close,
    ):
        mock_create.return_value = mock_sub_session
        mock_prompt.side_effect = _capturing_prompt([], "answer")
        agent = Agent(mock_session)
        result = await agent(
            SubAgentParams(prompt="do X", close_session=True)
        )
        session_id = result.extras["session_id"]
        await _pump_background()

    assert result.extras["status"] == "running"
    mock_close.assert_awaited_once()
    assert _get_store(mock_session).get(session_id) is None
    assert _get_agent_session(session_id) is None


async def test_agent_foreground_default_still_closes(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """Foreground default semantics are unchanged: the session closes."""
    mock_session.custom_config = {"chat_provider": None}

    with (
        patch(
            "kimix.utils._create_session_async", new_callable=AsyncMock
        ) as mock_create,
        patch(
            "kimix.utils.prompt_async", new_callable=AsyncMock
        ) as mock_prompt,
        patch(
            "kimix.utils.close_session_async", new_callable=AsyncMock
        ) as mock_close,
    ):
        mock_create.return_value = mock_sub_session
        mock_prompt.side_effect = _capturing_prompt([], "answer")
        agent = Agent(mock_session)
        result = await agent(
            SubAgentParams(prompt="do X", run_in_background=False)
        )

    assert not result.is_error
    assert result.extras["status"] == "closed"
    mock_close.assert_awaited_once()


async def test_agent_background_notifies_parent_on_completion(
    mock_session: MagicMock, mock_sub_session: MagicMock
) -> None:
    """When the background run settles the parent receives a notice steer."""
    mock_session.custom_config = {"chat_provider": None}
    mock_session.id = "main-1"
    parent_sdk = _fake_sdk_session("main-1")
    _register_agent_session("main-1", parent_sdk)
    fake = _FakeSteer(delivered=True)
    try:
        with (
            patch(
                "kimix.utils._create_session_async", new_callable=AsyncMock
            ) as mock_create,
            patch(
                "kimix.utils.prompt_async", new_callable=AsyncMock
            ) as mock_prompt,
            patch("kimix.utils.close_session_async", new_callable=AsyncMock),
            patch(
                "kimi_cli.soul.steer.Steer.from_session", return_value=fake
            ) as mock_from,
        ):
            mock_create.return_value = mock_sub_session
            mock_prompt.side_effect = _capturing_prompt([], "all done")
            agent = Agent(mock_session)
            result = await agent(SubAgentParams(prompt="do X"))
            session_id = result.extras["session_id"]
            await _pump_background()
    finally:
        _unregister_agent_session("main-1")

    assert mock_from.call_count == 1
    assert len(fake.pushed) == 1
    assert session_id in fake.pushed[0]
    assert "all done" in fake.pushed[0]


async def test_wait_for_background_agents_waits_until_settled(
    mock_session: MagicMock,
) -> None:
    """``wait_for_background_agents`` blocks until the session's own detached
    background tasks settle (used by ``prompt_async`` when a session finishes
    while its subagents are still running)."""
    finished: list[str] = []

    async def _slow() -> None:
        await asyncio.sleep(0.01)
        finished.append("slow")

    async def _fast() -> None:
        finished.append("fast")

    tasks = _background_tasks_for(mock_session)
    slow = asyncio.create_task(_slow())
    fast = asyncio.create_task(_fast())
    tasks.add(slow)
    tasks.add(fast)
    slow.add_done_callback(tasks.discard)
    fast.add_done_callback(tasks.discard)

    await wait_for_background_agents(mock_session)

    assert sorted(finished) == ["fast", "slow"]
    # Settled tasks are discarded from the owning set via the done callback.
    await _pump_background()
    assert _background_tasks_for(mock_session) == set()


async def test_wait_for_background_agents_sdk_wrapped_session(
    mock_session: MagicMock,
) -> None:
    """The SDK wrapper (``session._cli.session``) resolves to the same task
    set as the bare CLI session, so ``prompt_async`` can wait on it."""
    done = asyncio.Event()

    async def _work() -> None:
        done.set()

    tasks = _background_tasks_for(mock_session)
    task = asyncio.create_task(_work())
    tasks.add(task)
    task.add_done_callback(tasks.discard)

    sdk = MagicMock()
    cli = MagicMock()
    cli.session = mock_session
    sdk._cli = cli

    await wait_for_background_agents(sdk)
    assert done.is_set()


async def test_agent_resume_while_running_rejected(
    mock_session: MagicMock,
) -> None:
    """Resuming a subagent that is mid-run errors instead of a silent no-op."""
    mock_session.custom_config = {"chat_provider": None}
    busy = MagicMock()
    busy.id = "busy-1"
    busy.get_custom_config.return_value = {}
    busy._cancel_event = asyncio.Event()  # truthy while a prompt owns it
    store = _get_store(mock_session)
    store.put(
        AgentSessionEntry(
            session=busy,
            session_id="busy-1",
            created_at=time.time(),
            last_accessed=time.time(),
            conversation_history=[],
            total_turns=1,
            state="running",
        )
    )

    agent = Agent(mock_session)
    result = await agent(
        SubAgentParams(
            prompt="more work", session_id="busy-1", run_in_background=False
        )
    )

    assert result.is_error
    assert "currently running" in result.message


async def test_store_eviction_prefers_completed_sessions(
    mock_sub_session: MagicMock,
) -> None:
    """LRU pressure evicts finished subagents before running ones."""
    store = AgentSessionStore()
    store.MAX_SESSIONS = 3
    now = time.time()

    def _entry(sid: str, last: float, state: str) -> AgentSessionEntry:
        return AgentSessionEntry(
            session=mock_sub_session,
            session_id=sid,
            created_at=now,
            last_accessed=last,
            conversation_history=[],
            total_turns=1,
            state=state,  # type: ignore[arg-type]
        )

    store.put(_entry("running-1", now - 500, "running"))
    store.put(_entry("done-1", now - 100, "completed"))
    store.put(_entry("done-2", now - 50, "completed"))

    with patch(
        "kimix.utils.close_session_async", new_callable=AsyncMock
    ):
        await store.evict_lru_if_needed()

    # Only one entry had to go: the least-recently-accessed *completed* one.
    # The running session survives even though it is the oldest overall.
    assert store.get("running-1") is not None
    assert store.get("done-2") is not None
    assert store.get("done-1") is None
