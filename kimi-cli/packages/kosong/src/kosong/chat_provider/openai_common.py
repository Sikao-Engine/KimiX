import asyncio
import contextlib
import copy
import re
import ssl
import uuid
from collections.abc import AsyncIterator, Awaitable, Mapping
from typing import Any, cast

import certifi
import httpx
import openai
from openai import AsyncOpenAI, AsyncStream, OpenAIError
from openai.types import ReasoningEffort
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionToolParam,
)
from openai.types.completion_usage import CompletionUsage

from kosong.chat_provider import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    ChatProviderError,
    StreamedMessagePart,
    ThinkingEffort,
    TokenUsage,
    convert_httpx_error,
)
from kosong.contrib.chat_provider.common import BaseStreamedMessage
from kosong.message import (
    ContentPart,
    TextPart,
    ThinkPart,
    ToolCall,
    ToolCallPart,
)
from kosong.tooling import Tool

from typing_extensions import TypedDict


class CommonGenerationKwargs(TypedDict, total=False):
    """Shared generation kwargs for OpenAI-compatible chat providers.

    Provider-specific ``GenerationKwargs`` TypedDicts can extend this to
    inherit the common fields while adding their own proprietary ones.
    """

    max_tokens: int | None
    temperature: float | None
    top_p: float | None


_SSL_CONTEXT: ssl.SSLContext | None = None


def _get_ssl_context() -> ssl.SSLContext:
    """Cached SSL context to avoid repeated CA bundle loading on client re-creation."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    return _SSL_CONTEXT


def create_openai_client(
    *,
    api_key: str | None,
    base_url: str | None,
    client_kwargs: Mapping[str, Any],
) -> AsyncOpenAI:
    kwargs = dict(client_kwargs)
    if "http_client" not in kwargs:
        kwargs["http_client"] = httpx.AsyncClient(verify=_get_ssl_context())
    return AsyncOpenAI(api_key=api_key, base_url=base_url, **kwargs)


_CLIENT_CLOSE_TASKS: set[asyncio.Task[None]] = set()


def _on_close_task_done(task: asyncio.Task[None]) -> None:
    _CLIENT_CLOSE_TASKS.discard(task)
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()


async def _drain_awaitable(awaitable: Awaitable[object]) -> None:
    try:
        await asyncio.wait_for(awaitable, timeout=5.0)
    except asyncio.TimeoutError:
        return
    except RuntimeError as exc:
        # On Windows/Python 3.14, closing an httpx.AsyncClient whose
        # underlying transports were bound to a now-closed ProactorEventLoop
        # raises RuntimeError('Event loop is closed').  This is harmless —
        # the OS will reclaim the socket — so we swallow it.
        if "Event loop is closed" in str(exc):
            return
        raise
    except Exception:
        return


def close_openai_client(client: AsyncOpenAI) -> None:
    """Schedule an async close of the given AsyncOpenAI client.

    ``AsyncOpenAI.close()`` is always a callable that returns an awaitable,
    so we skip the ``getattr`` / ``callable`` / ``isawaitable`` guards.
    """
    try:
        result = client.close()
    except Exception:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop running.  The client's original loop is gone.
        # Creating a new loop to close it will fail for any transport
        # bound to the old loop (ProactorEventLoop on Windows raises
        # RuntimeError('Event loop is closed')).  Just abandon the
        # client — the OS will clean up the sockets on process exit.
        return
    # Loop is running but we're in a sync context (e.g. on_retryable_error).
    # Create a task and keep a strong reference so it can run to completion.
    task = loop.create_task(_drain_awaitable(cast(Awaitable[object], result)))
    _CLIENT_CLOSE_TASKS.add(task)
    task.add_done_callback(_on_close_task_done)


def close_replaced_openai_client(client: AsyncOpenAI, *, client_kwargs: Mapping[str, Any]) -> None:
    """
    Close a replaced OpenAI client unless it would close a shared external http client.

    When callers pass `http_client=...` to `AsyncOpenAI`, multiple wrappers may share the same
    `httpx.AsyncClient`. Closing the replaced wrapper would also close that shared client and
    break the new wrapper immediately.
    """
    shared_http_client = client_kwargs.get("http_client")
    if isinstance(shared_http_client, httpx.AsyncClient) and getattr(client, "_client", None) is (
        shared_http_client
    ):
        return
    close_openai_client(client)


def convert_error(error: OpenAIError | httpx.HTTPError) -> ChatProviderError:
    # httpx errors may leak through the OpenAI SDK during streaming;
    # delegate to the shared converter.
    if isinstance(error, httpx.HTTPError):
        return convert_httpx_error(error)
    # OpenAI SDK errors — check subclasses before parents to avoid
    # misclassification (e.g. APITimeoutError inherits APIConnectionError).
    match error:
        case openai.APIStatusError():
            req_id = error.response.headers.get("x-request-id")
            return APIStatusError(error.status_code, error.message, request_id=req_id)
        case openai.APITimeoutError():
            return APITimeoutError(error.message)
        case openai.APIConnectionError():
            return APIConnectionError(error.message)
        case openai.APIError() if type(error) is openai.APIError and error.body is None:
            # Base APIError with no body indicates a transport-layer failure
            # (e.g. "Network connection lost." during streaming).  SSE error
            # events from the server carry a body dict and should fall through
            # to the default case instead.
            return _classify_base_api_error(error.message)
        case _:
            return ChatProviderError(f"Error: {error}")


_NETWORK_RE = re.compile(r"network|connection|connect|disconnect", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"timed?\s*out|timeout|deadline", re.IGNORECASE)


def _classify_base_api_error(message: str) -> ChatProviderError:
    """Heuristically map an ``openai.APIError`` message to a retryable error type.

    Timeout patterns are checked first because a message like
    "connection timed out" should be classified as a timeout, not a
    connection error.
    """
    if _TIMEOUT_RE.search(message):
        return APITimeoutError(message)
    if _NETWORK_RE.search(message):
        return APIConnectionError(message)
    return ChatProviderError(f"Error: {message}")


def thinking_effort_to_reasoning_effort(effort: ThinkingEffort) -> ReasoningEffort:
    match effort:
        case "off":
            return None
        case "low":
            return "low"
        case "medium":
            return "medium"
        case "high":
            return "high"
        case "xhigh":
            # OpenAI supports xhigh natively for models after gpt-5.1-codex-max.
            return "xhigh"
        case "max":
            # OpenAI's ceiling is xhigh; kosong's ``max`` (Anthropic-specific)
            # clamps to the highest level OpenAI accepts rather than dropping
            # down to high.
            return "xhigh"


def reasoning_effort_to_thinking_effort(effort: ReasoningEffort) -> ThinkingEffort:
    match effort:
        case "low" | "minimal":
            return "low"
        case "medium":
            return "medium"
        case "high":
            return "high"
        case "xhigh":
            return "xhigh"
        case "none" | None:
            return "off"


def tool_to_openai(tool: Tool) -> ChatCompletionToolParam:
    """Convert a single tool to OpenAI tool format."""
    # simply `model_dump` because the `Tool` type is OpenAI-compatible
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def apply_generation_kwargs(self: Any, *, attr: str = "_generation_kwargs", **kwargs: Any) -> Any:
    """Copy *self* with updated generation kwargs.

    This is the shared implementation of the ``with_generation_kwargs``
    pattern used across all chat providers.  Returns a shallow copy of
    *self* whose *attr* dict is a deep copy of the original, merged with
    *kwargs*.
    """
    new_self = copy.copy(self)
    new_kwargs = copy.deepcopy(getattr(self, attr))
    new_kwargs.update(kwargs)
    setattr(new_self, attr, new_kwargs)
    return new_self


class OpenAICompatibleProviderMixin:
    """Mix-in for any chat provider backed by an ``AsyncOpenAI`` client.

    Provides canonical implementations of :meth:`on_retryable_error` and
    :meth:`model_parameters`, plus a helper to initialise the client during
    ``__init__``.

    Subclasses must store generation kwargs in ``self._generation_kwargs``
    (the standard pattern across all providers).
    """

    def _init_openai_client(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        client_kwargs: Mapping[str, Any],
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._client_kwargs: dict[str, Any] = dict(client_kwargs)
        self.client: AsyncOpenAI = create_openai_client(
            api_key=self._api_key,
            base_url=self._base_url,
            client_kwargs=self._client_kwargs,
        )

    def on_retryable_error(self, error: BaseException) -> bool:
        old_client = self.client
        # Read api_key from the live client (not self._api_key) so that
        # OAuth token refreshes applied via client.api_key are preserved.
        current_api_key = old_client.api_key
        self.client = create_openai_client(
            api_key=current_api_key,
            base_url=self._base_url,
            client_kwargs=self._client_kwargs,
        )
        self._api_key = current_api_key
        close_replaced_openai_client(old_client, client_kwargs=self._client_kwargs)
        return True

    @property
    def model_parameters(self) -> dict[str, Any]:
        """Parameters of the underlying model (for tracing / logging)."""
        model_parameters: dict[str, Any] = {"base_url": str(self.client.base_url)}
        model_parameters.update(self._generation_kwargs)
        return model_parameters


def extract_reasoning_from_content(
    content: list[ContentPart],
) -> tuple[str, list[ContentPart]]:
    """Separate ThinkPart content from visible content parts.

    Returns a ``(reasoning_text, visible_parts)`` tuple where *reasoning_text*
    is the concatenated text of all ``ThinkPart`` items and *visible_parts*
    contains every non-``ThinkPart`` entry in its original order.
    """
    reasoning = ""
    visible: list[ContentPart] = []
    for part in content:
        if isinstance(part, ThinkPart):
            reasoning += part.think
        else:
            visible.append(part)
    return reasoning, visible


def extract_usage_from_chunk(chunk: ChatCompletionChunk) -> CompletionUsage | None:
    """Extract token usage from a streaming ``ChatCompletionChunk``.

    OpenAI-compatible APIs may place usage info at the top-level ``usage``
    field (standard) or nest it inside the first choice's model dump (some
    compatibility layers).  This helper handles both formats.
    """
    if chunk.usage:
        return chunk.usage
    if not chunk.choices:
        return None
    choice_dump: dict[str, object] = chunk.choices[0].model_dump()
    raw_usage = choice_dump.get("usage")
    if isinstance(raw_usage, CompletionUsage):
        return raw_usage
    if isinstance(raw_usage, dict):
        return CompletionUsage.model_validate(raw_usage)
    return None


class OpenAICompatibleStreamedMessage(BaseStreamedMessage):
    """Base class for streamed messages using the OpenAI Chat Completions wire format.

    Handles both streaming and non-streaming responses, text / reasoning /
    tool-call delta processing, and usage extraction.  Subclasses only need
    to supply *reasoning_key* (e.g. ``"reasoning_content"`` for Kimi) and,
    optionally, override :meth:`usage` for provider-specific cache-token
    extraction.
    """

    def __init__(
        self,
        response: ChatCompletion | AsyncStream[ChatCompletionChunk],
        *,
        reasoning_key: str | None = None,
    ):
        super().__init__()
        self._reasoning_key: str | None = reasoning_key
        if isinstance(response, ChatCompletion):
            self._iter = self._convert_non_stream_response(response)
        else:
            self._iter = self._convert_stream_response(response)
        self._usage: CompletionUsage | None = None

    # -- usage (OpenAI-standard CompletionUsage → TokenUsage) ------------------

    @property
    def usage(self) -> TokenUsage | None:
        """Derive ``TokenUsage`` from the collected ``CompletionUsage``.

        The default implementation handles the standard OpenAI caching
        schema (``prompt_tokens_details.cached_tokens``).  Providers whose
        models surface caching via non-standard attributes (e.g. Moonshot's
        legacy ``cached_tokens`` field) should override this property.
        """
        if self._usage:
            cached = 0
            total_input = self._usage.prompt_tokens
            if (
                self._usage.prompt_tokens_details
                and self._usage.prompt_tokens_details.cached_tokens
            ):
                cached = self._usage.prompt_tokens_details.cached_tokens
            return self._build_token_usage(
                input_other=total_input - cached,
                output=self._usage.completion_tokens,
                input_cache_read=cached,
            )
        return None

    # -- non-streaming conversion ---------------------------------------------

    async def _convert_non_stream_response(
        self,
        response: ChatCompletion,
    ) -> AsyncIterator[StreamedMessagePart]:
        self._id = response.id
        self._usage = response.usage
        message = response.choices[0].message
        reasoning_key = self._reasoning_key
        if reasoning_key and (reasoning_content := getattr(message, reasoning_key, None)):
            assert isinstance(reasoning_content, str)
            yield ThinkPart(think=reasoning_content)
        if message.content:
            yield TextPart(text=message.content)
        if message.tool_calls:
            for tool_call in message.tool_calls:
                if isinstance(tool_call, ChatCompletionMessageFunctionToolCall):
                    yield ToolCall(
                        id=tool_call.id or str(uuid.uuid4()),
                        function=ToolCall.FunctionBody(
                            name=tool_call.function.name,
                            arguments=tool_call.function.arguments,
                        ),
                    )

    # -- streaming conversion -------------------------------------------------

    async def _convert_stream_response(
        self,
        response: AsyncIterator[ChatCompletionChunk],
    ) -> AsyncIterator[StreamedMessagePart]:
        try:
            async for chunk in response:
                if chunk.id:
                    self._id = chunk.id
                if usage := extract_usage_from_chunk(chunk):
                    self._usage = usage

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # extract reasoning / thinking content
                reasoning_key = self._reasoning_key
                if reasoning_key and (reasoning_content := getattr(delta, reasoning_key, None)):
                    assert isinstance(reasoning_content, str)
                    yield ThinkPart(think=reasoning_content)

                # extract text content
                if delta.content:
                    yield TextPart(text=delta.content)

                # extract tool-call deltas
                for tool_call in delta.tool_calls or []:
                    if not tool_call.function:
                        continue

                    if tool_call.function.name:
                        yield ToolCall(
                            id=tool_call.id or str(uuid.uuid4()),
                            function=ToolCall.FunctionBody(
                                name=tool_call.function.name,
                                arguments=tool_call.function.arguments,
                            ),
                        )
                    elif tool_call.function.arguments:
                        yield ToolCallPart(
                            arguments_part=tool_call.function.arguments,
                        )
                    else:
                        # skip empty tool calls
                        pass
        except (OpenAIError, httpx.HTTPError) as e:
            raise convert_error(e) from e
