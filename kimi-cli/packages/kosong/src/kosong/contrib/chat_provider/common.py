from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Literal

from kosong.chat_provider import StreamedMessagePart

if TYPE_CHECKING:
    from kosong.message import ToolCall

type ToolMessageConversion = Literal["extract_text"]


def validate_tool_call_arguments(tool_calls: list[ToolCall]) -> list[str]:
    """Validate and sanitize tool call arguments. Returns error messages.

    Each tool call with non-empty arguments is parsed as JSON.  Invalid
    JSON and non-object values are both reset to ``"{}"`` and reported
    via the returned error list.
    """
    from kosong.utils.jsonx import loads_relaxed

    errors: list[str] = []
    for tc in tool_calls:
        if not tc.function.arguments:
            continue
        try:
            parsed = loads_relaxed(tc.function.arguments)
        except json.JSONDecodeError as exc:
            errors.append(
                f"Error: Tool call '{tc.function.name}' has invalid JSON arguments: {exc}"
            )
            tc.function.arguments = "{}"
            continue
        if not isinstance(parsed, dict):
            errors.append(
                f"Error: Tool call '{tc.function.name}' arguments must be a JSON object, "
                f"got {type(parsed).__name__}."
            )
            tc.function.arguments = "{}"
    return errors


def check_tool_call_id(
    tool_call_id: str | None, message_content: str
) -> str | None:
    """Return an error message if *tool_call_id* is missing, otherwise ``None``.

    This is a shared helper for the ``tool_call_id is None`` guard used across
    multiple providers.
    """
    if tool_call_id is None:
        return (
            f"Error: Tool message is missing `tool_call_id`. "
            f"Content: {message_content}"
        )
    return None


class BaseStreamedMessage:
    """Mixin / base class for provider-specific streamed messages.

    Provides the common ``__aiter__`` / ``__anext__`` / ``id`` boilerplate.
    Subclasses must set ``self._iter`` in ``__init__``.
    """

    _iter: AsyncIterator[StreamedMessagePart]
    _id: str | None = None

    def __aiter__(self) -> AsyncIterator[StreamedMessagePart]:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    @property
    def id(self) -> str | None:
        return self._id
