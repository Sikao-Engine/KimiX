"""Tests for kimix.cli_server HTTP client.

All HTTP calls are mocked so no real LLM backend is contacted.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from kimix.cli_server.client import (
    KimixAsyncClient,
    KimixHttpClient,
    HealthResponse,
    SessionResponse,
    SessionStatusResponse,
    MessageResponse,
    MessagePart,
    MessageInfo,
    PromptInput,
    PromptPart,
    SSEEvent,
    EventType,
    ParsedEvent,
    parse_event,
    check_health_sync,
    abort_session_sync,
    _json_dumps,
    _json_loads,
)


# ── JSON helpers ──────────────────────────────────────────────────


def test_json_dumps_loads() -> None:
    obj = {"key": "value", "number": 42, "flag": True}
    dumped = _json_dumps(obj)
    loaded = _json_loads(dumped)
    assert loaded == obj


# ── Model factories ───────────────────────────────────────────────


def _make_response(status_code: int, json_data: Any) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = json.dumps(json_data).encode("utf-8")
    resp.json.return_value = json_data
    return resp


# ── Async Client Tests ────────────────────────────────────────────


@pytest.fixture
def async_client() -> KimixAsyncClient:
    return KimixAsyncClient(host="127.0.0.1", port=4096)


@pytest.mark.asyncio
async def test_health_check(async_client: KimixAsyncClient) -> None:
    mock_resp = _make_response(200, {"healthy": True, "version": "0.1.0"})
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.health_check()
    assert isinstance(result, HealthResponse)
    assert result.healthy is True
    assert result.version == "0.1.0"


@pytest.mark.asyncio
async def test_create_session(async_client: KimixAsyncClient) -> None:
    payload = {
        "id": "ses_test123",
        "title": "Test Session",
        "createdAt": 1700000000.0,
        "updatedAt": 1700000000.0,
        "parentID": None,
    }
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "post", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.create_session(title="Test Session")
    assert isinstance(result, SessionResponse)
    assert result.id == "ses_test123"
    assert result.title == "Test Session"


@pytest.mark.asyncio
async def test_get_session(async_client: KimixAsyncClient) -> None:
    payload = {
        "id": "ses_test123",
        "title": "Test Session",
        "createdAt": 1700000000.0,
        "updatedAt": 1700000001.0,
        "parentID": None,
    }
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.get_session("ses_test123")
    assert result.id == "ses_test123"
    assert result.updated_at == 1700000001.0


@pytest.mark.asyncio
async def test_delete_session(async_client: KimixAsyncClient) -> None:
    mock_resp = _make_response(200, {})
    with patch.object(
        async_client._client, "delete", new_callable=AsyncMock, return_value=mock_resp
    ):
        ok = await async_client.delete_session("ses_test123")
    assert ok is True


@pytest.mark.asyncio
async def test_list_sessions(async_client: KimixAsyncClient) -> None:
    payload = [
        {
            "id": "ses_1",
            "title": "Session 1",
            "createdAt": 1700000000.0,
            "updatedAt": 1700000001.0,
            "parentID": None,
        }
    ]
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        sessions = await async_client.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == "ses_1"


@pytest.mark.asyncio
async def test_get_all_session_status(async_client: KimixAsyncClient) -> None:
    payload = {"ses_1": {"type": "idle", "time": 1700000000.0}}
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        statuses = await async_client.get_all_session_status()
    assert "ses_1" in statuses
    assert statuses["ses_1"].type == "idle"


@pytest.mark.asyncio
async def test_get_messages(async_client: KimixAsyncClient) -> None:
    payload = [
        {
            "info": {
                "id": "msg_1",
                "role": "user",
                "sessionID": "ses_1",
                "time": {"created": 1700000000000},
            },
            "parts": [
                {
                    "id": "prt_1",
                    "type": "text",
                    "text": "Hello",
                    "sessionID": "ses_1",
                    "messageID": "msg_1",
                }
            ],
        }
    ]
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        messages = await async_client.get_messages("ses_1", limit=10)
    assert len(messages) == 1
    assert messages[0].info.role == "user"
    assert messages[0].parts[0].text == "Hello"


@pytest.mark.asyncio
async def test_send_prompt_async(async_client: KimixAsyncClient) -> None:
    mock_resp = _make_response(204, {})
    with patch.object(
        async_client._client, "post", new_callable=AsyncMock, return_value=mock_resp
    ):
        ok = await async_client.send_prompt_async(
            "ses_1", "write tests", agent="worker"
        )
    assert ok is True


@pytest.mark.asyncio
async def test_send_prompt(async_client: KimixAsyncClient) -> None:
    mock_resp = _make_response(204, {})
    with patch.object(
        async_client._client, "post", new_callable=AsyncMock, return_value=mock_resp
    ):
        ok = await async_client.send_prompt("ses_1", "hello")
    assert ok is True


@pytest.mark.asyncio
async def test_get_output(async_client: KimixAsyncClient) -> None:
    payload = {"text": "accumulated text", "sessionID": "ses_1"}
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.get_output("ses_1")
    assert result["text"] == "accumulated text"


@pytest.mark.asyncio
async def test_get_state(async_client: KimixAsyncClient) -> None:
    payload = {"state": "idle", "sessionID": "ses_1"}
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.get_state("ses_1")
    assert result["state"] == "idle"


@pytest.mark.asyncio
async def test_abort_session(async_client: KimixAsyncClient) -> None:
    mock_resp = _make_response(200, {})
    with patch.object(
        async_client._client, "post", new_callable=AsyncMock, return_value=mock_resp
    ):
        ok = await async_client.abort_session("ses_1")
    assert ok is True


@pytest.mark.asyncio
async def test_grant_permission(async_client: KimixAsyncClient) -> None:
    mock_resp = _make_response(200, {})
    with patch.object(
        async_client._client, "post", new_callable=AsyncMock, return_value=mock_resp
    ):
        ok = await async_client.grant_permission("ses_1", "perm_1")
    assert ok is True


@pytest.mark.asyncio
async def test_clear_session(async_client: KimixAsyncClient) -> None:
    payload = {"cleared": 1, "sessionID": "ses_1"}
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.clear_session("ses_1")
    assert result["cleared"] == 1


@pytest.mark.asyncio
async def test_get_session_context(async_client: KimixAsyncClient) -> None:
    payload = {"sessionID": "ses_1", "context_usage": {"tokens": 100}}
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.get_session_context("ses_1")
    assert result["sessionID"] == "ses_1"


@pytest.mark.asyncio
async def test_compact_session(async_client: KimixAsyncClient) -> None:
    payload = {"compacted": 1, "sessionID": "ses_1", "keep": 10}
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.compact_session("ses_1", keep=10)
    assert result["compacted"] == 1


@pytest.mark.asyncio
async def test_export_session(async_client: KimixAsyncClient) -> None:
    payload = {"output": "/tmp/export.json", "count": 42, "sessionID": "ses_1"}
    mock_resp = _make_response(200, payload)
    with patch.object(
        async_client._client, "get", new_callable=AsyncMock, return_value=mock_resp
    ):
        result = await async_client.export_session("ses_1")
    assert result["count"] == 42


# ── Sync Client Tests ─────────────────────────────────────────────


@pytest.fixture
def sync_client() -> KimixHttpClient:
    return KimixHttpClient(host="127.0.0.1", port=4096)


def _mock_httpx_client(mock_resp):
    client = MagicMock()
    client.get = AsyncMock(return_value=mock_resp)
    client.post = AsyncMock(return_value=mock_resp)
    client.delete = AsyncMock(return_value=mock_resp)
    return client


def test_sync_health_check(sync_client: KimixHttpClient) -> None:
    payload = {"healthy": True, "version": "0.1.0"}
    mock_resp = _make_response(200, payload)
    with patch(
        "kimix.cli_server.client.httpx.AsyncClient",
        return_value=_mock_httpx_client(mock_resp),
    ):
        result = sync_client.health_check()
    assert result.healthy is True


def test_sync_create_session(sync_client: KimixHttpClient) -> None:
    payload = {
        "id": "ses_sync",
        "title": "Sync Session",
        "createdAt": 1700000000.0,
        "updatedAt": 1700000000.0,
        "parentID": None,
    }
    mock_resp = _make_response(200, payload)
    with patch(
        "kimix.cli_server.client.httpx.AsyncClient",
        return_value=_mock_httpx_client(mock_resp),
    ):
        result = sync_client.create_session(title="Sync Session")
    assert result.id == "ses_sync"


def test_sync_delete_session(sync_client: KimixHttpClient) -> None:
    mock_resp = _make_response(200, {})
    with patch(
        "kimix.cli_server.client.httpx.AsyncClient",
        return_value=_mock_httpx_client(mock_resp),
    ):
        ok = sync_client.delete_session("ses_sync")
    assert ok is True


# ── SSE Parser Tests ──────────────────────────────────────────────


def test_parse_event_text_delta() -> None:
    data = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "prt_1",
                "sessionID": "ses_1",
                "messageID": "msg_1",
                "type": "text",
                "text": "hello world",
            },
            "delta": "world",
        },
    }
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event, session_id="ses_1")
    assert parsed.type == EventType.TEXT
    assert parsed.text == "hello world"
    assert parsed.delta == "world"


def test_parse_event_reasoning() -> None:
    data = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "prt_1",
                "sessionID": "ses_1",
                "messageID": "msg_1",
                "type": "reasoning",
                "text": "thinking...",
            },
            "delta": "...",
        },
    }
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event, session_id="ses_1")
    assert parsed.type == EventType.REASONING
    assert parsed.text == "thinking..."


def test_parse_event_tool() -> None:
    data = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "prt_1",
                "sessionID": "ses_1",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "read_file",
                "callID": "toolu_1",
                "state": {"status": "running", "input": {"path": "foo.py"}},
            },
        },
    }
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event, session_id="ses_1")
    assert parsed.type == EventType.TOOL
    assert parsed.tool_name == "read_file"
    assert parsed.tool_status == "running"


def test_parse_event_step_finish_terminal() -> None:
    data = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "prt_1",
                "sessionID": "ses_1",
                "messageID": "msg_1",
                "type": "step-finish",
                "reason": "stop",
                "cost": 0.001,
                "tokens": {"input": 10, "output": 5},
            },
        },
    }
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event, session_id="ses_1")
    assert parsed.type == EventType.STEP_FINISH
    assert parsed.finished is True
    assert parsed.is_terminal() is True


def test_parse_event_step_finish_non_terminal() -> None:
    data = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "prt_1",
                "sessionID": "ses_1",
                "messageID": "msg_1",
                "type": "step-finish",
                "reason": "tool-calls",
            },
        },
    }
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event, session_id="ses_1")
    assert parsed.type == EventType.STEP_FINISH
    assert parsed.finished is False
    assert parsed.is_terminal() is False


def test_parse_event_session_idle() -> None:
    data = {
        "type": "session.idle",
        "properties": {"sessionID": "ses_1", "status": {"type": "idle"}},
    }
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event, session_id="ses_1")
    assert parsed.type == EventType.SESSION_IDLE
    assert parsed.is_terminal() is True


def test_parse_event_skip_heartbeat() -> None:
    data = {"type": "server.heartbeat", "properties": {}}
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event)
    assert parsed.type == EventType.SKIP


def test_parse_event_reconnect() -> None:
    event = SSEEvent(event="__reconnected__", data="3")
    parsed = parse_event(event)
    assert parsed.type == EventType.RECONNECTED
    assert "3" in parsed.text


def test_parse_event_filters_other_session() -> None:
    data = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "prt_1",
                "sessionID": "ses_other",
                "messageID": "msg_1",
                "type": "text",
                "text": "hello",
            },
        },
    }
    event = SSEEvent(data=json.dumps(data))
    parsed = parse_event(event, session_id="ses_1")
    assert parsed.type == EventType.SKIP


# ── Sync Helper Tests ─────────────────────────────────────────────


def test_check_health_sync() -> None:
    payload = {"healthy": True, "version": "0.1.0"}
    mock_resp = _make_response(200, payload)
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client
        assert check_health_sync(4096) is True


def test_check_health_sync_failure() -> None:
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("conn refused")
        mock_client_cls.return_value = mock_client
        assert check_health_sync(4096) is False


def test_abort_session_sync() -> None:
    mock_resp = _make_response(200, {})
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client
        assert abort_session_sync("ses_1", 4096) is True


# ── Model Tests ───────────────────────────────────────────────────


def test_session_response_to_dict() -> None:
    sr = SessionResponse(
        id="ses_1", title="T", created_at=1.0, updated_at=2.0, parent_id="p1"
    )
    d = sr.to_dict()
    assert d["id"] == "ses_1"
    assert d["title"] == "T"
    assert d["parentID"] == "p1"


def test_prompt_input_to_dict() -> None:
    pi = PromptInput(
        parts=[PromptPart(type="text", text="hello")],
        agent="worker",
    )
    d = pi.to_dict()
    assert d["parts"][0]["text"] == "hello"
    assert d["agent"] == "worker"
    assert "model" not in d


def test_message_part_from_dict() -> None:
    d = {
        "id": "prt_1",
        "type": "text",
        "text": "hi",
        "sessionID": "ses_1",
        "messageID": "msg_1",
    }
    mp = MessagePart.from_dict(d)
    assert mp.id == "prt_1"
    assert mp.text == "hi"
    assert mp.session_id == "ses_1"


def test_message_response_from_dict() -> None:
    d = {
        "info": {
            "id": "msg_1",
            "role": "user",
            "sessionID": "ses_1",
            "time": {"created": 1000},
        },
        "parts": [
            {"id": "prt_1", "type": "text", "text": "hi", "sessionID": "ses_1", "messageID": "msg_1"}
        ],
    }
    mr = MessageResponse.from_dict(d)
    assert mr.info.role == "user"
    assert len(mr.parts) == 1


# ── Stream parser tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_sse_stream() -> None:
    mock_response = MagicMock()
    mock_response.aiter_lines = AsyncMock(
        return_value=async_iter([
            "data: {\"type\": \"server.connected\"}",
            "",
            ": heartbeat",
            "data: {\"type\": \"session.created\"}",
            "",
        ])
    )
    # Need to make aiter_lines an async iterator
    async def async_gen():
        for line in [
            "data: {\"type\": \"server.connected\"}",
            "",
            ": heartbeat",
            "data: {\"type\": \"session.created\"}",
            "",
        ]:
            yield line

    mock_response.aiter_lines = async_gen

    from kimix.cli_server.client import _parse_sse_stream

    events: List[SSEEvent] = []
    async for event in _parse_sse_stream(mock_response):
        events.append(event)

    assert len(events) == 2
    assert events[0].data == '{"type": "server.connected"}'
    assert events[1].data == '{"type": "session.created"}'


# Helper for async iteration in tests
async def async_iter(items):
    for item in items:
        yield item
