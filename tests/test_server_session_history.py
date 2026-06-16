from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
