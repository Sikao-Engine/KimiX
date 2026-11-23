from __future__ import annotations

import importlib
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import apsw
import pytest
from kosong.message import Message
from kosong.tooling.empty import EmptyToolset

from kimi_cli.soul import kimisoul as kimisoul_module
from kimi_cli.soul.agent import Agent
from kimi_cli.soul.compaction import CompactionResult
from kimi_cli.soul.context import Context, replay_context
from kimi_cli.soul.history_index import HistoryIndex
from kimi_cli.soul.kimisoul import KimiSoul


class _NoDatabaseBus:
    """Isolate the orthogonal, explicitly approved broadcast SQLite surface."""

    def __init__(self, *, work_dir) -> None:
        self.path = Path(str(work_dir)) / ".kimix_cache" / "_broadcast.db"
        self.retention_ms = 1

    def prune(self) -> int:
        return 0

    def close(self) -> None:
        return None


def _forbidden(*args, **kwargs):
    raise AssertionError("session context/history attempted to open SQLite")


@pytest.mark.asyncio
async def test_soul_turn_compaction_revert_and_close_never_open_sqlite(
    runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(apsw, "Connection", _forbidden)
    try:
        aiosqlite = importlib.import_module("aiosqlite")
    except ModuleNotFoundError:
        pass
    else:
        monkeypatch.setattr(aiosqlite, "connect", _forbidden)
    monkeypatch.setattr(sqlite3, "connect", _forbidden)
    monkeypatch.setattr(kimisoul_module, "BroadcastBus", _NoDatabaseBus)
    monkeypatch.setattr(kimisoul_module, "wire_send", lambda message: None)
    monkeypatch.setattr(
        kimisoul_module,
        "perform_export",
        AsyncMock(return_value=(tmp_path / "compact.md", 1)),
    )

    context_path = runtime.session.dir / "context.jsonl"
    runtime.session.context_file = context_path
    context = Context.from_file_backend(context_path)
    await context.restore()
    agent = Agent(
        name="jsonl-test",
        system_prompt="JSONL system prompt",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    soul = KimiSoul(agent, context=context)
    await context.append_message(Message(role="user", content="original turn"))
    await context.update_token_count(100)
    await context.checkpoint(add_user_message=False)

    soul._compaction = type(
        "CompactionStub",
        (),
        {
            "compact": AsyncMock(
                return_value=CompactionResult(
                    messages=[Message(role="user", content="compact summary")],
                    usage=None,
                    compaction_id="abc",
                    shadowed_tokens=1,
                )
            )
        },
    )()
    await soul.compact_context(manual=True)
    assert [m.extract_text() for m in context.history] == ["compact summary"]

    # A new checkpoint/revert after compaction exercises the same live soul and
    # retained handles without any database driver becoming reachable.
    await context.checkpoint(add_user_message=False)
    await context.append_message(Message(role="user", content="discard me"))
    await context.revert_to(0)
    assert [m.extract_text() for m in context.history] == ["compact summary"]
    await soul.close()
    await soul.close()

    session_dir = runtime.session.dir
    assert not list(session_dir.glob("*.db"))
    assert not list(session_dir.glob("*.db-wal"))
    assert not list(session_dir.glob("*.db-shm"))
    assert [m.extract_text() for m in replay_context(context_path).messages] == ["compact summary"]
    history = HistoryIndex(jsonl_path=session_dir / "history.jsonl")
    history.load()
    assert any(turn["is_compacted"] for turn in history._turns)
    history.close()
