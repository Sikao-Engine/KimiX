from collections.abc import Sequence
from dataclasses import dataclass

from loguru import logger

from kosong.chat_provider import (
    APIConnectionError,
    APIEmptyResponseError,
    APITimeoutError,
    ChatProvider,
    StreamedMessagePart,
    TokenUsage,
)
from kosong.message import ContentPart, Message, TextPart, ThinkPart, ToolCall
from kosong.tooling import Tool
from kosong.utils.aio import Callback, callback


async def generate(
    chat_provider: ChatProvider,
    system_prompt: str,
    tools: Sequence[Tool],
    history: Sequence[Message],
    *,
    on_message_part: Callback[[StreamedMessagePart], None] | None = None,
    on_tool_call: Callback[[ToolCall], None] | None = None,
) -> GenerateResult:
    """
    Generate one message based on the given context.
    Parts of the message will be streamed to the specified callbacks if provided.

    Args:
        chat_provider: The chat provider to use for generation.
        system_prompt: The system prompt to use for generation.
        tools: The tools available for the model to call.
        history: The message history to use for generation.
        on_message_part: An optional callback to be called for each raw message part.
        on_tool_call: An optional callback to be called for each complete tool call.

    Returns:
        A tuple of the generated message and the token usage (if available).
        All parts in the message are guaranteed to be complete and merged as much as possible.

    Raises:
        APIConnectionError: If the API connection fails.
        APITimeoutError: If the API request times out.
        APIStatusError: If the API returns a status code of 4xx or 5xx.
        APIEmptyResponseError: If the API returns an empty response.
        ChatProviderError: If any other recognized chat provider error occurs.
    """
    message = Message(role="assistant", content=[])
    pending_part: StreamedMessagePart | None = None  # message part that is currently incomplete

    logger.trace("Generating with history: {history}", history=history)
    stream = await chat_provider.generate(system_prompt, tools, history)
    try:
        async for part in stream:
            logger.trace("Received part: {part}", part=part)
            if on_message_part:
                await callback(on_message_part, part.model_copy(deep=True))

            if pending_part is None:
                pending_part = part
            elif not pending_part.merge_in_place(part):  # try merge into the pending part
                # unmergeable part must push the pending part to the buffer
                _message_append(message, pending_part)
                if isinstance(pending_part, ToolCall) and on_tool_call:
                    await callback(on_tool_call, pending_part)
                pending_part = part
    except (APITimeoutError, APIConnectionError) as exc:
        # The stream died mid-flight (stalled connection / dropped transport)
        # *after* some parts were already delivered. If a complete tool call
        # was fully streamed before the failure, salvage it: the backend
        # (observed with GLM/bigmodel while writing long tool call arguments)
        # sometimes stops sending the trailing keep-alive/finish chunks after
        # the arguments are complete. Discarding the message here would throw
        # away perfectly valid work and push the caller into the retry /
        # session-restart path, visibly "stopping" the turn right when the
        # tool call was about to run.
        if isinstance(pending_part, ToolCall) and _is_complete_tool_call(pending_part):
            logger.warning(
                "Stream failed with {error_type} after a complete tool call "
                "'{name}'; salvaging the tool call instead of failing the step.",
                error_type=type(exc).__name__,
                name=pending_part.function.name,
            )
            _message_append(message, pending_part)
            if on_tool_call:
                await callback(on_tool_call, pending_part)
            pending_part = None
            if message.content or message.tool_calls:
                return GenerateResult(
                    id=stream.id,
                    message=message,
                    usage=stream.usage,
                )
        raise

    # end of message
    if pending_part is not None:
        _message_append(message, pending_part)
        if isinstance(pending_part, ToolCall) and on_tool_call:
            await callback(on_tool_call, pending_part)

    if not message.content and not message.tool_calls:
        raise APIEmptyResponseError("The API returned an empty response.")

    # A response with only ThinkPart (no TextPart, no tool calls) indicates an
    # abnormal termination — typically a stream interruption or max_tokens
    # exhaustion during reasoning.  The model should always produce visible
    # output after thinking; a think-only response is never intentional.
    think_parts = [p for p in message.content if isinstance(p, ThinkPart)]
    has_think = bool(think_parts)
    has_text = any(isinstance(p, TextPart) and p.text.strip() for p in message.content)
    if has_think and not has_text and not message.tool_calls:
        reason_len = sum(len(p.think) for p in think_parts)
        raise APIEmptyResponseError(
            "The API returned a response containing only thinking content "
            "without any text or tool calls. This usually indicates the "
            "stream was interrupted or the output token budget was exhausted "
            f"during reasoning. (reasoning text length: {reason_len} characters)"
        )

    return GenerateResult(
        id=stream.id,
        message=message,
        usage=stream.usage,
    )


@dataclass(frozen=True, slots=True)
class GenerateResult:
    """The result of a generation."""

    id: str | None
    """The ID of the generated message."""
    message: Message
    """The generated message."""
    usage: TokenUsage | None
    """The token usage of the generated message."""


def _sanitize_tool_call_arguments(tool_call: ToolCall) -> None:
    """Repair invalid-JSON tool call arguments in place before the call is
    dispatched to tools and appended to the generated message.

    Some gateways stream duplicated argument chunks which merge into strings
    like ``'{}{}'``; such arguments may execute leniently (json_repair) but
    poison the persisted history — strict backends 400 on them when echoed
    back.  The repair keeps history sendable.  See
    ``kosong.utils.jsonx.sanitize_tool_arguments``.
    """
    from kosong.utils.jsonx import sanitize_tool_arguments

    original = tool_call.function.arguments
    sanitized = sanitize_tool_arguments(original)
    if sanitized != original:
        logger.warning(
            "Repaired invalid tool call arguments for '{name}': {original!r} -> {sanitized!r}",
            name=tool_call.function.name,
            original=original,
            sanitized=sanitized,
        )
        tool_call.function.arguments = sanitized


def _is_complete_tool_call(tool_call: ToolCall) -> bool:
    """Whether *tool_call* carries complete, strict-parseable JSON object arguments.

    Used by the stream-failure salvage path: only a tool call whose arguments
    form a valid JSON object is safe to execute when the stream died without a
    terminal chunk. Truncated arguments stay on the normal error path (they are
    unreliable to act on and are repaired to ``'{}'`` only for history reuse).
    """
    import orjson

    arguments = tool_call.function.arguments
    if not arguments:
        # A tool call with no arguments at all is treated as complete (the
        # same convention as ``validate_tool_call_arguments``).
        return True
    try:
        parsed = orjson.loads(arguments)
    except orjson.JSONDecodeError:
        return False
    return isinstance(parsed, dict)


def _message_append(message: Message, part: StreamedMessagePart) -> None:
    match part:
        case ContentPart():
            message.content.append(part)
        case ToolCall():
            _sanitize_tool_call_arguments(part)
            if message.tool_calls is None:
                message.tool_calls = []
            message.tool_calls.append(part)
        case _:
            # may be an orphaned `ToolCallPart`
            return
