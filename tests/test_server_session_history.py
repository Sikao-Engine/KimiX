from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import orjson
import pytest
from kaos.path import KaosPath

from kimix.server import session_manager as session_manager_module
from kimix.server.session_manager import SessionManager


def _disk_session(tmp_path, session_id: str = "ses_disk"):
    context_file = tmp_path / session_id / "context.jsonl"
    context_file.parent.mkdir(parents=True)
    context_file.write_text(
        "\n".join(
            [
                json.dumps({"role": "_system_prompt", "content": "hidden"}),
                json.dumps({"role": "user", "content": "hello from disk"}),
                json.dumps({"role": "assistant", "content": [{"text": "hi"}]}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return SimpleNamespace(
        id=session_id,
        title="Disk session",
        updated_at=123.0,
        work_dir=KaosPath.unsafe_from_local_path(tmp_path),
        context_file=context_file,
    )


@pytest.mark.asyncio
async def test_list_sessions_includes_persisted_session(tmp_path, monkeypatch):
    from kimi_cli.session import Session as CliSession

    disk_session = _disk_session(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(CliSession, "list", AsyncMock(return_value=[disk_session]))

    manager = SessionManager()

    sessions = await manager.list_sessions()

    assert [session.id for session in sessions] == ["ses_disk"]
    assert sessions[0].title == "Disk session"
    assert sessions[0].directory == str(disk_session.work_dir)


@pytest.mark.asyncio
async def test_get_messages_reads_persisted_context(tmp_path, monkeypatch):
    from kimi_cli.session import Session as CliSession

    disk_session = _disk_session(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(CliSession, "list", AsyncMock(return_value=[disk_session]))

    manager = SessionManager()

    messages = await manager.get_messages("ses_disk")

    assert [message["info"]["role"] for message in messages] == [
        "user",
        "assistant",
    ]
    assert messages[0]["parts"][0]["text"] == "hello from disk"
    assert messages[1]["parts"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_prompt_async_restores_persisted_session_before_return(tmp_path, monkeypatch):
    from kimi_cli.session import Session as CliSession

    disk_session = _disk_session(tmp_path)
    sdk_session = SimpleNamespace(prompt=AsyncMock())
    scheduled = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(CliSession, "list", AsyncMock(return_value=[disk_session]))
    monkeypatch.setattr(CliSession, "find", AsyncMock(return_value=disk_session))
    create_session = AsyncMock(return_value=sdk_session)
    monkeypatch.setattr(session_manager_module, "_create_session_async", create_session)
    monkeypatch.setattr(
        session_manager_module.asyncio,
        "ensure_future",
        lambda coroutine: scheduled.append(coroutine),
    )

    manager = SessionManager()

    await manager.prompt_async("ses_disk", "continue")

    assert scheduled
    create_session.assert_awaited_once_with(
        session_id="ses_disk",
        work_dir=KaosPath.unsafe_from_local_path(tmp_path),
        resume=True,
    )
    scheduled[0].close()


def test_load_context_messages_uses_latest_100_active_entries(tmp_path):
    disk = _disk_session(tmp_path)
    records = [
        {"role": "user", "content": "obsolete", "created_at": 1.0},
        {"role": "_reset"},
    ]
    records.extend(
        {"role": "user", "content": f"active {index}", "created_at": 10.0 + index}
        for index in range(105)
    )
    disk.context_file.write_bytes(
        b"".join(
            orjson.dumps(record, option=orjson.OPT_APPEND_NEWLINE)
            for record in records
        )
    )
    manager = SessionManager()
    messages = manager._load_context_messages(disk.id, disk.context_file)
    assert len(messages) == 100
    assert messages[0].parts[0].text == "active 5"
    assert messages[-1].parts[0].text == "active 104"
    assert messages[0].info.time["created"] == 15000
    assert all(message.parts[0].text != "obsolete" for message in messages)


@pytest.mark.asyncio
async def test_search_messages_active_view_filter_sort_paginate_and_cjk(
    tmp_path, monkeypatch
):
    from kimi_cli.session import Session as CliSession
    from kimix.server.session_manager import ManagedSession, SessionInfo

    disk = _disk_session(tmp_path)
    records = [
        {"role": "user", "content": "obsolete python", "created_at": 1.0},
        {"role": "_reset"},
        {"role": "user", "content": "first python 50%", "created_at": 10.0},
        {"role": "assistant", "content": "日本語 python", "created_at": 20.0},
        {"role": "user", "content": "last python", "created_at": 30.0},
    ]
    disk.context_file.write_bytes(
        b"".join(orjson.dumps(record, option=orjson.OPT_APPEND_NEWLINE) for record in records)
    )
    monkeypatch.setattr(CliSession, "find", AsyncMock(return_value=disk))
    manager = SessionManager()
    manager._sessions[disk.id] = ManagedSession(
        info=SessionInfo(id=disk.id), work_dir=str(disk.work_dir)
    )

    newest = await manager.search_messages(disk.id, "python", sort="newest")
    oldest = await manager.search_messages(disk.id, "python", sort="oldest")
    assert [row["rowid"] for row in newest] == [3, 2, 1]
    assert [row["rowid"] for row in oldest] == [1, 2, 3]
    assert all(row["content_text"] != "obsolete python" for row in newest)
    assert set(newest[0]) == {
        "rowid",
        "role",
        "content",
        "content_text",
        "created_at",
        "snippet",
        "score",
    }
    assert orjson.loads(newest[0]["content"])["content"] == "last python"
    assert newest[0]["created_at"] == 30.0
    assert ">>>python<<<" in newest[0]["snippet"]

    users = await manager.search_messages(disk.id, "python", role="user", sort="oldest")
    assert [row["rowid"] for row in users] == [1, 3]
    page = await manager.search_messages(
        disk.id, "python", sort="oldest", offset=1, limit=1
    )
    assert [row["rowid"] for row in page] == [2]
    assert (await manager.search_messages(disk.id, "日"))[0]["rowid"] == 2
    assert (await manager.search_messages(disk.id, "50%"))[0]["rowid"] == 1
    assert await manager.search_messages(disk.id, "python", limit=0) == []
    assert await manager.search_messages("missing", "python") == []
