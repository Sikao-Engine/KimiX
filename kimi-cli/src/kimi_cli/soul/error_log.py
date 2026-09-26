"""Comprehensive session-state snapshots for LLM provider failures.

When the chat provider (see ``kimi_cli.llm.create_llm``) keeps failing with a
remote connection error — ``APITimeoutError`` / ``APIConnectionError`` /
persistent 5xx — the agent loop exhausts every retry and raises
``SessionRestartRequired``, which the outer layers turn into an automatic
session restart::

    Auto-restarting session (1/3): Step 7: APITimeoutError [connection
    recovery exhausted] — retries exhausted, restarting session
    ⚠️ Connection lost (APITimeoutError). Restarting session (attempt 1/3)...

Every point of that path records a full debug snapshot of the session into
``<work dir>/.kimix_cache/error_log/`` so failures can be diagnosed offline:

- the error chain (type, message, status code, request id, Retry-After,
  full tracebacks, cause/context chain),
- restart bookkeeping (attempt/max, restartability, recovery flags),
- LLM provider/model state (provider type, base URL, model, thinking effort,
  generation kwargs, sanitized provider config — never raw secrets),
- loop-control / compaction / context-overflow configuration,
- the complete conversation context (system prompt + messages, capped),
- tool table, MCP status, wire tail, share-dir log tail,
- process, environment, thread and asyncio-task state.

Recording is strictly best-effort: :func:`record_session_error` never raises
and never touches the wire, so it can be called from any error path.

Tuning knobs (environment variables):

- ``KIMIX_ERROR_LOG_ENABLED``  — set to ``0``/``false`` to disable (default on).
- ``KIMIX_ERROR_LOG_DIR``      — override the output directory.
- ``KIMIX_ERROR_LOG_MAX_FILES`` — snapshot retention (default 50).
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import os
import platform
import re
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
import pendulum
from pydantic import BaseModel, SecretStr

from kaos.path import KaosPath
from kimi_cli.metadata import KIMIX_CACHE_DIR_NAME
from kimi_cli.utils.logging import logger
from kosong.chat_provider import (
    APIConnectionError,
    APIEmptyResponseError,
    APIStatusError,
    APITimeoutError,
    ChatProviderError,
)

if TYPE_CHECKING:
    from kimi_cli.session import Session as CliSession
    from kimi_cli.soul.kimisoul import KimiSoul

__all__ = [
    "DEFAULT_MAX_SNAPSHOTS",
    "ERROR_LOG_DIR_NAME",
    "PHASE_CONNECTION_ERROR",
    "PHASE_NON_RESTARTABLE",
    "PHASE_OVERFLOW_RECOVERY_FAILED",
    "PHASE_RESTART_EXHAUSTED",
    "PHASE_SESSION_RESTART",
    "PHASE_STEP_RETRIES_EXHAUSTED",
    "error_log_dir",
    "record_session_error",
]

SCHEMA_VERSION = 1
KIND = "error_log"

#: Directory name below ``<work dir>/.kimix_cache``.
ERROR_LOG_DIR_NAME = "error_log"
#: Pointer file that always names the most recent snapshot.
LATEST_POINTER_NAME = ".latest.json"

ENABLE_ENV_VAR = "KIMIX_ERROR_LOG_ENABLED"
DIR_ENV_VAR = "KIMIX_ERROR_LOG_DIR"
MAX_FILES_ENV_VAR = "KIMIX_ERROR_LOG_MAX_FILES"
DEFAULT_MAX_SNAPSHOTS = 50

# ── Snapshot phases (one snapshot per phase per failure) ─────────────────────
PHASE_CONNECTION_ERROR = "connection_error"
"""Generic hook point for a chat-provider connection failure."""
PHASE_STEP_RETRIES_EXHAUSTED = "step_retries_exhausted"
"""All per-step retries exhausted → ``SessionRestartRequired`` raised (soul)."""
PHASE_OVERFLOW_RECOVERY_FAILED = "overflow_recovery_failed"
"""Context-overflow force-compaction itself failed → restart required."""
PHASE_SESSION_RESTART = "session_restart"
"""The SDK is about to auto-restart the session (``Session.prompt``)."""
PHASE_RESTART_EXHAUSTED = "restart_exhausted"
"""The restart budget was consumed; the original error surfaces."""
PHASE_NON_RESTARTABLE = "non_restartable"
"""A deterministic error (e.g. 400) must not burn the restart budget."""

# ── Size caps (bytes/chars/records — debug output must stay bounded) ─────────
_MAX_STRING_CHARS = 20_000
_MAX_SYSTEM_PROMPT_CHARS = 120_000
_MAX_REPR_CHARS = 2_000
_MAX_TRACEBACK_CHARS = 40_000
_MAX_MESSAGE_PART_CHARS = 20_000
_MAX_HISTORY_MESSAGES = 400
_MAX_WIRE_TAIL_BYTES = 512 * 1024
_MAX_WIRE_TAIL_RECORDS = 40
_MAX_LOG_TAIL_BYTES = 256 * 1024
_MAX_LOG_TAIL_LINES = 400
_MAX_TASKS = 64
_MAX_FRAMES = 50
_MAX_DEPTH = 24

_MASKED = "***masked***"
_TRUTHY = {"1", "true", "yes", "on"}

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|apikey|token|secret|password|passwd|authorization|cookie"
    r"|credential|private[_-]?key|bearer)",
    re.IGNORECASE,
)
#: Field names that merely *mention* tokens/keys but carry counters, not
#: credentials (e.g. ``max_tokens``, ``input_tokens``) — never masked.
#: Only *known counter* words may precede ``token(s)``; anything else that
#: ends in ``token`` (``refresh_token``, ``HF_TOKEN``) stays masked.
_NOT_SECRET_KEY_RE = re.compile(
    r"^((max|min|total|input|output|prompt|completion|reasoning|cached"
    r"|thoughts|audio|source|target|used|remaining|count)[_-]?tokens?"
    r"|tokens?_count|tokens?_used)$"
    r"|^(max|min|total|used|remaining|count)[_-]",
    re.IGNORECASE,
)
_ENV_KEY_RE = re.compile(
    r"^(KIMI|KIMIX|KOSONG|OPENAI|ANTHROPIC|GEMINI|GOOGLE|MOONSHOT|DEEPSEEK"
    r"|OPENROUTER|ZAI|GLM|QWEN|DASHSCOPE|STEPFUN|MINIMAX|XAI|GROQ|MISTRAL"
    r"|VERTEX|AZURE|BEDROCK|AWS_|COPILOT|NVIDIA|NOVITA|FIREWORKS|UPSTAGE"
    r"|HUGGINGFACE|OLLAMA|TOGETHER|FIREBASE|BTW)"
    r"|(_PROXY$|_CA_BUNDLE$|_KEY$|_TOKEN$|_SECRET$|BASE_URL$|ENDPOINT$)",
    re.IGNORECASE,
)

#: Process start reference for "how long has this session been running".
_PROCESS_START_MONOTONIC = time.monotonic()


# ═════════════════════════════════════════════════════════════════════════════
# Public entry points
# ═════════════════════════════════════════════════════════════════════════════


def error_log_dir(work_dir: str | os.PathLike[str] | KaosPath | None = None) -> Path:
    """Resolve the error-log directory for *work_dir*.

    ``$KIMIX_ERROR_LOG_DIR`` wins when set; otherwise
    ``<work dir>/.kimix_cache/error_log``; finally the process CWD's
    ``.kimix_cache/error_log`` when no work directory is known. The directory
    is **not** created here — :func:`record_session_error` creates it on write.
    """
    if override := os.getenv(DIR_ENV_VAR):
        return Path(override).expanduser()
    if work_dir is not None:
        return Path(str(_canonical(work_dir))) / KIMIX_CACHE_DIR_NAME / ERROR_LOG_DIR_NAME
    return Path.cwd() / KIMIX_CACHE_DIR_NAME / ERROR_LOG_DIR_NAME


def record_session_error(
    *,
    phase: str,
    error: BaseException | None = None,
    reason: str | None = None,
    soul: KimiSoul | Any | None = None,
    session: CliSession | Any | None = None,
    restart_attempt: int | None = None,
    max_restarts: int | None = None,
    restartable: bool | None = None,
    secondary_errors: Mapping[str, BaseException | None] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Path | None:
    """Write one comprehensive session-state snapshot. Never raises.

    Args:
        phase: Which failure point produced the snapshot (see ``PHASE_*``).
        error: The exception being handled (``SessionRestartRequired`` or the
            original chat-provider error).
        reason: Human-readable one-liner (typically the restart reason string).
        soul: The live ``KimiSoul`` (or a test double) for full state capture.
        session: Explicit session; derived from *soul* when omitted.
        restart_attempt / max_restarts: Restart bookkeeping, when known.
        restartable: Whether the error is considered restartable.
        secondary_errors: Related exceptions captured beside *error* (e.g. a
            compaction failure), recorded under ``error.related``.
        extra: Phase-specific structured details (JSON-serializable).

    Returns:
        The snapshot path, or ``None`` when disabled or writing failed.
    """
    if not _enabled():
        return None
    try:
        collector = _Collector()
        work_dir = collector.collect(
            "work_dir", lambda: _resolve_work_dir(soul, session)
        )
        snapshot = _build_snapshot(
            collector,
            phase=phase,
            reason=reason,
            error=error,
            soul=soul,
            session=session,
            work_dir=work_dir,
            restart_attempt=restart_attempt,
            max_restarts=max_restarts,
            restartable=restartable,
            secondary_errors=secondary_errors,
            extra=extra,
        )
        return _write_snapshot(snapshot, work_dir=work_dir)
    except Exception:
        logger.opt(exception=True).warning(
            "Failed to record {phase} error-log snapshot", phase=phase
        )
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Snapshot assembly
# ═════════════════════════════════════════════════════════════════════════════


class _Collector:
    """Best-effort section collector: a failing section never fails the snapshot."""

    __slots__ = ("warnings",)

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def collect(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            self.warnings.append(f"{name}: {type(exc).__name__}: {exc}")
            return None


def _build_snapshot(
    collector: _Collector,
    *,
    phase: str,
    reason: str | None,
    error: BaseException | None,
    soul: Any | None,
    session: Any | None,
    work_dir: Path | None,
    restart_attempt: int | None,
    max_restarts: int | None,
    restartable: bool | None,
    secondary_errors: Mapping[str, BaseException | None] | None,
    extra: Mapping[str, Any] | None,
) -> dict[str, Any]:
    now = pendulum.now("UTC")
    runtime = _soul_runtime(soul)
    if session is None:
        session = _runtime_session(runtime)

    snapshot: dict[str, Any] = {
        "kind": KIND,
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "reason": reason,
        "recorded_at_utc": now.isoformat(),
        "recorded_at_epoch": now.timestamp(),
        "recorded_at_local": _local_time(now),
        "work_dir": str(_canonical(work_dir)) if work_dir is not None else None,
    }
    snapshot["error"] = collector.collect("error", lambda: _error_section(error, secondary_errors))
    snapshot["restart"] = collector.collect(
        "restart",
        lambda: _restart_section(
            soul, restart_attempt, max_restarts, restartable, error
        ),
    )
    snapshot["session"] = collector.collect("session", lambda: _session_section(session))
    snapshot["llm"] = collector.collect("llm", lambda: _llm_section(runtime))
    snapshot["soul"] = collector.collect("soul", lambda: _soul_section(soul))
    snapshot["config"] = collector.collect("config", lambda: _config_section(runtime))
    snapshot["loop_control"] = collector.collect(
        "loop_control", lambda: _loop_control_section(runtime)
    )
    snapshot["context"] = collector.collect("context", lambda: _context_section(soul))
    snapshot["tools"] = collector.collect("tools", lambda: _tools_section(soul))
    snapshot["wire"] = collector.collect("wire", lambda: _wire_section(session))
    snapshot["log_tail"] = collector.collect("log_tail", _log_tail_section)
    snapshot["process"] = collector.collect("process", _process_section)
    snapshot["versions"] = collector.collect("versions", _versions_section)
    snapshot["environment"] = collector.collect("environment", _environment_section)
    snapshot["threads"] = collector.collect("threads", _threads_section)
    snapshot["asyncio"] = collector.collect("asyncio", _asyncio_section)
    snapshot["current_stack"] = collector.collect("current_stack", _current_stack_section)
    snapshot["extra"] = _json_safe(dict(extra)) if extra else None
    if collector.warnings:
        snapshot["collection_warnings"] = collector.warnings
    return snapshot


def _local_time(now: pendulum.DateTime) -> str | None:
    try:
        return now.in_timezone(pendulum.local_timezone()).isoformat()
    except Exception:
        return None


# ── sections ────────────────────────────────────────────────────────────────


def _error_section(
    error: BaseException | None,
    secondary_errors: Mapping[str, BaseException | None] | None,
) -> dict[str, Any]:
    if error is None:
        related = {
            str(name): _exception_info(exc)
            for name, exc in (secondary_errors or {}).items()
        }
        info: dict[str, Any] = {"type": None, "related": related or None}
        return info
    info = _exception_info(error)
    related = {
        str(name): _exception_info(exc)
        for name, exc in (secondary_errors or {}).items()
        if exc is not None
    }
    if related:
        info["related"] = related
    return info


def _restart_section(
    soul: Any | None,
    restart_attempt: int | None,
    max_restarts: int | None,
    restartable: bool | None,
    error: BaseException | None,
) -> dict[str, Any]:
    loop_max = _loop_control_value(soul, "max_session_restarts")
    return {
        "attempt": restart_attempt,
        "max_restarts": max_restarts if max_restarts is not None else loop_max,
        "config_max_session_restarts": loop_max,
        "restartable": restartable if restartable is not None else _restartable(error),
        "auto_restart_enabled": (loop_max if loop_max is not None else 3) > 0,
    }


def _restartable(error: BaseException | None) -> bool | None:
    """Mirror ``kimi_agent_sdk._session._is_restartable_error`` decisions."""
    if error is None:
        return None
    if isinstance(error, APIStatusError):
        return error.status_code in frozenset({408, 429, 500, 502, 503, 504})
    return None


def _session_section(session: Any | None) -> dict[str, Any] | None:
    if session is None:
        return None
    info: dict[str, Any] = {
        "id": getattr(session, "id", None),
        "work_dir": _path_str(getattr(session, "work_dir", None)),
        "anonymous": getattr(session, "sessions_dir_override", None) is not None,
    }
    for attr in ("title", "updated_at", "context_file"):
        info[attr] = _json_safe(getattr(session, attr, None))
    session_dir = _session_dir(session)
    info["dir"] = _path_str(session_dir)
    wire_file = getattr(session, "wire_file", None)
    wire_path = getattr(wire_file, "path", None)
    info["wire_file"] = _path_str(wire_path)
    if wire_path is not None:
        info["wire_file_size_bytes"] = _file_size(wire_path)
    if session_dir is not None:
        info["dir_entries"] = _dir_entries(session_dir)
    for attr in ("state", "custom_data", "custom_config"):
        info[attr] = _json_safe(getattr(session, attr, None))
    return info


def _soul_section(soul: Any | None) -> dict[str, Any] | None:
    if soul is None:
        return None
    info: dict[str, Any] = {
        "class": type(soul).__name__,
        "name": _safe_call(getattr(soul, "name", None)),
        "model_name": _safe_call(getattr(soul, "model_name", None)),
        "thinking": _safe_call(getattr(soul, "thinking", None)),
        "anonymous": getattr(soul, "_anonymous", None),
        "current_step_no": getattr(soul, "_current_step_no", None),
        "current_turn_id": getattr(soul, "_current_turn_id", None),
        "current_turn_user_text": _truncate_str(
            str(getattr(soul, "_current_turn_user_text", "") or ""), 2_000
        ),
        "run_active": getattr(soul, "_run_active", None),
        "last_tool_calls": _json_safe(getattr(soul, "_last_tool_calls", None)),
    }
    status = getattr(soul, "status", None)
    if callable(status):
        status = _safe_call(status)
    if status is not None:
        info["status"] = _json_safe(status)
    return info


def _llm_section(runtime: Any | None) -> dict[str, Any] | None:
    if runtime is None:
        return None
    llm = getattr(runtime, "llm", None)
    if llm is None:
        return {"set": False}
    info: dict[str, Any] = {"set": True}
    provider = getattr(llm, "chat_provider", None)
    info["chat_provider_class"] = _type_name(provider)
    info["chat_provider_module"] = type(provider).__module__ if provider is not None else None
    for attr in ("name", "model_name", "thinking_effort", "thinking"):
        info[attr] = _json_safe(getattr(provider, attr, None))
    info["max_context_size"] = getattr(llm, "max_context_size", None)
    info["capabilities"] = sorted(
        str(cap) for cap in (getattr(llm, "capabilities", None) or ())
    )
    gen_kwargs = getattr(provider, "_generation_kwargs", None)
    if isinstance(gen_kwargs, Mapping):
        info["generation_kwargs"] = _json_safe(dict(gen_kwargs))
    # Provider configuration (base URL, headers, key presence — never the key).
    for attr in ("base_url", "default_headers", "extra_body", "_extra_body",
                 "default_max_tokens", "max_completion_tokens", "metadata"):
        value = getattr(provider, attr, None)
        if value is not None:
            info[f"provider_{attr}"] = _json_safe(value)
    api_key = getattr(provider, "api_key", None)
    info["provider_api_key_set"] = _secret_is_set(api_key)
    # Config-level provider/model records.
    provider_config = getattr(llm, "provider_config", None)
    if provider_config is not None:
        info["provider_config"] = _json_safe(_model_dump(provider_config))
    model_config = getattr(llm, "model_config", None)
    if model_config is not None:
        info["model_config"] = _json_safe(_model_dump(model_config))
    return info


def _config_section(runtime: Any | None) -> dict[str, Any] | None:
    config = getattr(runtime, "config", None)
    if config is None:
        return None
    return _json_safe(_model_dump(config))


def _loop_control_section(runtime: Any | None) -> dict[str, Any] | None:
    loop_control = getattr(getattr(runtime, "config", None), "loop_control", None)
    if loop_control is None:
        return None
    return _json_safe(_model_dump(loop_control))


def _context_section(soul: Any | None) -> dict[str, Any] | None:
    context = getattr(soul, "context", None) or getattr(soul, "_context", None)
    if context is None:
        return None
    history: Sequence[Any] = getattr(context, "history", None) or []
    system_prompt = getattr(context, "system_prompt", None)
    storage = getattr(context, "storage", None)
    included = list(history)[-_MAX_HISTORY_MESSAGES:] if hasattr(context, "history") else []
    return {
        "class": _type_name(context),
        "message_count": len(history),
        "included_message_count": len(included),
        "history_head_dropped": max(0, len(history) - len(included)),
        "token_count": getattr(context, "token_count", None),
        "token_count_with_pending": getattr(context, "token_count_with_pending", None),
        "n_checkpoints": getattr(context, "n_checkpoints", None),
        "model_name": getattr(context, "model_name", None),
        "storage_class": _type_name(storage),
        "storage_path": _path_str(getattr(storage, "storage_path", None)),
        "file_backend": _path_str(getattr(context, "file_backend", None)),
        "system_prompt_chars": len(system_prompt) if isinstance(system_prompt, str) else None,
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        if isinstance(system_prompt, str)
        else None,
        "system_prompt": _truncate_str(system_prompt, _MAX_SYSTEM_PROMPT_CHARS)
        if isinstance(system_prompt, str)
        else None,
        "messages": [_serialize_message(msg) for msg in included],
    }


def _tools_section(soul: Any | None) -> dict[str, Any] | None:
    agent = getattr(soul, "agent", None)
    toolset = getattr(agent, "toolset", None)
    if toolset is None:
        return None
    tools = getattr(toolset, "tools", None) or []
    names: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        names.append(str(name) if name is not None else _type_name(tool))
    info: dict[str, Any] = {
        "toolset_class": _type_name(toolset),
        "count": len(names),
        "names": sorted(names),
    }
    mcp = getattr(toolset, "mcp_status_snapshot", None)
    if callable(mcp):
        info["mcp_status"] = _json_safe(_safe_call(mcp))
    return info


def _wire_section(session: Any | None) -> dict[str, Any] | None:
    if session is None:
        return None
    wire_file = getattr(session, "wire_file", None)
    wire_path = getattr(wire_file, "path", None)
    if wire_path is None:
        return None
    size = _file_size(wire_path)
    info: dict[str, Any] = {
        "path": _path_str(wire_path),
        "size_bytes": size,
        "version": getattr(wire_file, "version", None),
        "is_empty": _safe_call(getattr(wire_file, "is_empty", None)),
    }
    tail = _read_tail_lines(wire_path, _MAX_WIRE_TAIL_BYTES)
    if tail is not None:
        text_lines, truncated_bytes = tail
        info["tail_truncated_bytes"] = truncated_bytes
        records: list[Any] = []
        for line in text_lines[-_MAX_WIRE_TAIL_RECORDS:]:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(_json_safe(orjson.loads(line)))
            except orjson.JSONDecodeError:
                records.append({"unparsed": _truncate_str(line, 4_000)})
        info["tail_records"] = records
        info["tail_records_total_lines"] = len(text_lines)
    return info


def _log_tail_section() -> dict[str, Any] | None:
    from kimi_cli.share import get_share_dir

    log_path = get_share_dir() / "logs" / "kimi.log"
    if not log_path.exists():
        return None
    tail = _read_tail_lines(log_path, _MAX_LOG_TAIL_BYTES)
    if tail is None:
        return None
    text_lines, truncated_bytes = tail
    return {
        "path": str(log_path),
        "size_bytes": _file_size(log_path),
        "tail_truncated_bytes": truncated_bytes,
        "tail_lines": [_truncate_str(line, 4_000) for line in text_lines[-_MAX_LOG_TAIL_LINES:]],
        "tail_lines_total": len(text_lines),
    }


def _process_section() -> dict[str, Any]:
    return {
        "pid": os.getpid(),
        "ppid": os.getppid() if hasattr(os, "getppid") else None,
        "cwd": os.getcwd(),
        "argv": [str(arg) for arg in sys.argv],
        "executable": sys.executable,
        "python": sys.version.replace("\n", " "),
        "python_implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
        "in_process_seconds": round(time.monotonic() - _PROCESS_START_MONOTONIC, 3),
        "thread_count": threading.active_count(),
    }


def _versions_section() -> dict[str, Any]:
    from importlib.metadata import PackageNotFoundError, version

    versions: dict[str, Any] = {"python": platform.python_version()}
    for package in (
        "kimi-cli-x",
        "kimix",
        "kimi-agent-sdk",
        "kosong",
        "kaos",
        "pydantic",
        "orjson",
        "tenacity",
        "pendulum",
    ):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    return versions


def _environment_section() -> dict[str, Any]:
    filtered: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not _ENV_KEY_RE.search(key):
            continue
        if _mask_key(key):
            filtered[key] = {"masked": True, "set": bool(value), "length": len(value)}
        else:
            filtered[key] = _truncate_str(value, 2_000)
    return filtered


def _threads_section() -> list[dict[str, Any]]:
    return [
        {
            "name": thread.name,
            "ident": thread.ident,
            "daemon": thread.daemon,
            "alive": thread.is_alive(),
        }
        for thread in threading.enumerate()
    ]


def _asyncio_section() -> dict[str, Any]:
    import asyncio

    info: dict[str, Any] = {}
    try:
        loop = asyncio.get_running_loop()
        info["running_loop"] = True
        info["loop_debug"] = loop.get_debug()
    except RuntimeError:
        info["running_loop"] = False
        return info
    try:
        tasks = list(asyncio.all_tasks(loop))
    except Exception:
        return info
    info["task_count"] = len(tasks)
    entries: list[dict[str, Any]] = []
    for task in tasks[:_MAX_TASKS]:
        entry: dict[str, Any] = {
            "name": task.get_name(),
            "done": task.done(),
            "cancelled": task.cancelled(),
        }
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                entry["exception"] = f"{type(exc).__name__}: {exc}"
        try:
            entry["stack"] = _format_frames(task.get_stack(limit=8))
        except Exception as exc:
            entry["stack_error"] = f"{type(exc).__name__}: {exc}"
        entries.append(entry)
    info["tasks"] = entries
    return info


def _current_stack_section() -> list[str]:
    return traceback.format_stack()[-_MAX_FRAMES:]


# ═════════════════════════════════════════════════════════════════════════════
# Exception serialization
# ═════════════════════════════════════════════════════════════════════════════


def _exception_info(exc: BaseException | None, *, depth: int = 0) -> dict[str, Any] | None:
    """Full, self-contained description of one exception (and its chain)."""
    if exc is None:
        return None
    info: dict[str, Any] = {
        "type": _type_name(exc),
        "module": type(exc).__module__,
        "message": _truncate_str(str(exc), _MAX_STRING_CHARS),
        "repr": _truncate_str(repr(exc), _MAX_REPR_CHARS),
        "args": [_truncate_str(str(arg), 1_000) for arg in exc.args],
    }
    if isinstance(exc, ChatProviderError):
        info["chat_provider_error"] = True
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        # Connection-class failures are the trigger for the auto-restart path.
        info["connection_error"] = True
    if isinstance(exc, APIEmptyResponseError):
        info["empty_response_error"] = True
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        info["status_code"] = status_code
    for attr in ("request_id", "retry_after"):
        value = getattr(exc, attr, None)
        if value is not None:
            info[attr] = _json_safe(value)
    headers = getattr(exc, "headers", None)
    if headers is not None:
        info["headers"] = _json_safe(dict(headers))
    body = getattr(exc, "body", None)
    if body is not None:
        info["body"] = _json_safe(body)
    if getattr(exc, "_kimi_recovery_exhausted", False):
        info["connection_recovery_exhausted"] = True
    original = getattr(exc, "original_error", None)
    if original is not None and original is not exc and depth < 6:
        info["original_error"] = _exception_info(original, depth=depth + 1)
    if exc.__traceback__ is not None:
        info["traceback"] = _truncate_str(
            "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
            _MAX_TRACEBACK_CHARS,
        )
    if depth < 4:
        if exc.__cause__ is not None:
            info["cause"] = _exception_info(exc.__cause__, depth=depth + 1)
        if exc.__context__ is not None and exc.__context__ is not exc.__cause__:
            info["context"] = _exception_info(exc.__context__, depth=depth + 1)
    classification = _classify_error(exc)
    if classification:
        info["classification"] = classification
    return info


def _classify_error(exc: BaseException) -> str | None:
    """Reuse the soul's error classification so snapshots and logs agree."""
    if not isinstance(exc, Exception):
        return None
    try:
        from kimi_cli.soul.kimisoul import classify_api_error

        error_type, status_code = classify_api_error(exc)
        if status_code is not None:
            return f"{error_type}:{status_code}"
        return error_type
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Generic JSON-safe conversion
# ═════════════════════════════════════════════════════════════════════════════


def _json_safe(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Convert *value* into something :mod:`orjson` can serialize.

    Pydantic models, dataclasses, enums, paths, sets, exceptions and other
    exotic values are converted recursively; secret-looking keys are masked;
    long strings are truncated. Never raises.
    """
    try:
        return _json_safe_inner(value, key=key, depth=depth)
    except Exception:
        return _truncate_str(_safe_repr(value), _MAX_REPR_CHARS)


def _json_safe_inner(value: Any, *, key: str, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return _truncate_str(_safe_repr(value), 256)
    if _mask_key(key):
        # Secret-carrying keys are masked regardless of the value type so the
        # raw credential (e.g. an ``api_key`` string serialized by a pydantic
        # field_serializer) can never reach the snapshot.
        return _mask_value(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str):
        return _truncate_str(value, _MAX_STRING_CHARS)
    if isinstance(value, SecretStr):
        return _mask_value(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, KaosPath):
        return str(_canonical(value))
    if isinstance(value, enum.Enum):
        return _json_safe_inner(value.value, key=key, depth=depth + 1)
    if isinstance(value, BaseModel):
        return _json_safe_inner(_model_dump(value), key=key, depth=depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            str(field.name): _json_safe_inner(
                getattr(value, field.name, None), key=field.name, depth=depth + 1
            )
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {
            _json_safe_inner(k, key="", depth=depth + 1)
            if isinstance(k, str)
            else str(k): _json_safe_inner(v, key=str(k), depth=depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe_inner(item, key=key, depth=depth + 1) for item in value]
    if isinstance(value, set | frozenset):
        return sorted(str(_json_safe_inner(item, key=key, depth=depth + 1)) for item in value)
    if isinstance(value, bytes | bytearray):
        return {"$bytes": len(value), "head_hex": bytes(value[:64]).hex()}
    if callable(value):
        return f"<callable {_type_name(value)}>"
    if isinstance(value, BaseException):
        return _truncate_str(f"{type(value).__name__}: {value}", _MAX_REPR_CHARS)
    return _truncate_str(_safe_repr(value), _MAX_REPR_CHARS)


def _mask_key(key: str) -> bool:
    """Whether *key* names a secret (counter-like names are excluded)."""
    if not key:
        return False
    if _NOT_SECRET_KEY_RE.search(key):
        return False
    return bool(_SECRET_KEY_RE.search(key))


def _mask_value(value: Any) -> Any:
    secret = getattr(value, "get_secret_value", None)
    raw = secret() if callable(secret) else value
    return {
        "masked": True,
        "set": bool(raw),
        "length": len(raw) if isinstance(raw, str | bytes | bytearray) else None,
    }


def _model_dump(model: BaseModel) -> Any:
    """``model_dump(mode="json")`` with a permissive fallback."""
    try:
        return model.model_dump(mode="json", warnings=False)
    except Exception:
        return model.model_dump(mode="python", warnings=False)


def _serialize_message(message: Any) -> Any:
    """One conversation message, JSON-safe with per-part truncation."""
    try:
        dumped = message.model_dump(mode="json", warnings=False)
    except Exception:
        return _truncate_str(_safe_repr(message), _MAX_REPR_CHARS)
    return _truncate_message_strings(dumped, depth=0)


def _truncate_message_strings(value: Any, *, depth: int) -> Any:
    limit = _MAX_MESSAGE_PART_CHARS
    if isinstance(value, str):
        return _truncate_str(value, limit)
    if isinstance(value, list):
        return [_truncate_message_strings(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        return {
            key: _truncate_message_strings(item, depth=depth + 1)
            for key, item in value.items()
        }
    return value


# ═════════════════════════════════════════════════════════════════════════════
# Small helpers
# ═════════════════════════════════════════════════════════════════════════════


def _enabled() -> bool:
    return os.getenv(ENABLE_ENV_VAR, "1").strip().lower() in _TRUTHY


def _secret_is_set(value: Any) -> bool:
    """Whether *value* carries a non-empty secret — without exposing it."""
    if value is None:
        return False
    getter = getattr(value, "get_secret_value", None)
    try:
        raw = getter() if callable(getter) else value
    except Exception:
        return True
    return bool(raw)


def _canonical(value: Any) -> Any:
    canonical = getattr(value, "canonical", None)
    if callable(canonical):
        try:
            return canonical()
        except Exception:
            return value
    return value


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<unrepr-able {type(value).__name__}>"


def _safe_call(value: Any) -> Any:
    try:
        return value() if callable(value) else value
    except Exception as exc:
        return f"<error: {type(exc).__name__}: {exc}>"


def _type_name(value: Any) -> str | None:
    return type(value).__name__ if value is not None else None


def _truncate_str(value: Any, limit: int) -> Any:
    if not isinstance(value, str):
        return value
    if len(value) <= limit:
        return value
    return f"{value[:limit]}...[truncated {len(value) - limit} chars]"


def _path_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(_canonical(value))


def _file_size(path: Any) -> int | None:
    try:
        return Path(str(_canonical(path))).stat().st_size
    except OSError:
        return None


def _dir_entries(directory: Path) -> list[dict[str, Any]] | None:
    try:
        entries = []
        for entry in sorted(directory.iterdir(), key=lambda item: item.name)[:200]:
            try:
                stat = entry.stat()
                entries.append(
                    {
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size_bytes": stat.st_size if entry.is_file() else None,
                        "mtime_epoch": stat.st_mtime,
                    }
                )
            except OSError:
                entries.append({"name": entry.name, "type": "unknown"})
        return entries
    except OSError:
        return None


def _soul_runtime(soul: Any | None) -> Any | None:
    if soul is None:
        return None
    for attr in ("runtime", "_runtime"):
        try:
            runtime = getattr(soul, attr, None)
        except Exception:
            continue
        if runtime is not None:
            return runtime
    return None


def _runtime_session(runtime: Any | None) -> Any | None:
    if runtime is None:
        return None
    try:
        return getattr(runtime, "session", None)
    except Exception:
        return None


def _resolve_work_dir(soul: Any | None, session: Any | None) -> Path | None:
    if session is None:
        session = _runtime_session(_soul_runtime(soul))
    if session is None:
        return None
    try:
        work_dir = getattr(session, "work_dir", None)
    except Exception:
        return None
    if work_dir is None:
        return None
    try:
        return Path(str(_canonical(work_dir)))
    except Exception:
        return None


def _session_dir(session: Any | None) -> Path | None:
    """Session directory without triggering ``Session.dir``'s mkdir side effect."""
    if session is None:
        return None
    session_id = getattr(session, "id", None)
    root = getattr(session, "_sessions_root", None)
    if root is not None and session_id:
        return Path(str(_canonical(root))) / str(session_id)
    session_dir = getattr(session, "dir", None)
    return Path(str(_canonical(session_dir))) if session_dir is not None else None


def _loop_control_value(soul: Any | None, attr: str) -> int | None:
    runtime = _soul_runtime(soul)
    loop_control = getattr(getattr(runtime, "config", None), "loop_control", None)
    value = getattr(loop_control, attr, None)
    return value if isinstance(value, int) else None


def _read_tail_lines(path: Any, max_bytes: int) -> tuple[list[str], int] | None:
    """Read the last *max_bytes* of a text file, decoded leniently."""
    try:
        file_path = Path(str(_canonical(path)))
        size = file_path.stat().st_size
        with file_path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
                data = handle.read()
                # Drop the (likely partial) first line.
                _, _, data = data.partition(b"\n")
                truncated = size - max_bytes
            else:
                data = handle.read()
                truncated = 0
        text = data.decode("utf-8", errors="replace")
        return text.splitlines(), truncated
    except OSError:
        return None


def _format_frames(frames: Sequence[Any] | None) -> list[str]:
    formatted: list[str] = []
    for frame in frames or []:
        formatted.append(
            f'  File "{frame.f_code.co_filename}", line {frame.f_code.co_lineno}, '
            f"in {frame.f_code.co_name}"
        )
        if len(formatted) >= _MAX_FRAMES:
            break
    return formatted


# ═════════════════════════════════════════════════════════════════════════════
# Persistence
# ═════════════════════════════════════════════════════════════════════════════


def _write_snapshot(snapshot: dict[str, Any], *, work_dir: Path | None) -> Path | None:
    directory = error_log_dir(work_dir)
    directory.mkdir(parents=True, exist_ok=True)
    now = pendulum.now("UTC")
    session_info = snapshot.get("session") or {}
    filename = _snapshot_filename(
        now,
        session_id=str(session_info.get("id") or ""),
        phase=str(snapshot.get("phase") or "unknown"),
        error_type=str((snapshot.get("error") or {}).get("type") or "unknown"),
    )
    path = directory / filename
    try:
        payload = orjson.dumps(snapshot, option=orjson.OPT_INDENT_2)
    except TypeError:
        # Belt-and-braces: a stray non-serializable value must not lose the
        # whole snapshot — re-sanitize everything and retry.
        payload = orjson.dumps(_json_safe(snapshot), option=orjson.OPT_INDENT_2)
    path.write_bytes(payload)
    _write_latest_pointer(directory, snapshot, filename)
    _prune_snapshots(directory, _max_snapshots())
    logger.warning(
        "Recorded {phase} debug snapshot: {path} ({size} bytes)",
        phase=snapshot.get("phase"),
        path=path,
        size=len(payload),
    )
    return path


def _write_latest_pointer(directory: Path, snapshot: dict[str, Any], filename: str) -> None:
    error = snapshot.get("error") or {}
    pointer = {
        "latest": filename,
        "recorded_at_utc": snapshot.get("recorded_at_utc"),
        "phase": snapshot.get("phase"),
        "error_type": error.get("type"),
        "error_message": _truncate_str(str(error.get("message") or ""), 500),
        "session_id": (snapshot.get("session") or {}).get("id"),
        "work_dir": snapshot.get("work_dir"),
    }
    (directory / LATEST_POINTER_NAME).write_bytes(orjson.dumps(pointer, option=orjson.OPT_INDENT_2))


def _max_snapshots() -> int:
    raw = os.getenv(MAX_FILES_ENV_VAR)
    if raw is None:
        return DEFAULT_MAX_SNAPSHOTS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_SNAPSHOTS
    return value if value > 0 else DEFAULT_MAX_SNAPSHOTS


def _prune_snapshots(directory: Path, max_files: int) -> None:
    """Keep only the newest *max_files* snapshots (``.latest.json`` excluded)."""
    if max_files <= 0:
        return
    try:
        files = [
            path
            for path in directory.glob("*.json")
            if path.name != LATEST_POINTER_NAME and path.is_file()
        ]
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for stale in files[max_files:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")
    return slug[:48]


def _snapshot_filename(
    now: pendulum.DateTime, *, session_id: str, phase: str, error_type: str
) -> str:
    stamp = f"{now.format('YYYYMMDD_HHmmss')}_{now.microsecond // 1000:03d}"
    parts = [
        stamp,
        _slug(phase) or "unknown",
        _slug(error_type) or "unknown",
        _slug(session_id)[:24] or "nosession",
        uuid.uuid4().hex[:6],
    ]
    return "_".join(parts) + ".json"
