"""Session-server bridge for the file tools' ``connect`` parameter.

Before a file tool runs its core logic, :func:`before_tool` asks every live
session-server sub-process (the SDK ``kimi_agent_sdk._server_process`` live
registry) for extra information about the file about to be touched, and the
reply is appended to the tool result's ``message`` via :func:`merge_message`.

The bridge is an *enhancement channel only*: every failure mode (import
cycle, empty registry, connection refused, timeout, JSON-RPC error,
malformed result) is swallowed, logged, and degrades to "no extra
information". It must NEVER break a tool call.

Contract: ``docs/tool_connect_bridge.md`` (repo root ``docs/``).

Single-flight (one ``connect`` per file at a time)
--------------------------------------------------
Several file tools may run *concurrently* against the same file (a model step
emits ``read`` + ``edit`` + ``write`` on one path and the toolset dispatches
them as parallel tasks). Sending one ``before_tool`` RPC per call would hit the
server several times for the same target with redundant work.

:func:`before_tool` therefore coalesces: the first caller for a target set
starts the RPC, later callers that target the same file(s) in the same ``cwd``
join that in-flight request and share its answer instead of opening their own
connection ("one connect at a time, redundant ones removed"). The in-flight
entry is dropped as soon as the request settles, so a *sequential* later call
still gets a fresh answer — no TTL cache, no stale diagnostics.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import orjson

from kimi_cli.utils.logging import logger

#: Per-message cap on text appended to a tool result (contract §5).
MAX_APPEND_CHARS = 4096

#: Default per-server JSON-RPC call timeout, seconds (contract §5).
DEFAULT_TIMEOUT = 5.0

#: Coalescing key: ``(cwd, sorted normalised targets)``.
_CoalesceKey = tuple[str | None, tuple[str, ...]]

#: In-flight ``before_tool`` requests, keyed by target set (single-flight map).
#: Values are the shared tasks; an entry lives only while the RPC runs.
_inflight: dict[_CoalesceKey, asyncio.Task[str | None]] = {}


def _normalize_target(value: Any) -> str:
    """Best-effort normalisation used *only* for the coalescing key.

    The payload sent to the server keeps the caller's raw strings (contract
    §2); this helper just makes ``a\\b.py``, ``a/b.py`` and ``./a/b.py`` share
    one key. It never raises and never touches the wire payload.
    """
    try:
        text = str(value).strip()
    except Exception:
        return ""
    if not text:
        return ""
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    while text.startswith("./"):
        text = text[2:]
    if text in (".", ""):
        return ""
    if os.name == "nt":
        # Windows paths are case-insensitive, so ``C:/Work/A.py`` and
        # ``c:/work/a.py`` are the same target. Never folded on POSIX, where
        # they are genuinely different files.
        text = text.casefold()
    return text


def _normalize_cwd(cwd: str | None) -> str | None:
    """Same dedupe-only normalisation for the working directory."""
    try:
        if cwd is None:
            return None
        text = str(cwd).strip()
    except Exception:
        return None
    text = text.replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    return text.casefold() if os.name == "nt" else text


def _coalesce_key(cwd: str | None, path: str | None, paths: list[str] | None) -> _CoalesceKey:
    """Build the single-flight key for one ``before_tool`` request."""
    targets: list[str] = []
    if paths:
        for item in paths:
            targets.append(_normalize_target(item))
    elif path is not None:
        targets.append(_normalize_target(path))
    cwd_key = _normalize_cwd(cwd)
    return cwd_key, tuple(sorted({t for t in targets if t}))


def _forget(key: _CoalesceKey, task: asyncio.Task[str | None]) -> None:
    """Drop a settled entry (never evict a newer task registered under the key)."""
    if _inflight.get(key) is task:
        _inflight.pop(key, None)
    # Always consume the exception slot: the shared task's result is delivered
    # through ``_join``, and an unretrieved exception would be logged as noise.
    if not task.cancelled():
        task.exception()


async def _join(task: asyncio.Task[str | None]) -> str | None:
    """Await a shared request; caller cancellation must not cancel it for others."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("session bridge: shared before_tool result failed", exc_info=True)
        return None


def _reset_inflight_for_tests() -> None:
    """Clear the single-flight map (tests only)."""
    _inflight.clear()


async def before_tool(
    tool_name: str,
    action: str,
    *,
    path: str | None = None,
    paths: list[str] | None = None,
    cwd: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """Query live session servers before a tool call; return joined info or None.

    The request payload always carries all five keys ``{tool, action, path,
    paths, cwd}`` (``null`` when unset); ``path`` is the primary/first target
    and ``paths`` is the full list only when there are several targets.

    Concurrent calls targeting the same file(s) share a single in-flight RPC
    (see the module docstring): the request that starts first runs, later ones
    with the same ``(cwd, targets)`` key join it and receive the same reply.

    Never raises: any exception is logged and reported as ``None`` so the
    tool proceeds exactly as if no server existed.
    """
    try:
        key = _coalesce_key(cwd, path, paths)
        existing = _inflight.get(key)
        if existing is not None and not existing.done():
            try:
                same_loop = existing.get_loop() is asyncio.get_running_loop()
            except Exception:
                same_loop = False
            if same_loop:
                logger.debug(
                    "session bridge: joining in-flight before_tool for {key}",
                    key=key,
                )
                return await _join(existing)
        task = asyncio.ensure_future(
            _query_all(tool_name, action, path=path, paths=paths, cwd=cwd, timeout=timeout)
        )
        _inflight[key] = task
        task.add_done_callback(lambda done, k=key: _forget(k, done))
        return await _join(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("session bridge: coalescing wrapper failed", exc_info=True)
    return await _query_all(tool_name, action, path=path, paths=paths, cwd=cwd, timeout=timeout)


async def _query_all(
    tool_name: str,
    action: str,
    *,
    path: str | None = None,
    paths: list[str] | None = None,
    cwd: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str | None:
    """The actual (non-coalesced) query; never raises, returns joined info/None."""
    try:
        # NOTE: lazy inline import to avoid the circular dependency
        # (kimix → kimi_agent_sdk → kimi_cli); same pattern as
        # kimi_cli/soul/toolset.py. If the SDK is unavailable there are
        # no session servers either → no info.
        try:
            from kimi_agent_sdk._server_process import (  # fmt: skip
                get_live_server_processes,
            )
        except Exception:
            logger.debug("session bridge: SDK live-server registry import failed", exc_info=True)
            return None

        try:
            candidates = list(get_live_server_processes())
        except Exception:
            logger.debug("session bridge: registry snapshot failed", exc_info=True)
            return None

        servers: list[Any] = []
        for srv in candidates:
            try:
                if (
                    srv.ready
                    and srv.process is not None
                    and srv.process.returncode is None
                    and srv.network in ("tcp", "http")
                ):
                    servers.append(srv)
            except Exception:
                # Malformed / partially initialized entry: skip, re-check is
                # per contract §4.
                continue
        if not servers:
            # Fast path: no qualifying server, no client, no socket.
            return None

        payload: dict[str, Any] = {
            "tool": tool_name,
            "action": action,
            "path": path,
            "paths": paths,
            "cwd": cwd,
        }
        messages: list[str] = []
        for srv in servers:
            info = await _query_server(srv, payload, timeout)
            if info:
                messages.append(info)
        if not messages:
            return None
        return "\n".join(messages)
    except Exception:
        # Catch-all: the bridge must never break a tool call.
        logger.debug("session bridge: before_tool failed for %s", tool_name, exc_info=True)
        return None


def merge_message(existing: str, info: str | None) -> str:
    """Append ``[server] <info>`` to a tool message (contract §6).

    ``info`` None/empty → ``existing`` returned byte-identical. An empty
    ``existing`` gets just the marker line; otherwise the block is separated
    by a blank line.
    """
    if not info:
        return existing
    if not existing:
        return f"[server] {info}"
    return f"{existing}\n\n[server] {info}"


async def _query_server(srv: Any, payload: dict[str, Any], timeout: float) -> str | None:
    """One ``before_tool`` round trip against one server; None on any failure."""
    # NOTE: lazy import — keeps this module importable without the app layer.
    from kimi_cli.session_server import JsonRpcClient

    client: JsonRpcClient | None = None
    result: Any = None
    try:
        if srv.network == "tcp":
            # wait_for bounds connect() too: an unreachable host must not hang
            # the tool beyond the agreed per-server budget.
            client = await asyncio.wait_for(
                JsonRpcClient.connect_tcp(srv.address, srv.port), timeout
            )
        else:
            client = await asyncio.wait_for(
                JsonRpcClient.connect_http(srv.address, srv.port), timeout
            )
        # Bound the *whole* round trip by ``timeout``: client.call's internal
        # per-read timeouts can stack up (e.g. a server dribbling stale
        # responses), so the nominal budget must be enforced here.
        result = await asyncio.wait_for(
            client.call("before_tool", payload, timeout=timeout), timeout
        )
    except Exception:
        logger.debug(
            "session bridge: before_tool call to %s %s:%s failed",
            srv.network,
            srv.address,
            srv.port,
            exc_info=True,
        )
        return None
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.debug("session bridge: client close failed", exc_info=True)
    return _extract_message(result)


def _extract_message(result: Any) -> str | None:
    """Contract §5 extraction: str as-is; dict → ``message``; else compact JSON.

    Whitespace-stripped, capped at MAX_APPEND_CHARS, empty/None → None.
    """
    if result is None:
        return None
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        message = result.get("message")
        text = message if isinstance(message, str) else _compact_json(result)
    else:
        text = _compact_json(result)
    text = text.strip()
    if not text:
        return None
    return text[:MAX_APPEND_CHARS]


def _compact_json(value: Any) -> str:
    try:
        return orjson.dumps(value, default=str).decode("utf-8")
    except Exception:
        return str(value)
