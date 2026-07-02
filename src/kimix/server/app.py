# -*- coding: utf-8 -*-
"""Kimix opencode-style HTTP server (FastAPI + SSE).

Provides REST API endpoints compatible with the opencode serve interface so
the official opencode web / SDK client can connect without changes.

Routes (opencode-standard):
    GET  /global/health                    — Health check
    GET  /event                            — SSE event stream (instance)
    GET  /global/event                     — SSE event stream (global alias)
    GET  /config                           — Instance config
    GET  /config/providers                 — Providers + default model map
    GET  /experimental/features            — Feature capability discovery
    GET  /project                          — List projects
    GET  /project/current                  — Current project
    GET  /path                             — Path info
    GET  /agent                            — List agents
    POST /session                          — Create session
    GET  /session                          — List sessions
    GET  /session/status                   — Get all session statuses
    GET  /session/{id}                     — Get session info
    DELETE /session/{id}                   — Delete session (returns bool)
    GET  /session/{id}/message             — Get messages (WithParts[])
    GET  /session/{id}/status               — Get session status (idle/busy/retry)
    GET  /session/{id}/todo                — Session todo list
    POST /session/{id}/message             — Send message (sync, WithParts)
    POST /session/{id}/prompt_async        — Send message (fire-and-forget, 204)
    POST /session/{id}/abort               — Abort session (returns bool)
    POST /session/{id}/summarize           — Summarize / compact session (returns bool)
    POST /session/{id}/permissions/{permissionID} — Grant permission
    GET  /session/{id}/clear               — Clear session
    GET  /session/{id}/context             — Get session context
    GET  /session/{id}/compact             — Compact session
    GET  /session/{id}/export              — Export session
"""

from __future__ import annotations

import asyncio
import contextlib
import orjson
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from kimix.server.bus import bus, BusEvent
from kimix.server.session_manager import session_manager

logger = logging.getLogger(__name__)

VERSION = "0.1.0"


# ── Request / Response Models ────────────────────────────────────


class CreateSessionRequest(BaseModel):
    parentID: Optional[str] = Field(None, description="Parent session ID")
    title: Optional[str] = Field(None, description="Session title")
    agent: Optional[str] = Field(None, description="Agent name")


class ModelRef(BaseModel):
    providerID: str = Field("", description="Provider ID")
    modelID: str = Field("", description="Model ID")


class PromptPart(BaseModel):
    id: Optional[str] = Field(None, description="Part ID")
    type: str = Field("text", description="Part type: text | file | agent")
    text: str = Field("", description="Text content (for text parts)")


class PromptInput(BaseModel):
    messageID: Optional[str] = Field(None, description="Client message ID")
    model: Optional[ModelRef] = Field(None, description="Model to use")
    agent: Optional[str] = Field(None, description="Agent name to use")
    system: Optional[str] = Field(None, description="System prompt override")
    parts: List[PromptPart] = Field(default_factory=list, description="Message parts")


# ── OpenAPI Response Models ──────────────────────────────────────


class HealthResponse(BaseModel):
    healthy: bool = Field(..., description="Server health status")
    version: str = Field(..., description="API version")


class SessionTime(BaseModel):
    created: int = Field(..., description="Creation timestamp (unix ms)")
    updated: int = Field(..., description="Last update timestamp (unix ms)")


class SessionResponse(BaseModel):
    id: str = Field(..., description="Session ID (ses_ prefix)")
    projectID: str = Field(..., description="Project ID")
    directory: str = Field(..., description="Working directory")
    parentID: Optional[str] = Field(None, description="Parent session ID")
    title: str = Field(..., description="Session title")
    version: str = Field(..., description="Server version")
    time: SessionTime = Field(..., description="Session timestamps")


class SessionStatusResponse(BaseModel):
    type: str = Field(..., description="Status: idle | busy | retry")


class FeatureInfo(BaseModel):
    enabled: bool = Field(..., description="Whether the feature is available")
    title: Optional[str] = Field(None, description="Human-readable label")
    description: Optional[str] = Field(
        None, description="Feature description / hint shown when unavailable"
    )


class FeaturesResponse(BaseModel):
    features: Dict[str, FeatureInfo] = Field(
        default_factory=dict, description="Map of feature name to capability info"
    )


class SummarizeRequest(BaseModel):
    providerID: Optional[str] = Field(None, description="Provider to summarize with")
    modelID: Optional[str] = Field(None, description="Model to summarize with")
    keep: Optional[int] = Field(None, description="Number of recent messages to keep")


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="Error detail message")


# ── Application Factory ─────────────────────────────────────────


def create_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        logger.info("Server shutting down, waking up SSE streams")
        for q in bus.get_all_queues():
            try:
                q.put_nowait(None)
            except Exception:
                pass

    app = FastAPI(
        title="Kimix API",
        version=VERSION,
        description="Kimix opencode-style REST API server. Use /docs for interactive Swagger UI.",
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
        return {"healthy": True, "version": VERSION}

    # ── SSE Event Stream ─────────────────────────────────────
    #
    # OpenCode wire format: `event: message` + `id: <evt>` + `data: {json}`.
    # The JSON payload carries `{id, type, properties}`. We use a raw
    # StreamingResponse for full control over the frame layout.

    async def _event_stream_response(request: Request) -> StreamingResponse:
        async def _generate():  # type: ignore[return]
            # Initial connected event (opencode emits this with an id).
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
                        # opencode emits a real heartbeat event (with id),
                        # not an SSE comment.
                        yield BusEvent(
                            type="server.heartbeat", properties={}
                        ).to_sse()
                        continue
                    except asyncio.CancelledError:
                        break
                    if event is None:
                        break
                    yield event.to_sse()
            finally:
                bus.remove_async_queue(q)
                logger.info("SSE client disconnected")

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get(
        "/event",
        tags=["Events"],
        summary="SSE event stream",
        description=(
            "Server-Sent Events stream for real-time session updates. "
            "Instance endpoint — pushes events for ALL sessions. "
            "Each frame is `event: message` + `id:` + `data: {id,type,properties}`."
        ),
    )
    async def event_stream(request: Request) -> StreamingResponse:
        return await _event_stream_response(request)

    @app.get(
        "/global/event",
        tags=["Events"],
        summary="Global SSE event stream",
        description="Alias of /event for opencode global event subscribers.",
    )
    async def global_event_stream(request: Request) -> StreamingResponse:
        return await _event_stream_response(request)

    # ── Bootstrap (opencode client startup probes) ───────────

    @app.get(
        "/config",
        tags=["Config"],
        summary="Get config",
        description="Return the instance configuration. Minimal kimix stub.",
    )
    async def get_config() -> Dict[str, Any]:
        return session_manager.get_config()

    @app.get(
        "/config/providers",
        tags=["Config"],
        summary="List providers",
        description="Return available providers and the default model map.",
    )
    async def get_config_providers() -> Dict[str, Any]:
        return session_manager.get_providers()

    @app.get(
        "/experimental/features",
        response_model=FeaturesResponse,
        tags=["Config"],
        summary="List server features",
        description=(
            "Capability discovery for opencode-sse extension clients. Returns a "
            "map of feature name to {enabled, title, description}. Clients must "
            "disable any extension UI whose feature is missing or has "
            "enabled == false."
        ),
    )
    async def list_features() -> Dict[str, Any]:
        return session_manager.get_features()

    @app.get(
        "/project",
        tags=["Project"],
        summary="List projects",
        description="Return the list of known projects.",
    )
    async def list_projects() -> List[Dict[str, Any]]:
        return [session_manager.get_project()]

    @app.get(
        "/project/current",
        tags=["Project"],
        summary="Current project",
        description="Return the currently active project.",
    )
    async def current_project() -> Dict[str, Any]:
        return session_manager.get_project()

    @app.get(
        "/path",
        tags=["Project"],
        summary="Get paths",
        description="Return path information for the current instance.",
    )
    async def get_path() -> Dict[str, Any]:
        return session_manager.get_path()

    @app.get(
        "/agent",
        tags=["Config"],
        summary="List agents",
        description="Return the list of available agents.",
    )
    async def list_agents() -> List[Dict[str, Any]]:
        return session_manager.list_agents()

    # ── Session CRUD ─────────────────────────────────────────

    @app.post(
        "/session",
        response_model=SessionResponse,
        tags=["Session"],
        summary="Create session",
        description="Create a new chat session. Returns the session metadata.",
        status_code=200,
    )
    async def create_session(body: Optional[CreateSessionRequest] = None) -> Dict[str, Any]:
        body = body or CreateSessionRequest()
        info = await session_manager.create_session(
            title=body.title, parent_id=body.parentID, agent=body.agent
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
        return [s.to_dict() for s in await session_manager.list_sessions()]

    @app.get(
        "/session/status",
        response_model=Dict[str, SessionStatusResponse],
        tags=["Session"],
        summary="Get all session statuses",
        description="Returns a map of session ID to current status (idle/busy/error).",
    )
    async def session_status() -> Dict[str, Dict[str, Any]]:
        return session_manager.get_all_session_statuses()

    @app.get(
        "/session/{sessionID}/status",
        response_model=SessionStatusResponse,
        tags=["Session"],
        summary="Get per-session status",
        description="Returns running/idle status for a specific session.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def session_status_id(sessionID: str) -> Dict[str, Any]:
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
            return (await session_manager.get_session(sessionID)).to_dict()
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

    @app.delete(
        "/session/{sessionID}",
        response_model=bool,
        tags=["Session"],
        summary="Delete session",
        description="Delete a session and close its underlying SDK session.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def delete_session(sessionID: str) -> bool:
        ok = await session_manager.delete_session(sessionID)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return True

    @app.get(
        "/session/{sessionID}/todo",
        tags=["Session"],
        summary="Get session todos",
        description="Return the todo list associated with a session.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def get_todos(sessionID: str) -> List[Dict[str, Any]]:
        try:
            return session_manager.get_todos(sessionID)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

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
            return await session_manager.get_messages(sessionID, limit=limit)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")

    def _extract_text(body: PromptInput) -> str:
        text = "\n".join(
            p.text for p in body.parts if p.type == "text" and p.text
        ).strip()
        if not text:
            raise HTTPException(status_code=400, detail="No text content in parts")
        return text

    # ── Prompt (sync, returns WithParts) ─────────────────────

    @app.post(
        "/session/{sessionID}/message",
        tags=["Message"],
        summary="Send message",
        description="Create and send a message; waits for the full response and returns it as a message-with-parts object. Events also stream via SSE /event.",
        responses={
            404: {"model": ErrorResponse, "description": "Session not found"},
            400: {"model": ErrorResponse, "description": "Invalid input"},
        },
    )
    async def send_prompt(sessionID: str, body: PromptInput) -> Dict[str, Any]:
        text = _extract_text(body)
        try:
            return await session_manager.prompt(sessionID, text, agent=body.agent)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # ── Prompt Async (fire-and-forget) ───────────────────────

    @app.post(
        "/session/{sessionID}/prompt_async",
        status_code=204,
        tags=["Message"],
        summary="Send message (async)",
        description="Send a prompt fire-and-forget style. Returns 204 immediately. Response events are streamed via SSE /event. Supports slash commands: /clear, /compact, /context, /export.",
        responses={
            404: {"model": ErrorResponse, "description": "Session not found"},
            400: {"model": ErrorResponse, "description": "Invalid input"},
        },
    )
    async def send_prompt_async(sessionID: str, body: PromptInput) -> Response:
        text = _extract_text(body)
        try:
            await session_manager.prompt_async(sessionID, text, agent=body.agent)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return Response(status_code=204)

    # ── Abort ────────────────────────────────────────────────

    @app.post(
        "/session/{sessionID}/abort",
        response_model=bool,
        tags=["Session"],
        summary="Abort session",
        description="Abort the current running prompt in a session.",
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def abort_session(sessionID: str) -> bool:
        try:
            session_manager.abort_session(sessionID)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return True

    # ── Summarize / Compact (opencode-sse extension) ─────────

    @app.post(
        "/session/{sessionID}/summarize",
        response_model=bool,
        tags=["Session"],
        summary="Summarize / compact session",
        description=(
            "Compact (summarize) a session's context to shrink the context "
            "window. opencode-sse extension endpoint — advertised via "
            "/experimental/features as the `compact` feature."
        ),
        responses={404: {"model": ErrorResponse, "description": "Session not found"}},
    )
    async def summarize_session(
        sessionID: str, body: Optional[SummarizeRequest] = None
    ) -> bool:
        body = body or SummarizeRequest()
        try:
            await session_manager.compact_session(sessionID, keep=body.keep)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Session not found: {sessionID}")
        return True

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
        # Permission handling — acknowledge for now
        logger.info("Permission granted: session=%s, permission=%s", sessionID, permissionID)
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
            return session_manager.get_session_context(sessionID)
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
