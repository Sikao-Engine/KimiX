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
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson

from kimi_cli.utils.logging import logger

#: Per-message cap on text appended to a tool result (contract §5).
MAX_APPEND_CHARS = 4096

#: Default per-server JSON-RPC call timeout, seconds (contract §5).
DEFAULT_TIMEOUT = 5.0


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

    Never raises: any exception is logged and reported as ``None`` so the
    tool proceeds exactly as if no server existed.
    """
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
