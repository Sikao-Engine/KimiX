# -*- coding: utf-8 -*-
"""Kimix opencode-style HTTP client (httpx + orjson).

Implements all REST API endpoints documented in http_doc.md:
- Health, SSE events, session CRUD, messaging, abort, permissions,
  clear, context, compact, export.

Uses orjson instead of the standard json module for serialization.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import httpx
import orjson

logger = logging.getLogger(__name__)

# ── JSON helpers (orjson) ─────────────────────────────────────────


def _json_dumps(obj: Any) -> str:
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS).decode("utf-8")


def _json_loads(data: str | bytes) -> Any:
    return orjson.loads(data)


# ── Request / Response Models ─────────────────────────────────────


@dataclass
class HealthResponse:
    healthy: bool = False
    version: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HealthResponse":
        return cls(
            healthy=bool(data.get("healthy", False)),
            version=str(data.get("version", "")),
        )


@dataclass
class SessionResponse:
    id: str = ""
    title: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    parent_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionResponse":
        return cls(
            id=str(data.get("id", "")),
            title=data.get("title"),
            created_at=float(data.get("createdAt", 0.0)),
            updated_at=float(data.get("updatedAt", 0.0)),
            parent_id=data.get("parentID"),
        )

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "id": self.id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }
        if self.title is not None:
            d["title"] = self.title
        if self.parent_id is not None:
            d["parentID"] = self.parent_id
        return d


@dataclass
class SessionStatusResponse:
    type: str = "idle"
    time: float = 0.0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionStatusResponse":
        return cls(
            type=str(data.get("type", "idle")),
            time=float(data.get("time", 0.0)),
        )


@dataclass
class PromptPart:
    type: str = "text"
    text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "text": self.text}


@dataclass
class PromptInput:
    parts: List[PromptPart] = field(default_factory=list)
    agent: Optional[str] = None
    model: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "parts": [p.to_dict() for p in self.parts],
        }
        if self.agent is not None:
            d["agent"] = self.agent
        if self.model is not None:
            d["model"] = self.model
        return d


@dataclass
class MessagePart:
    id: str = ""
    type: str = "text"
    text: str = ""
    session_id: str = ""
    message_id: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessagePart":
        return cls(
            id=str(data.get("id", "")),
            type=str(data.get("type", "text")),
            text=str(data.get("text", "")),
            session_id=str(data.get("sessionID", "")),
            message_id=str(data.get("messageID", "")),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "text": self.text,
            "sessionID": self.session_id,
            "messageID": self.message_id,
        }


@dataclass
class MessageInfo:
    id: str = ""
    role: str = "assistant"
    session_id: str = ""
    time: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageInfo":
        return cls(
            id=str(data.get("id", "")),
            role=str(data.get("role", "assistant")),
            session_id=str(data.get("sessionID", "")),
            time=data.get("time", {}),
        )


@dataclass
class MessageResponse:
    info: MessageInfo = field(default_factory=MessageInfo)
    parts: List[MessagePart] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageResponse":
        info_data = data.get("info", {})
        parts_data = data.get("parts", [])
        return cls(
            info=MessageInfo.from_dict(info_data),
            parts=[MessagePart.from_dict(p) for p in parts_data],
        )


# ── SSE Event Models ──────────────────────────────────────────────


@dataclass
class SSEEvent:
    """Parsed Server-Sent Event."""

    event: str = ""
    data: str = ""
    id: Optional[str] = None

    def json(self) -> Any:
        if not self.data:
            return None
        try:
            return _json_loads(self.data)
        except Exception:
            return None

    @property
    def is_reconnect(self) -> bool:
        return self.event == "__reconnected__"


class EventType(str, Enum):
    TEXT = "text"
    TEXT_DELTA = "text_delta"
    REASONING = "reasoning"
    TOOL = "tool"
    PERMISSION = "permission"
    STEP_START = "step-start"
    STEP_FINISH = "step-finish"
    SESSION_IDLE = "session_idle"
    RECONNECTED = "reconnected"
    SKIP = "skip"
    UNKNOWN = "unknown"


@dataclass
class ParsedEvent:
    type: EventType = EventType.UNKNOWN
    text: str = ""
    delta: str = ""
    tool_name: str = ""
    tool_status: str = ""
    tool_title: str = ""
    tool_input: Any = ""
    tool_output: str = ""
    tool_error: str = ""
    tool_call_id: str = ""
    created_at: float = 0.0
    permission_id: str = ""
    finished: bool = False
    cost: float = 0.0
    tokens: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def is_terminal(self) -> bool:
        """Check if this event signals the end of a prompt response."""
        if self.type == EventType.SESSION_IDLE:
            return True
        if self.type == EventType.STEP_FINISH:
            return self.text not in ("tool-calls", "tool_calls")
        return False


# ── SSE Event Parser ──────────────────────────────────────────────


def parse_event(event: SSEEvent, session_id: str = "") -> ParsedEvent:
    """Parse a raw SSE event into a structured ParsedEvent."""
    if event.event == "__reconnected__":
        return ParsedEvent(
            type=EventType.RECONNECTED,
            text=f"SSE reconnected (attempt {event.data})",
        )

    data = event.json()
    if not data:
        return ParsedEvent(type=EventType.SKIP)

    event_type: str = data.get("type", "")

    if event_type in ("server.connected", "server.heartbeat"):
        return ParsedEvent(type=EventType.SKIP)

    if session_id and not _matches_session(data, session_id):
        return ParsedEvent(type=EventType.SKIP)

    if event_type == "message.part.updated":
        return _parse_part_updated(data)

    if event_type in (
        "message.updated",
        "message.created",
        "session.updated",
        "session.created",
        "session.deleted",
        "session.diff",
    ):
        return ParsedEvent(type=EventType.SKIP)

    if event_type in ("session.idle", "session.status"):
        return _parse_session_status(data, event_type)

    return ParsedEvent(type=EventType.UNKNOWN, raw=data)


def _parse_part_updated(data: Dict[str, Any]) -> ParsedEvent:
    props = data.get("properties", {})
    part = props.get("part", {})
    delta = props.get("delta", "")
    part_type = part.get("type", "")

    if part_type == "text":
        return ParsedEvent(
            type=EventType.TEXT,
            delta=delta,
            text=part.get("text", ""),
            raw=data,
        )
    if part_type == "tool":
        state = part.get("state", {})
        tool_name = part.get("tool", "")
        call_id = part.get("callID", "")
        status = state.get("status", "")
        tool_input = state.get("input", {})
        return ParsedEvent(
            type=EventType.TOOL,
            tool_name=tool_name,
            tool_status=status,
            tool_title=tool_name,
            tool_input=tool_input,
            tool_output=state.get("output", ""),
            tool_error=state.get("error", ""),
            tool_call_id=call_id,
            raw=data,
        )
    if part_type == "reasoning":
        return ParsedEvent(
            type=EventType.REASONING,
            text=part.get("text", ""),
            delta=delta,
            raw=data,
        )
    if part_type == "step-start":
        return ParsedEvent(type=EventType.STEP_START, raw=data)
    if part_type == "step-finish":
        reason = part.get("reason", "")
        finished = reason not in ("tool-calls", "tool_calls")
        return ParsedEvent(
            type=EventType.STEP_FINISH,
            text=reason,
            finished=finished,
            cost=part.get("cost", 0.0),
            tokens=part.get("tokens", {}),
            raw=data,
        )
    return ParsedEvent(type=EventType.SKIP)


def _parse_session_status(data: Dict[str, Any], event_type: str) -> ParsedEvent:
    if event_type == "session.status":
        props = data.get("properties", {})
        status = props.get("status", {})
        status_type = status.get("type", "") if isinstance(status, dict) else ""
        if status_type != "idle":
            return ParsedEvent(type=EventType.SKIP)
    return ParsedEvent(type=EventType.SESSION_IDLE, finished=True, raw=data)


def _matches_session(data: Dict[str, Any], session_id: str) -> bool:
    props = data.get("properties", {})
    sid: Optional[str] = props.get("sessionID")
    if not sid:
        part = props.get("part", {})
        if isinstance(part, dict):
            sid = part.get("sessionID")
    if not sid:
        info = props.get("info", {})
        if isinstance(info, dict):
            sid = info.get("sessionID")
    return not sid or sid == session_id


# ── Sync Helpers ──────────────────────────────────────────────────


def check_health_sync(
    port: int,
    host: str = "127.0.0.1",
    timeout: float = 3.0,
) -> bool:
    """Synchronous health check."""
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout), trust_env=False
        ) as client:
            resp = client.get(f"http://{host}:{port}/global/health")
            data = _json_loads(resp.content)
            return bool(data.get("healthy", False))
    except Exception:
        return False


def abort_session_sync(
    session_id: str,
    port: int,
    host: str = "127.0.0.1",
    timeout: float = 10.0,
) -> bool:
    """Synchronous session abort."""
    try:
        with httpx.Client(
            timeout=httpx.Timeout(timeout), trust_env=False
        ) as client:
            resp = client.post(
                f"http://{host}:{port}/session/{session_id}/abort"
            )
            return resp.status_code == 200
    except Exception:
        return False


# ── Async Client ──────────────────────────────────────────────────


class KimixAsyncClient:
    """Async HTTP + SSE client for kimix serve (opencode-style API).

    Uses orjson for JSON and httpx for HTTP transport.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4096,
        timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self._base_url = f"http://{host}:{port}"
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), trust_env=False
        )

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except RuntimeError as exc:
            if "Event loop is closed" not in str(exc):
                raise

    async def __aenter__(self) -> "KimixAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> bool:
        await self.close()
        return False

    # ── Health ────────────────────────────────────────────────────

    async def health_check(self) -> HealthResponse:
        resp = await self._client.get(f"{self._base_url}/global/health")
        resp.raise_for_status()
        data = _json_loads(resp.content)
        return HealthResponse.from_dict(data)

    # ── Session CRUD ─────────────────────────────────────────────

    async def create_session(
        self, title: Optional[str] = None
    ) -> SessionResponse:
        body = {"title": title} if title else {}
        resp = await self._client.post(
            f"{self._base_url}/session",
            content=_json_dumps(body),
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return SessionResponse.from_dict(_json_loads(resp.content))

    async def get_session(self, session_id: str) -> SessionResponse:
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}"
        )
        resp.raise_for_status()
        return SessionResponse.from_dict(_json_loads(resp.content))

    async def delete_session(self, session_id: str) -> bool:
        resp = await self._client.delete(
            f"{self._base_url}/session/{session_id}"
        )
        return resp.status_code == 200

    async def list_sessions(self) -> List[SessionResponse]:
        resp = await self._client.get(f"{self._base_url}/session")
        resp.raise_for_status()
        return [
            SessionResponse.from_dict(s) for s in _json_loads(resp.content)
        ]

    async def get_all_session_status(self) -> Dict[str, SessionStatusResponse]:
        resp = await self._client.get(f"{self._base_url}/session/status")
        resp.raise_for_status()
        data = _json_loads(resp.content)
        return {
            k: SessionStatusResponse.from_dict(v)
            for k, v in data.items()
        }

    # ── Messages ─────────────────────────────────────────────────

    async def get_messages(
        self, session_id: str, limit: Optional[int] = None
    ) -> List[MessageResponse]:
        params: Dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}/message", params=params
        )
        resp.raise_for_status()
        return [
            MessageResponse.from_dict(m) for m in _json_loads(resp.content)
        ]

    async def send_prompt_async(
        self,
        session_id: str,
        text: str,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> bool:
        """Fire-and-forget prompt via prompt_async endpoint (HTTP 204)."""
        body = PromptInput(
            parts=[PromptPart(type="text", text=text)],
            agent=agent,
            model=model,
        )
        resp = await self._client.post(
            f"{self._base_url}/session/{session_id}/prompt_async",
            content=_json_dumps(body.to_dict()),
            headers={"Content-Type": "application/json"},
        )
        return resp.status_code == 204

    async def send_prompt(
        self, session_id: str, text: str) -> bool:
        """Fire-and-forget prompt via simple prompt endpoint (HTTP 204)."""
        body = {"text": text}
        resp = await self._client.post(
            f"{self._base_url}/session/{session_id}/prompt",
            content=_json_dumps(body),
            headers={"Content-Type": "application/json"},
        )
        return resp.status_code == 204

    async def get_output(self, session_id: str) -> Dict[str, Any]:
        """Get current accumulated output text."""
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}/output"
        )
        resp.raise_for_status()
        return _json_loads(resp.content)

    async def get_state(self, session_id: str) -> Dict[str, Any]:
        """Get session state (idle / processing)."""
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}/state"
        )
        resp.raise_for_status()
        return _json_loads(resp.content)

    # ── Control ──────────────────────────────────────────────────

    async def abort_session(self, session_id: str) -> bool:
        resp = await self._client.post(
            f"{self._base_url}/session/{session_id}/abort"
        )
        return resp.status_code == 200

    async def grant_permission(
        self, session_id: str, permission_id: str
    ) -> bool:
        resp = await self._client.post(
            f"{self._base_url}/session/{session_id}/permissions/{permission_id}"
        )
        return resp.status_code == 200

    # ── Session Operations ───────────────────────────────────────

    async def clear_session(self, session_id: str) -> Dict[str, Any]:
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}/clear"
        )
        resp.raise_for_status()
        return _json_loads(resp.content)

    async def get_session_context(self, session_id: str) -> Dict[str, Any]:
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}/context"
        )
        resp.raise_for_status()
        return _json_loads(resp.content)

    async def compact_session(
        self, session_id: str, keep: Optional[int] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if keep is not None:
            params["keep"] = keep
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}/compact", params=params
        )
        resp.raise_for_status()
        return _json_loads(resp.content)

    async def export_session(
        self, session_id: str, output_path: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if output_path is not None:
            params["output_path"] = output_path
        resp = await self._client.get(
            f"{self._base_url}/session/{session_id}/export", params=params
        )
        resp.raise_for_status()
        return _json_loads(resp.content)

    # ── SSE Streaming ────────────────────────────────────────────

    async def stream_events(
        self,
        session_id: str,
        timeout: float = 14400.0,
    ) -> AsyncIterator[SSEEvent]:
        """Stream SSE events from /event endpoint."""
        url = f"{self._base_url}/event"
        request = self._client.build_request(
            "GET",
            url,
            timeout=httpx.Timeout(timeout, connect=10.0, read=timeout),
        )
        response = await self._client.send(request, stream=True)
        try:
            response.raise_for_status()
            async for event in _parse_sse_stream(response):
                yield event
        finally:
            await response.aclose()

    async def stream_events_robust(
        self,
        session_id: str,
        timeout: float = 14400.0,
        max_reconnects: int = 5,
        reconnect_delay: float = 2.0,
        on_reconnect: Optional[Callable[[int], None]] = None,
    ) -> AsyncIterator[SSEEvent]:
        """SSE stream with auto-reconnect."""
        reconnects = 0
        while reconnects <= max_reconnects:
            try:
                async for event in self.stream_events(session_id, timeout):
                    reconnects = 0
                    yield event
                return
            except (
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.ConnectError,
                httpx.ReadTimeout,
            ) as exc:
                reconnects += 1
                if reconnects > max_reconnects:
                    logger.error("[SSE] Max reconnects reached: %s", exc)
                    raise
                logger.warning(
                    "[SSE] Reconnecting (%d/%d): %s",
                    reconnects,
                    max_reconnects,
                    exc,
                )
                if on_reconnect:
                    on_reconnect(reconnects)
                await asyncio.sleep(reconnect_delay * reconnects)
                yield SSEEvent(
                    event="__reconnected__", data=str(reconnects)
                )


# ── Sync Client ───────────────────────────────────────────────────


class KimixHttpClient:
    """Synchronous HTTP client for kimix serve.

    Wraps all async operations with asyncio.run() for convenience
    in synchronous contexts.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4096,
        timeout: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._async_client = KimixAsyncClient(
            host=host, port=port, timeout=timeout
        )

    def _run(self, coro):
        """Run *coro* with a fresh async client to avoid stale loop bindings."""
        client = KimixAsyncClient(
            host=self._host, port=self._port, timeout=self._timeout
        )
        try:
            return asyncio.run(coro(client))
        finally:
            try:
                asyncio.run(client.close())
            except RuntimeError:
                pass

    def close(self) -> None:
        try:
            asyncio.run(self._async_client.close())
        except RuntimeError:
            pass

    def __enter__(self) -> "KimixHttpClient":
        return self

    def __exit__(self, *args: Any) -> bool:
        self.close()
        return False

    # ── Health ────────────────────────────────────────────────────

    def health_check(self) -> HealthResponse:
        return self._run(lambda c: c.health_check())

    # ── Session CRUD ─────────────────────────────────────────────

    def create_session(
        self, title: Optional[str] = None
    ) -> SessionResponse:
        return self._run(lambda c: c.create_session(title=title))

    def get_session(self, session_id: str) -> SessionResponse:
        return self._run(lambda c: c.get_session(session_id))

    def delete_session(self, session_id: str) -> bool:
        return self._run(lambda c: c.delete_session(session_id))

    def list_sessions(self) -> List[SessionResponse]:
        return self._run(lambda c: c.list_sessions())

    def get_all_session_status(self) -> Dict[str, SessionStatusResponse]:
        return self._run(lambda c: c.get_all_session_status())

    # ── Messages ─────────────────────────────────────────────────

    def get_messages(
        self, session_id: str, limit: Optional[int] = None
    ) -> List[MessageResponse]:
        return self._run(
            lambda c: c.get_messages(session_id, limit=limit)
        )

    def send_prompt_async(
        self,
        session_id: str,
        text: str,
        agent: Optional[str] = None,
        model: Optional[str] = None,
    ) -> bool:
        return self._run(
            lambda c: c.send_prompt_async(
                session_id, text, agent=agent, model=model
            )
        )

    def send_prompt(self, session_id: str, text: str) -> bool:
        return self._run(
            lambda c: c.send_prompt(session_id, text)
        )

    def get_output(self, session_id: str) -> Dict[str, Any]:
        return self._run(lambda c: c.get_output(session_id))

    def get_state(self, session_id: str) -> Dict[str, Any]:
        return self._run(lambda c: c.get_state(session_id))

    # ── Control ──────────────────────────────────────────────────

    def abort_session(self, session_id: str) -> bool:
        return self._run(lambda c: c.abort_session(session_id))

    def grant_permission(
        self, session_id: str, permission_id: str
    ) -> bool:
        return self._run(
            lambda c: c.grant_permission(session_id, permission_id)
        )

    # ── Session Operations ───────────────────────────────────────

    def clear_session(self, session_id: str) -> Dict[str, Any]:
        return self._run(lambda c: c.clear_session(session_id))

    def get_session_context(self, session_id: str) -> Dict[str, Any]:
        return self._run(
            lambda c: c.get_session_context(session_id)
        )

    def compact_session(
        self, session_id: str, keep: Optional[int] = None
    ) -> Dict[str, Any]:
        return self._run(
            lambda c: c.compact_session(session_id, keep=keep)
        )

    def export_session(
        self, session_id: str, output_path: Optional[str] = None
    ) -> Dict[str, Any]:
        return self._run(
            lambda c: c.export_session(
                session_id, output_path=output_path
            )
        )


# ── SSE Stream Parser (internal) ──────────────────────────────────


async def _parse_sse_stream(
    response: httpx.Response,
) -> AsyncIterator[SSEEvent]:
    """Parse HTTP response body into SSEEvent stream.

    Handles the opencode SSE format where:
    - No `event:` field is used
    - Events are `data: {json}` followed by blank line
    - Comments (`: ...`) are ignored
    """
    current = SSEEvent()
    data_lines: List[str] = []

    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r\n")

        if not line:
            if data_lines or current.event:
                current.data = "\n".join(data_lines)
                yield current
                current = SSEEvent()
                data_lines = []
            continue

        if line.startswith(":"):
            continue  # SSE comment / heartbeat

        if ":" in line:
            field_name, _, value = line.partition(":")
            value = value.lstrip(" ")
        else:
            field_name = line
            value = ""

        if field_name == "event":
            current.event = value
        elif field_name == "data":
            data_lines.append(value)
        elif field_name == "id":
            current.id = value

    if data_lines or current.event:
        current.data = "\n".join(data_lines)
        yield current
