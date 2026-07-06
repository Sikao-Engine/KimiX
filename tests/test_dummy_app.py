"""Comprehensive tests for kimix.server.dummy_app.

Uses httpx.AsyncClient with ASGITransport to exercise every route
and verifies that DummySessionManager methods are actually invoked.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from kimix.server.dummy_app import create_app, session_manager
from kimix.server.dummy_session_manager import DummySessionManager, SessionInfo

_RESULT_FILE = Path(__file__).parent / "test_result.txt"


def _log_result(test_name: str, status_code: int, body: str) -> None:
    """Append test result to test_result.txt."""
    entry = {
        "test": test_name,
        "status_code": status_code,
        "body": body,
    }
    with open(_RESULT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


@pytest.fixture(autouse=True)
def mock_sdk_session():
    """Prevent real SDK session creation in all tests."""
    with patch(
        "kimix.server.dummy_session_manager._create_session_async",
        new_callable=AsyncMock,
    ) as mock:
        mock_session = MagicMock()
        mock_session.cancel = MagicMock()
        mock_session.clear = AsyncMock()
        mock_session.compact = AsyncMock()
        mock_session.export = AsyncMock(return_value=("/tmp/out.json", 0))
        mock.return_value = mock_session
        yield mock


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Health ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check(client: httpx.AsyncClient) -> None:
    resp = await client.get("/global/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["healthy"] is True
    assert data["version"] == "0.1.0"
    _log_result("test_health_check", resp.status_code, resp.text)


# ── SSE Event Stream ──────────────────────────────────────────────


def test_event_stream(app) -> None:
    """Patch asyncio.wait_for so the event loop exits quickly after the connected event."""
    import asyncio

    async def _cancelling_wait_for(aw, timeout=None):
        raise asyncio.CancelledError()

    with patch("kimix.server.dummy_app.asyncio.wait_for", _cancelling_wait_for):
        client = TestClient(app)
        resp = client.get("/event")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("text/event-stream")
    assert "server.connected" in resp.text
    _log_result("test_event_stream", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_create_session_defaults(client: httpx.AsyncClient) -> None:
    """Verify that supervisor and ralph_loop default to False/0."""
    with patch.object(
        session_manager,
        "create_session",
        wraps=session_manager.create_session,
    ) as mock_create:
        resp = await client.post("/session", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"].startswith("ses_")
    mock_create.assert_awaited_once_with(
        title=None, supervisor=False, ralph_loop=0
    )
    _log_result("test_create_session_defaults", resp.status_code, resp.text)


# ── Session CRUD ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "create_session",
        wraps=session_manager.create_session,
    ) as mock_create:
        resp = await client.post(
            "/session",
            json={"title": "My Session", "supervisor": True, "ralph_loop": 4},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"].startswith("ses_")
    assert data["title"] == "My Session"
    mock_create.assert_awaited_once_with(
        title="My Session", supervisor=True, ralph_loop=4
    )
    _log_result("test_create_session", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_list_sessions(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "list_sessions",
        wraps=session_manager.list_sessions,
    ) as mock_list:
        resp = await client.get("/session")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    mock_list.assert_called_once()
    _log_result("test_list_sessions", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_get_session(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "get_session",
        return_value=SessionInfo(
            id="ses_abc123", title="Test", createdAt=1.0, updatedAt=1.0
        ),
    ) as mock_get:
        resp = await client.get("/session/ses_abc123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "ses_abc123"
    mock_get.assert_called_once_with("ses_abc123")
    _log_result("test_get_session", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_get_session_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "get_session",
        side_effect=KeyError("missing"),
    ) as mock_get:
        resp = await client.get("/session/ses_missing")
    assert resp.status_code == 404
    mock_get.assert_called_once_with("ses_missing")
    _log_result("test_get_session_not_found", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_delete_session(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "delete_session",
        return_value=True,
    ) as mock_delete:
        resp = await client.delete("/session/ses_abc123")
    assert resp.status_code == 200
    mock_delete.assert_awaited_once_with("ses_abc123")
    _log_result("test_delete_session", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_delete_session_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "delete_session",
        return_value=False,
    ) as mock_delete:
        resp = await client.delete("/session/ses_missing")
    assert resp.status_code == 404
    mock_delete.assert_awaited_once_with("ses_missing")
    _log_result("test_delete_session_not_found", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_session_status(client: httpx.AsyncClient) -> None:
    create_resp = await client.post("/session", json={"title": "Status Test"})
    assert create_resp.status_code == 200
    session_id = create_resp.json()["id"]
    with patch.object(
        session_manager,
        "get_session_status",
        wraps=session_manager.get_session_status,
    ) as mock_status:
        resp = await client.get(f"/session/{session_id}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)
    mock_status.assert_called_once()
    _log_result("test_session_status", resp.status_code, resp.text)


# ── Messages ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_messages(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "get_messages",
        return_value=[{"role": "user", "text": "hi"}],
    ) as mock_msgs:
        resp = await client.get("/session/ses_abc123/message?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    mock_msgs.assert_called_once_with("ses_abc123", limit=5)
    _log_result("test_get_messages", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_get_messages_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "get_messages",
        side_effect=KeyError("missing"),
    ) as mock_msgs:
        resp = await client.get("/session/ses_missing/message")
    assert resp.status_code == 404
    mock_msgs.assert_called_once_with("ses_missing", limit=None)
    _log_result("test_get_messages_not_found", resp.status_code, resp.text)


# ── Prompt Async ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_prompt_async(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "prompt_async",
        new_callable=AsyncMock,
    ) as mock_prompt:
        resp = await client.post(
            "/session/ses_abc123/prompt_async",
            json={
                "parts": [{"type": "text", "text": "hello"}],
            },
        )
    assert resp.status_code == 204
    mock_prompt.assert_awaited_once_with("ses_abc123", "hello")
    _log_result("test_send_prompt_async", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_send_prompt_async_no_text(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/session/ses_abc123/prompt_async",
        json={"parts": [{"type": "text", "text": ""}]},
    )
    assert resp.status_code == 400
    _log_result("test_send_prompt_async_no_text", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_send_prompt_async_session_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "prompt_async",
        side_effect=KeyError("missing"),
    ) as mock_prompt:
        resp = await client.post(
            "/session/ses_missing/prompt_async",
            json={"parts": [{"type": "text", "text": "hi"}]},
        )
    assert resp.status_code == 404
    mock_prompt.assert_awaited_once_with("ses_missing", "hi")
    _log_result("test_send_prompt_async_session_not_found", resp.status_code, resp.text)


# ── Abort ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_abort_session(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "abort_session",
        return_value=True,
    ) as mock_abort:
        resp = await client.post("/session/ses_abc123/abort")
    assert resp.status_code == 200
    mock_abort.assert_called_once_with("ses_abc123")
    _log_result("test_abort_session", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_abort_session_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "abort_session",
        side_effect=KeyError("missing"),
    ) as mock_abort:
        resp = await client.post("/session/ses_missing/abort")
    assert resp.status_code == 404
    mock_abort.assert_called_once_with("ses_missing")
    _log_result("test_abort_session_not_found", resp.status_code, resp.text)


# ── Permissions ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_grant_permission(client: httpx.AsyncClient) -> None:
    resp = await client.post("/session/ses_abc123/permissions/perm_1")
    assert resp.status_code == 200
    _log_result("test_grant_permission", resp.status_code, resp.text)


# ── Options ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_session(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "clear_session",
        return_value=True,
    ) as mock_clear:
        resp = await client.get("/session/ses_abc123/clear")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cleared"] == 1
    assert data["sessionID"] == "ses_abc123"
    mock_clear.assert_awaited_once_with("ses_abc123")
    _log_result("test_clear_session", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_clear_session_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "clear_session",
        side_effect=KeyError("missing"),
    ) as mock_clear:
        resp = await client.get("/session/ses_missing/clear")
    assert resp.status_code == 404
    mock_clear.assert_awaited_once_with("ses_missing")
    _log_result("test_clear_session_not_found", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_get_session_context(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "get_session_context",
        return_value={"sessionID": "ses_abc123", "context_usage": None},
    ) as mock_ctx:
        resp = await client.get("/session/ses_abc123/context")
    assert resp.status_code == 200
    data = resp.json()
    assert data["sessionID"] == "ses_abc123"
    mock_ctx.assert_awaited_once_with("ses_abc123")
    _log_result("test_get_session_context", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_get_session_context_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "get_session_context",
        side_effect=KeyError("missing"),
    ) as mock_ctx:
        resp = await client.get("/session/ses_missing/context")
    assert resp.status_code == 404
    mock_ctx.assert_awaited_once_with("ses_missing")
    _log_result("test_get_session_context_not_found", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_compact_session(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "compact_session",
        return_value=True,
    ) as mock_compact:
        resp = await client.get("/session/ses_abc123/compact?keep=5")
    assert resp.status_code == 200
    data = resp.json()
    assert data["compacted"] == 1
    assert data["keep"] == 5
    mock_compact.assert_awaited_once_with("ses_abc123", keep=5)
    _log_result("test_compact_session", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_compact_session_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "compact_session",
        side_effect=KeyError("missing"),
    ) as mock_compact:
        resp = await client.get("/session/ses_missing/compact")
    assert resp.status_code == 404
    mock_compact.assert_awaited_once_with("ses_missing", keep=10)
    _log_result("test_compact_session_not_found", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_export_session(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "export_session",
        return_value=("/tmp/out.json", 0),
    ) as mock_export:
        resp = await client.get("/session/ses_abc123/export?output_path=/tmp/out.json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["output"] == "/tmp/out.json"
    assert data["sessionID"] == "ses_abc123"
    mock_export.assert_awaited_once_with("ses_abc123", output_path="/tmp/out.json")
    _log_result("test_export_session", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_export_session_not_found(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "export_session",
        side_effect=KeyError("missing"),
    ) as mock_export:
        resp = await client.get("/session/ses_missing/export")
    assert resp.status_code == 404
    mock_export.assert_awaited_once_with("ses_missing", output_path=None)
    _log_result("test_export_session_not_found", resp.status_code, resp.text)


@pytest.mark.asyncio
async def test_export_session_bad_value(client: httpx.AsyncClient) -> None:
    with patch.object(
        session_manager,
        "export_session",
        side_effect=ValueError("bad path"),
    ) as mock_export:
        resp = await client.get("/session/ses_abc123/export?output_path=bad")
    assert resp.status_code == 400
    mock_export.assert_awaited_once_with("ses_abc123", output_path="bad")
    _log_result("test_export_session_bad_value", resp.status_code, resp.text)


# ── Multi-function lifecycle test ─────────────────────────────────


@pytest.mark.asyncio
async def test_full_session_lifecycle(client: httpx.AsyncClient) -> None:
    """Create, query, message, export, and delete a session in one flow."""
    # 1. Create session
    create_resp = await client.post("/session", json={"title": "Lifecycle Test"})
    assert create_resp.status_code == 200
    session = create_resp.json()
    session_id = session["id"]
    assert session_id.startswith("ses_")
    assert session["title"] == "Lifecycle Test"
    _log_result("test_full_session_lifecycle_create", create_resp.status_code, create_resp.text)

    # 2. List sessions
    list_resp = await client.get("/session")
    assert list_resp.status_code == 200
    sessions = list_resp.json()
    assert isinstance(sessions, list)
    _log_result("test_full_session_lifecycle_list", list_resp.status_code, list_resp.text)

    # 3. Get session status
    status_resp = await client.get(f"/session/{session_id}/status")
    assert status_resp.status_code == 200
    assert isinstance(status_resp.json(), dict)
    _log_result("test_full_session_lifecycle_status", status_resp.status_code, status_resp.text)

    # 4. Get specific session
    get_resp = await client.get(f"/session/{session_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == session_id
    _log_result("test_full_session_lifecycle_get", get_resp.status_code, get_resp.text)

    # 5. Send prompt async
    prompt_resp = await client.post(
        f"/session/{session_id}/prompt_async",
        json={"parts": [{"type": "text", "text": "hello world"}]},
    )
    assert prompt_resp.status_code == 204
    _log_result("test_full_session_lifecycle_prompt", prompt_resp.status_code, prompt_resp.text)

    # 6. Get messages
    msgs_resp = await client.get(f"/session/{session_id}/message")
    assert msgs_resp.status_code == 200
    assert isinstance(msgs_resp.json(), list)
    _log_result("test_full_session_lifecycle_messages", msgs_resp.status_code, msgs_resp.text)

    # 7. Abort
    abort_resp = await client.post(f"/session/{session_id}/abort")
    assert abort_resp.status_code == 200
    _log_result("test_full_session_lifecycle_abort", abort_resp.status_code, abort_resp.text)

    # 8. Grant permission
    perm_resp = await client.post(f"/session/{session_id}/permissions/perm_123")
    assert perm_resp.status_code == 200
    _log_result("test_full_session_lifecycle_permission", perm_resp.status_code, perm_resp.text)

    # 9. Get context
    ctx_resp = await client.get(f"/session/{session_id}/context")
    assert ctx_resp.status_code == 200
    assert ctx_resp.json()["sessionID"] == session_id
    _log_result("test_full_session_lifecycle_context", ctx_resp.status_code, ctx_resp.text)

    # 10. Compact
    compact_resp = await client.get(f"/session/{session_id}/compact?keep=3")
    assert compact_resp.status_code == 200
    assert compact_resp.json()["keep"] == 3
    _log_result("test_full_session_lifecycle_compact", compact_resp.status_code, compact_resp.text)

    # 11. Export
    export_resp = await client.get(f"/session/{session_id}/export")
    assert export_resp.status_code == 200
    assert export_resp.json()["sessionID"] == session_id
    _log_result("test_full_session_lifecycle_export", export_resp.status_code, export_resp.text)

    # 12. Clear
    clear_resp = await client.get(f"/session/{session_id}/clear")
    assert clear_resp.status_code == 200
    assert clear_resp.json()["cleared"] == 1
    _log_result("test_full_session_lifecycle_clear", clear_resp.status_code, clear_resp.text)

    # 13. Delete
    delete_resp = await client.delete(f"/session/{session_id}")
    assert delete_resp.status_code == 200
    _log_result("test_full_session_lifecycle_delete", delete_resp.status_code, delete_resp.text)

    # 14. Health still OK after all operations
    health_resp = await client.get("/global/health")
    assert health_resp.status_code == 200
    assert health_resp.json()["healthy"] is True
    _log_result("test_full_session_lifecycle_health", health_resp.status_code, health_resp.text)
