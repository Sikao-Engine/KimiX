"""Read-only iteration over the tool invocations recorded in a session context.

A *tool invocation* is one ``assistant`` tool call (a ``kosong`` ``ToolCall``:
tool name + JSON arguments + ``tool_call_id``) paired with the ``tool`` role
message that answered it (matched by ``tool_call_id``).  Both halves already live
in the conversation history, but nothing exposed them as a single record, so
commands such as ``/reflection_bash`` had no way to ask "what did the agent run
and what came back?".

This module owns that pairing and nothing else:

* :func:`session_history` — best-effort read of ``session._cli.soul.context.history``
  (never raises: the context may be empty, closed, or a test double).
* :func:`iter_tool_invocations` / :func:`collect_tool_invocations` — yield
  :class:`ToolInvocation` records in conversation order.  Arguments are parsed
  from the JSON string (relaxed for the LLM's sloppy JSON), the tool result is
  split into the tool's ``message`` (the ``<system>…</system>`` metadata, e.g.
  ``ERROR: failed ...`` / ``success``) and its ``output`` (the real payload), and
  a failed call is flagged with :attr:`ToolInvocation.is_error`.
* :func:`tool_call_error_dicts` — the plain-``dict`` view used by
  :func:`kimix.utils.session.get_tool_call_errors`.

Everything is duck-typed on purpose: the same functions work over real
``kosong.message.Message`` objects, over ``SimpleNamespace`` test doubles, and
over dict-shaped history restored from an older export.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import orjson
import regex as re

#: ``<system>…</system>`` wrapper added by ``kimi_cli.soul.message.system()``.
#: Tool results put their metadata (and the ``ERROR: …`` failure marker) there;
#: a successful result can carry several such parts after compaction merges
#: adjacent metadata lines.
_SYSTEM_RE = re.compile(r'^<system>(?P<body>.*)</system>$', re.DOTALL)

#: Prefix the soul writes in front of a failed tool's message
#: (``kimi_cli.soul.message.tool_result_to_message``).
_ERROR_PREFIX = 'ERROR:'

#: :attr:`ToolInvocation.status` values.
STATUS_OK = 'ok'
STATUS_FAILED = 'failed'
STATUS_NO_RESULT = 'no_result'

#: Attribute paths tried, in order, to reach the conversation history.
_HISTORY_PATHS: tuple[tuple[str, ...], ...] = (
    ('_cli', 'soul', 'context', 'history'),
    ('_cli', 'context', 'history'),
    ('context', 'history'),
)

#: Argument keys that carry "the thing the tool acted on"; the first one present
#: is exposed as :attr:`ToolInvocation.command` (shell tools use ``cmd``).
_COMMAND_KEYS: tuple[str, ...] = ('cmd', 'command', 'script', 'shell_cmd')


@dataclass(slots=True)
class ToolInvocation:
    """One assistant tool call paired with the tool result that answered it."""

    index: int
    """Ordinal among the tool calls yielded by the iterator (0-based)."""

    tool_name: str
    """Name of the called tool, e.g. ``bash``."""

    call_id: str
    """The ``tool_call_id`` linking the call to its result (``""`` if absent)."""

    arguments_raw: str | None = None
    """The raw JSON argument string exactly as the model produced it."""

    arguments: dict[str, Any] | None = None
    """Parsed arguments, or ``None`` when the payload was missing/unparsable."""

    arguments_error: str | None = None
    """Why the arguments could not be parsed (``None`` when they parsed fine)."""

    message: str | None = None
    """The tool's metadata message (``ERROR: …`` stripped of its prefix)."""

    output: str | None = None
    """The tool's payload text (everything outside ``<system>…</system>``)."""

    is_error: bool = False
    """``True`` when the result message carries the soul's ``ERROR:`` marker."""

    has_result: bool = False
    """``False`` when no ``tool`` message answered this call (aborted/pruned)."""

    @property
    def status(self) -> str:
        """One of :data:`STATUS_OK`, :data:`STATUS_FAILED`, :data:`STATUS_NO_RESULT`."""
        if not self.has_result:
            return STATUS_NO_RESULT
        return STATUS_FAILED if self.is_error else STATUS_OK

    @property
    def command(self) -> str | None:
        """The shell command (or equivalent target) this call executed."""
        if self.arguments:
            for key in _COMMAND_KEYS:
                value = self.arguments.get(key)
                if isinstance(value, str):
                    return value
        return None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable view (no truncation — callers decide)."""
        data: dict[str, Any] = {
            'index': self.index,
            'status': self.status,
            'tool': self.tool_name,
            'call_id': self.call_id,
            'command': self.command,
            'arguments': self.arguments,
            'message': self.message,
            'output': self.output,
        }
        if self.arguments_error is not None:
            data['arguments_raw'] = self.arguments_raw
            data['arguments_error'] = self.arguments_error
        return data


# ---------------------------------------------------------------------------
# Context access
# ---------------------------------------------------------------------------


def session_history(session: Any) -> list[Any]:
    """Return the conversation history of *session* (empty on any problem).

    Accepts the SDK session (``session._cli.soul.context.history``), a bare soul
    context, or anything exposing a ``history()`` accessor.  Never raises — a
    closed session, an un-restored context and a ``SimpleNamespace`` test double
    all degrade to an empty list.
    """
    if session is None:
        return []
    accessor = getattr(session, 'history', None)
    if callable(accessor):
        try:
            history = accessor()
        except Exception:
            history = None
        if history is not None:
            return list(history)
    for path in _HISTORY_PATHS:
        node: Any = session
        for name in path:
            node = getattr(node, name, None)
            if node is None:
                break
        else:
            try:
                return list(node)
            except TypeError:
                continue
    return []


# ---------------------------------------------------------------------------
# Part / message decoding
# ---------------------------------------------------------------------------


def _part_text(part: Any) -> str | None:
    """Return the text of one content part (``None`` when it has none)."""
    if isinstance(part, str):
        return part
    if isinstance(part, dict):
        text = part.get('text')
        if isinstance(text, str):
            return text
        return _placeholder(part.get('type'))
    text = getattr(part, 'text', None)
    if isinstance(text, str):
        return text
    return _placeholder(getattr(part, 'type', None))


def _placeholder(part_type: Any) -> str | None:
    """``"[image_url]"``-style stand-in for a non-text content part."""
    if isinstance(part_type, str) and part_type and part_type != 'text':
        return f'[{part_type}]'
    return None


def _content_parts(message: Any) -> list[Any]:
    """Return the content parts of *message* as a list (never ``None``)."""
    if isinstance(message, dict):
        content = message.get('content')
    else:
        content = getattr(message, 'content', None)
    if content is None:
        return []
    if isinstance(content, (str, bytes)):
        return [content.decode('utf-8', 'replace') if isinstance(content, bytes) else content]
    if isinstance(content, list):
        return list(content)
    return [content]


def _system_body(text: str) -> str | None:
    """Return the inner text of a ``<system>…</system>`` part, else ``None``."""
    match = _SYSTEM_RE.match(text.strip())
    return match.group('body') if match is not None else None


def split_tool_result(message: Any) -> tuple[str | None, str | None, bool]:
    """Split a ``tool`` role message into ``(metadata, output, is_error)``.

    ``metadata`` concatenates every ``<system>…</system>`` part (the soul puts the
    tool's ``message`` there, and ``ERROR: …`` for failures); ``output`` is every
    other part verbatim.  ``is_error`` is ``True`` when a metadata part starts
    with the ``ERROR:`` marker.
    """
    metadata: list[str] = []
    output: list[str] = []
    is_error = False
    for part in _content_parts(message):
        text = _part_text(part)
        if text is None:
            continue
        body = _system_body(text)
        if body is None:
            output.append(text)
            continue
        body = body.strip()
        if body.startswith(_ERROR_PREFIX):
            is_error = True
            body = body[len(_ERROR_PREFIX):].lstrip(' :')
        if body:
            metadata.append(body)
    return ('\n'.join(metadata) or None, '\n'.join(output) or None, is_error)


# ---------------------------------------------------------------------------
# Tool-call decoding
# ---------------------------------------------------------------------------


def _parse_arguments(raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a tool call's JSON argument string into ``(dict, error)``."""
    if raw is None:
        return None, None
    if not raw.strip():
        return {}, None
    try:
        parsed = orjson.loads(raw)
    except Exception as exc:  # noqa: BLE001 - any decoder failure is "unparsable"
        first_error = f'{type(exc).__name__}: {exc}'
        parsed = _parse_relaxed(raw)
        if parsed is None:
            return None, first_error
    if isinstance(parsed, dict):
        return parsed, None
    return None, f'arguments are not a JSON object ({type(parsed).__name__})'


def _parse_relaxed(raw: str) -> Any:
    """Second-chance parse through ``kosong``'s lenient reader (LLM JSON)."""
    try:
        from kosong.utils.jsonx import loads_relaxed
    except ImportError:  # pragma: no cover - kosong is always present in-tree
        return None
    try:
        return loads_relaxed(raw)
    except Exception:  # noqa: BLE001 - genuinely malformed payload
        return None


def _call_function(call: Any) -> tuple[str, str | None]:
    """Return ``(tool_name, raw_arguments)`` from a ``ToolCall``-shaped object."""
    function = call.get('function') if isinstance(call, dict) else getattr(call, 'function', None)
    if isinstance(function, dict):
        name = function.get('name')
        arguments = function.get('arguments')
    else:
        name = getattr(function, 'name', None) if function is not None else None
        arguments = getattr(function, 'arguments', None) if function is not None else None
    return (name if isinstance(name, str) else '', arguments if isinstance(arguments, str) else None)


def _call_id(call: Any) -> str:
    call_id = call.get('id') if isinstance(call, dict) else getattr(call, 'id', None)
    return call_id if isinstance(call_id, str) else ''


def _role(message: Any) -> str | None:
    role = message.get('role') if isinstance(message, dict) else getattr(message, 'role', None)
    return role if isinstance(role, str) else None


def _tool_calls(message: Any) -> list[Any]:
    calls = (
        message.get('tool_calls')
        if isinstance(message, dict)
        else getattr(message, 'tool_calls', None)
    )
    return list(calls) if calls else []


def _tool_call_id_of(message: Any) -> str | None:
    call_id = (
        message.get('tool_call_id')
        if isinstance(message, dict)
        else getattr(message, 'tool_call_id', None)
    )
    return call_id if isinstance(call_id, str) else None


# ---------------------------------------------------------------------------
# Public iteration
# ---------------------------------------------------------------------------


def iter_tool_invocations(
    session: Any = None,
    *,
    tool_names: Iterable[str] | None = None,
    failed_only: bool = False,
    history: Sequence[Any] | None = None,
) -> Iterator[ToolInvocation]:
    """Yield every tool invocation of *session* in conversation order.

    Args:
        session: The SDK session (or anything :func:`session_history` understands).
        tool_names: Case-insensitive tool-name filter (``None`` = every tool).
        failed_only: Yield only calls whose result carried the ``ERROR:`` marker.
        history: Explicit history overriding *session* (used by tests/callers
            that already hold the message list).

    Results are paired with their call by ``tool_call_id``; when a call was never
    answered (aborted, cleared, or pruned away) the invocation is still yielded
    with ``has_result=False`` so nothing silently disappears.
    """
    messages = list(history) if history is not None else session_history(session)
    if not messages:
        return
    wanted = None if tool_names is None else {name.lower() for name in tool_names}

    results: dict[str, Any] = {}
    for message in messages:
        if _role(message) != 'tool':
            continue
        call_id = _tool_call_id_of(message)
        if call_id:
            # Last one wins: a retried/re-sent call keeps its final answer.
            results[call_id] = message

    index = 0
    for message in messages:
        if _role(message) != 'assistant':
            continue
        for call in _tool_calls(message):
            tool_name, raw_arguments = _call_function(call)
            if wanted is not None and tool_name.lower() not in wanted:
                continue
            arguments, arguments_error = _parse_arguments(raw_arguments)
            call_id = _call_id(call)
            result = results.get(call_id) if call_id else None
            message_text, output_text, is_error = split_tool_result(result) if result is not None else (None, None, False)
            invocation = ToolInvocation(
                index=index,
                tool_name=tool_name,
                call_id=call_id,
                arguments_raw=raw_arguments,
                arguments=arguments,
                arguments_error=arguments_error,
                message=message_text,
                output=output_text,
                is_error=is_error,
                has_result=result is not None,
            )
            index += 1
            if failed_only and not invocation.is_error:
                continue
            yield invocation


def collect_tool_invocations(
    session: Any = None,
    *,
    tool_names: Iterable[str] | None = None,
    failed_only: bool = False,
    history: Sequence[Any] | None = None,
) -> list[ToolInvocation]:
    """List form of :func:`iter_tool_invocations`."""
    return list(
        iter_tool_invocations(
            session,
            tool_names=tool_names,
            failed_only=failed_only,
            history=history,
        )
    )


def tool_call_error_dicts(session: Any = None) -> list[dict[str, Any]]:
    """Return every failed tool call of *session* as plain dictionaries."""
    return [invocation.to_dict() for invocation in iter_tool_invocations(session, failed_only=True)]


__all__ = [
    'STATUS_FAILED',
    'STATUS_NO_RESULT',
    'STATUS_OK',
    'ToolInvocation',
    'collect_tool_invocations',
    'iter_tool_invocations',
    'session_history',
    'split_tool_result',
    'tool_call_error_dicts',
]
