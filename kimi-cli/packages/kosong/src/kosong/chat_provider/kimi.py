import copy
import mimetypes
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, Self, Unpack, cast

import httpx
from openai import AsyncOpenAI, AsyncStream, BaseModel, OpenAIError, omit
from openai._types import RequestFiles, RequestOptions
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from typing_extensions import TypedDict

from kosong.chat_provider import (
    ChatProvider,
    ChatProviderError,
    RetryableChatProvider,
    ThinkingEffort,
    TokenUsage,
)
from kosong.chat_provider.openai_common import (
    CommonGenerationKwargs,
    OpenAICompatibleProviderMixin,
    OpenAICompatibleStreamedMessage,
    apply_generation_kwargs,
    convert_error,
    extract_reasoning_from_content,
    is_effectively_empty_content_parts,
    maybe_log_reasoning_content_error,
    tool_to_openai,
)
from kosong.message import (
    ContentPart,
    Message,
    TextPart,
    ThinkPart,
    VideoURLPart,
)
from kosong.tooling import Tool
from kosong.utils.jsonschema import JsonDict, ensure_property_types

if TYPE_CHECKING:

    def type_check(kimi: "Kimi"):
        _: ChatProvider = kimi
        _: RetryableChatProvider = kimi


class ThinkingConfig(TypedDict, total=False):
    type: Literal["enabled", "disabled"]
    keep: Any
    """Moonshot-specific ``thinking.keep`` switch for preserved thinking.
    Forwarded verbatim to the API; callers are responsible for choosing a value
    the server accepts (e.g. ``"all"``)."""


class ExtraBody(TypedDict, total=False, extra_items=Any):
    thinking: ThinkingConfig


class Kimi(OpenAICompatibleProviderMixin):
    """
    A chat provider that uses the Kimi API.

    >>> chat_provider = Kimi(model="kimi-k2-turbo-preview", api_key="sk-1234567890")
    >>> chat_provider.name
    'kimi'
    >>> chat_provider.model_name
    'kimi-k2-turbo-preview'
    >>> chat_provider.with_generation_kwargs(temperature=0)._generation_kwargs
    {'temperature': 0}
    >>> chat_provider._generation_kwargs
    {}
    """

    name = "kimi"

    class GenerationKwargs(CommonGenerationKwargs, total=False):
        """
        See https://platform.moonshot.ai/docs/api/chat#request-body.
        """

        n: int | None
        presence_penalty: float | None
        frequency_penalty: float | None
        stop: str | list[str] | None
        prompt_cache_key: str | None
        reasoning_effort: str | None
        """Legacy thinking parameter. Use `extra_body.thinking` instead."""
        extra_body: ExtraBody | None

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        stream: bool = True,
        **client_kwargs: Any,
    ):
        if api_key is None:
            api_key = os.getenv("KIMI_API_KEY")
        if api_key is None:
            raise ChatProviderError(
                "The api_key client option or the KIMI_API_KEY environment variable is not set"
            )
        if base_url is None:
            base_url = os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")

        self._init_openai_client(api_key=api_key, base_url=base_url, client_kwargs=client_kwargs)
        """The underlying `AsyncOpenAI` client."""
        self.model: str = model
        """The name of the model to use."""
        self.stream: bool = stream
        """Whether to generate responses as a stream."""
        self._generation_kwargs: Kimi.GenerationKwargs = {}

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        reasoning_effort = self._generation_kwargs.get("reasoning_effort")
        if reasoning_effort is None:
            return None
        match reasoning_effort:
            case "low":
                return "low"
            case "medium":
                return "medium"
            case "high":
                return "high"
            case _:
                return "off"

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> "KimiStreamedMessage":
        messages: list[ChatCompletionMessageParam] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(_convert_message(message) for message in history)

        generation_kwargs: dict[str, Any] = {
            # default kimi generation kwargs
            "max_tokens": 32000,
        }
        generation_kwargs.update(self._generation_kwargs)

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=(_convert_tool(tool) for tool in tools),
                stream=self.stream,
                stream_options={"include_usage": True} if self.stream else omit,
                **generation_kwargs,
            )
            return KimiStreamedMessage(response)
        except (OpenAIError, httpx.HTTPError) as e:
            maybe_log_reasoning_content_error(
                e,
                provider_name=self.name,
                model=self.model,
                messages=messages,
                generation_kwargs=generation_kwargs,
            )
            raise convert_error(e) from e

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        match effort:
            case "off":
                reasoning_effort = None
            case "low":
                reasoning_effort = "low"
            case "medium":
                reasoning_effort = "medium"
            case "high" | "xhigh" | "max":
                # Kimi's API caps at "high"; xhigh/max are Anthropic-specific.
                reasoning_effort = "high"
        return self.with_generation_kwargs(reasoning_effort=reasoning_effort).with_extra_body(
            {
                "thinking": {
                    "type": "enabled" if effort != "off" else "disabled",
                }
            }
        )

    def with_generation_kwargs(self, **kwargs: Unpack[GenerationKwargs]) -> Self:
        """
        Copy the chat provider, updating the generation kwargs with the given values.

        Returns:
            Self: A new instance of the chat provider with updated generation kwargs.
        """
        return apply_generation_kwargs(self, **kwargs)

    def with_extra_body(self, extra_body: ExtraBody) -> Self:
        """
        Copy the chat provider, updating the extra_body in generation kwargs.

        Top-level keys follow last-writer-wins semantics, except for the
        ``thinking`` key: its sub-dict is merged field-by-field so that a
        later call adding ``thinking.keep`` does not erase a ``thinking.type``
        installed by an earlier ``with_thinking`` call.

        Returns:
            Self: A new instance of the chat provider with updated extra_body.
        """
        new_self = copy.copy(self)
        new_self._generation_kwargs = copy.deepcopy(self._generation_kwargs)
        old_extra_body = new_self._generation_kwargs.get("extra_body") or {}
        new_extra_body: ExtraBody = {**old_extra_body, **extra_body}
        old_thinking = old_extra_body.get("thinking")
        new_thinking = extra_body.get("thinking")
        if old_thinking is not None and new_thinking is not None:
            new_extra_body["thinking"] = {**old_thinking, **new_thinking}
        new_self._generation_kwargs["extra_body"] = new_extra_body
        return new_self

    @property
    def files(self) -> "KimiFiles":
        return KimiFiles(self.client)


class KimiFiles:
    def __init__(self, client: AsyncOpenAI) -> None:
        self._client = client

    async def upload_video(self, *, data: bytes, mime_type: str) -> VideoURLPart:
        """Upload a video to Kimi files API and return a video URL content part."""
        if not mime_type.startswith("video/"):
            raise ChatProviderError(f"Expected a video mime type, got {mime_type}")
        url = await self._upload_file(data=data, mime_type=mime_type, purpose="video")
        return VideoURLPart(video_url=VideoURLPart.VideoURL(url=url))

    async def _upload_file(self, *, data: bytes, mime_type: str, purpose: "KimiFilePurpose") -> str:
        filename = _guess_filename(mime_type)
        files: RequestFiles = {"file": (filename, data, mime_type)}
        options: RequestOptions = {"headers": {"Content-Type": "multipart/form-data"}}
        try:
            response: KimiFileObject = await self._client.post(
                "/files",
                cast_to=KimiFileObject,
                body={"purpose": purpose},
                files=files,
                options=options,
            )
        except (OpenAIError, httpx.HTTPError) as e:
            raise convert_error(e) from e
        return f"ms://{response.id}"


class KimiFileObject(BaseModel):
    id: str


type KimiFilePurpose = Literal["video", "image"]


def _guess_filename(mime_type: str) -> str:
    extension = mimetypes.guess_extension(mime_type) or ".bin"
    return f"upload{extension}"


def _convert_message(message: Message) -> ChatCompletionMessageParam:
    message = message.model_copy(deep=True)
    reasoning_content, visible_content = extract_reasoning_from_content(message.content)
    has_reasoning = any(isinstance(part, ThinkPart) for part in message.content)
    message.content = visible_content
    dumped_message = message.model_dump(exclude_none=True)
    if (
        message.role == "assistant"
        and message.tool_calls
        and is_effectively_empty_content_parts(visible_content)
    ):
        # OpenAI-compatible APIs allow assistant tool-call messages to omit
        # `content`, but the Kimi-for-Coding compat layer rejects a content
        # list that contains an empty text part (observed: `content:
        # [{"type": "text", "text": ""}]` -> 400 "text content is empty").
        # Dropping `content` entirely is always accepted, so do that whenever
        # the visible content is effectively empty alongside a tool call.
        dumped_message.pop("content", None)
    if has_reasoning:
        dumped_message["reasoning_content"] = reasoning_content
    return cast(ChatCompletionMessageParam, dumped_message)


def _convert_tool(tool: Tool) -> ChatCompletionToolParam:
    if tool.name.startswith("$"):
        # Kimi builtin functions start with `$`
        return cast(
            ChatCompletionToolParam,
            {
                "type": "builtin_function",
                "function": {
                    "name": tool.name,
                    # no need to set description and parameters
                },
            },
        )
    converted = tool_to_openai(tool)
    # Moonshot's API rejects parameter schemas whose nested properties omit
    # `type` (e.g. enum-only properties exposed by some MCP servers). Patch
    # the schema locally so such tools keep working against Kimi without
    # requiring every MCP server author to tighten their schemas.
    function = converted["function"]
    parameters = function.get("parameters")
    if isinstance(parameters, dict):
        normalized = ensure_property_types(cast(JsonDict, parameters))
        function["parameters"] = cast(dict[str, object], normalized)
    return converted


class KimiStreamedMessage(OpenAICompatibleStreamedMessage):
    """The streamed message of the Kimi chat provider."""

    def __init__(self, response: ChatCompletion | AsyncStream[ChatCompletionChunk]):
        super().__init__(response, reasoning_key="reasoning_content")

    @property
    def usage(self) -> TokenUsage | None:
        if self._usage:
            cached = 0
            total_input = self._usage.prompt_tokens
            if hasattr(self._usage, "cached_tokens"):
                # https://platform.moonshot.cn/docs/api/chat#%E8%BF%94%E5%9B%9E%E5%86%85%E5%AE%B9
                # TODO: delete this when Moonshot API becomes compatible with OpenAI API
                cached = getattr(self._usage, "cached_tokens") or 0  # noqa: B009
            elif (
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


if __name__ == "__main__":

    async def _dev_main():
        chat = Kimi(model="kimi-k2-turbo-preview", stream=False)
        system_prompt = ""
        history = [
            Message(role="user", content="Hello, who is Confucius?"),
        ]
        stream = await chat.with_generation_kwargs(
            temperature=0,
            max_tokens=1000,
        ).generate(system_prompt, [], history)
        async for part in stream:
            print(part.model_dump(exclude_none=True))
        print("id:", stream.id)
        print("usage:", stream.usage)

    import asyncio

    from dotenv import load_dotenv

    load_dotenv()
    asyncio.run(_dev_main())
