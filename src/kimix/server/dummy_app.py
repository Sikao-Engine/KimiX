# -*- coding: utf-8 -*-
"""Dummy Kimix HTTP server (FastAPI + SSE).

Mirrors `src/kimix/server/app.py` but uses DummySessionManager so all
endpoints print request info and return stub responses — no real logic.

All 15 routes are preserved; the SSE /event stream is stubbed (no bus dependency).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from kimix.server.bus import bus, BusEvent
from kimix.server.dummy_session_manager import session_manager  # <-- dummy

logger = logging.getLogger(__name__)

VERSION = "0.1.0"


# ── Request / Response Models (identical to app.py) ──────────────


class CreateSessionRequest(BaseModel):
    title: Optional[str] = Field(None, description="Session title")
    supervisor: bool = Field(False, description="Create a supervisor session")
    ralph_loop: int = Field(0, description="Max Ralph loop iterations (0 = default)")


class PromptPart(BaseModel):
    type: str = Field("text", description="Part type: text")
    text: str = Field("", description="Text content")


class PromptInput(BaseModel):
    parts: List[PromptPart] = Field(default_factory=list, description="Message parts")
    model: Optional[str] = Field(None, description="Model name to use")


class PlanConfirmInput(BaseModel):
    action: str = Field(..., description="Action: accept or revise")
    feedback: Optional[str] = Field(None, description="Revision feedback when action=revise")


# ── OpenAPI Response Models ──────────────────────────────────────


class HealthResponse(BaseModel):
    healthy: bool = Field(..., description="Server health status")
    version: str = Field(..., description="API version")


class SessionResponse(BaseModel):
    id: str = Field(..., description="Session ID (ses_ prefix)")
    title: Optional[str] = Field(None, description="Session title")
    createdAt: float = Field(..., description="Creation timestamp (unix)")
    updatedAt: float = Field(..., description="Last update timestamp (unix)")
    parentID: Optional[str] = Field(None, description="Parent session ID")


class SessionStatusResponse(BaseModel):
    type: str = Field(..., description="Status: running | idle")
    token_count: int = Field(0, description="Context token count (when idle)")
    context_usage: float = Field(0.0, description="Context usage percentage (when idle)")


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Error detail message")


# ── Application Factory ─────────────────────────────────────────


def create_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        logger.info("Dummy server shutting down")

    app = FastAPI(
        title="Kimix API (Dummy)",
        version=VERSION,
        description="Dummy Kimix opencode-style REST API server. All responses are stubs. Use /docs for interactive Swagger UI.",
        docs_url="/docs",
        openapi_url="/openapi.json",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health ────────────────────────────────────────────────

    @app.get(
        "/global/health",
        response_model=HealthResponse,
        tags=["Health"],
        summary="Health check",
        description="Returns server health status and API version.",
    )
    async def health() -> Dict[str, Any]:
        print("[DummyApp] GET /global/health")
        return {"healthy": True, "version": VERSION}

    # ── SSE Event Stream (stub) ───────────────────────────────

    @app.get(
        "/event",
        tags=["Events"],
        summary="SSE event stream",
        description=(
            "Server-Sent Events stream (dummy — sends connected + heartbeat only)."
        ),
    )
    async def event_stream(request: Request) -> StreamingResponse:
        print("[DummyApp] GET /event")

        async def _generate():
            # Initial connected event
            yield BusEvent(type="server.connected", properties={}).to_sse()

            q = bus.create_async_queue()
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=10.0)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield BusEvent(type="server.heartbeat", properties={}).to_sse()
                        continue
                    except asyncio.CancelledError:
                        break
                    if event is None:
                        break
                    yield event.to_sse()
            finally:
                bus.remove_async_queue(q)

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Session CRUD ─────────────────────────────────────────

    @app.post(
        "/session",
        response_model=SessionResponse,
        tags=["Session"],
        summary="Create session",
        description="Create a new chat session. Returns the session metadata.",
        status_code=200,
    )
    async def create_session(body: CreateSessionRequest) -> Dict[str, Any]:
        info = await session_manager.create_session(
            title=body.title,
            supervisor=body.supervisor,
            ralph_loop=body.ralph_loop,
        )
        return info.to_dict()

    @app.get(
        "/session",
        response_model=List[SessionResponse],
        tags=["Session"],
        summary="List sessions",
        description="List all active sessions, sorted by most recently updated.",
    )
    async def list_sessions() -> List[Dict[str, Any]]:
        return [s.to_dict() for s in session_manager.list_sessions()]

    @app.get(
        "/session/{sessionID}/status",
        response_model=SessionStatusResponse,
        tags=["Session"],
        summary="Get session status",
        description="Returns running/idle status. When idle, includes context usage and token count.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def session_status(sessionID: str) -> Dict[str, Any]:
        try:
            return session_manager.get_session_status(sessionID).to_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

    @app.get(
        "/session/{sessionID}",
        response_model=SessionResponse,
        tags=["Session"],
        summary="Get session",
        description="Get metadata for a specific session by ID.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def get_session(sessionID: str) -> Dict[str, Any]:
        try:
            return session_manager.get_session(sessionID).to_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

    @app.delete(
        "/session/{sessionID}",
        tags=["Session"],
        summary="Delete session",
        description="Delete a session and close its underlying SDK session.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
        status_code=200,
    )
    async def delete_session(sessionID: str) -> Response:
        ok = await session_manager.delete_session(sessionID)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return Response(status_code=200)

    # ── Messages ─────────────────────────────────────────────

    @app.get(
        "/session/{sessionID}/message",
        tags=["Message"],
        summary="Get messages",
        description="Get messages for a session. Optionally limit the number of most recent messages.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def get_messages(
        sessionID: str,
        limit: Optional[int] = Query(default=None, description="Maximum number of messages to return"),
    ) -> List[Dict[str, Any]]:
        try:
            value = session_manager.get_messages(sessionID, limit=limit)
            return value
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

    # ── Prompt Async (fire-and-forget) ───────────────────────

    @app.post(
        "/session/{sessionID}/prompt_async",
        status_code=204,
        tags=["Message"],
        summary="Send message (async)",
        description="Send a prompt fire-and-forget style. Returns 204 immediately. Response events are streamed via SSE /event.",
        responses={
            404: {"model": ErrorResponse, "description": "Session not found"},
            400: {"model": ErrorResponse, "description": "Invalid input"},
        },
    )
    async def send_prompt_async(sessionID: str, body: PromptInput) -> Response:
        text_parts = [p.text for p in body.parts if p.type == "text" and p.text]
        text = "\n".join(text_parts)
        if not text:
            raise HTTPException(status_code=400, detail="No text content in parts")
        text = text.strip()
        try:
            if text:
                await session_manager.prompt_async(
                    sessionID, text
                )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return Response(status_code=204)

    # ── Plan ───────────────────────────────────────────────

    @app.post(
        "/session/{sessionID}/plan",
        status_code=204,
        tags=["Plan"],
        summary="Generate plan",
        description="Generate an implementation plan using the planner agent. Returns 204 immediately. Plan events stream via SSE /event.",
        responses={
            404: {"model": ErrorResponse, "description": "Session not found"},
            400: {"model": ErrorResponse, "description": "Invalid input"},
        },
    )
    async def plan_session(sessionID: str, body: PromptInput) -> Response:
        text_parts = [p.text for p in body.parts if p.type == "text" and p.text]
        text = "\n".join(text_parts)
        if not text:
            raise HTTPException(status_code=400, detail="No text content in parts")
        text = text.strip()
        try:
            await session_manager.plan_async(sessionID, text)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return Response(status_code=204)

    @app.post(
        "/session/{sessionID}/plan/confirm",
        tags=["Plan"],
        summary="Confirm or revise plan",
        description="Accept the generated plan or provide revision feedback.",
        responses={
            404: {"model": ErrorResponse, "description": "Session not found"},
        },
    )
    async def confirm_plan(sessionID: str, body: PlanConfirmInput) -> bool:
        try:
            return await session_manager.confirm_plan_async(
                sessionID, action=body.action, feedback=body.feedback
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

    # ── Abort ────────────────────────────────────────────────

    @app.post(
        "/session/{sessionID}/abort",
        tags=["Session"],
        summary="Abort session",
        description="Abort the current running prompt in a session.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
        status_code=200,
    )
    async def abort_session(sessionID: str) -> Response:
        try:
            session_manager.abort_session(sessionID)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return Response(status_code=200)

    # ── Permissions ──────────────────────────────────────────

    @app.post(
        "/session/{sessionID}/permissions/{permissionID}",
        tags=["Session"],
        summary="Grant permission",
        description="Grant a pending permission request.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
        status_code=200,
    )
    async def grant_permission(sessionID: str, permissionID: str) -> Response:
        print(f"[DummyApp] POST /session/{sessionID}/permissions/{permissionID}")
        return Response(status_code=200)

    # ── Options ──────────────────────────────────────────────

    @app.get(
        "/session/{sessionID}/clear",
        tags=["Options"],
        summary="Clear session",
        description="Clear a specific session and return a confirmation.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def clear_session(sessionID: str) -> Dict[str, Any]:
        try:
            await session_manager.clear_session(sessionID)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return {"cleared": 1, "sessionID": sessionID}

    @app.get(
        "/session/{sessionID}/context",
        tags=["Options"],
        summary="Get session context",
        description="Return context for a specific session.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def get_session_context(sessionID: str) -> Dict[str, Any]:
        try:
            return await session_manager.get_session_context(sessionID)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

    @app.get(
        "/session/{sessionID}/compact",
        tags=["Options"],
        summary="Compact session",
        description="Compact a specific session by trimming message history.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def compact_session(
        sessionID: str,
        keep: Optional[int] = Query(default=10, ge=0, description="Number of recent messages to keep"),
    ) -> Dict[str, Any]:
        try:
            await session_manager.compact_session(sessionID, keep=keep)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return {"compacted": 1, "sessionID": sessionID, "keep": keep}

    @app.get(
        "/session/{sessionID}/export",
        tags=["Options"],
        summary="Export session",
        description="Export a specific session to a file.",
        responses={
            404: {"model": ErrorResponse, "description": "Session not found"},
            400: {"model": ErrorResponse, "description": "Invalid input"},
        },
    )
    async def export_session(
        sessionID: str,
        output_path: Optional[str] = Query(default=None, description="Output file path"),
    ) -> Dict[str, Any]:
        try:
            output, count = await session_manager.export_session(sessionID, output_path=output_path)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"output": output, "count": count, "sessionID": sessionID}

    return app
