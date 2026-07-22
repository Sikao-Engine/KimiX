"""Live probe for the OpenAI Responses provider against the venus llmproxy.

Reproduces the kimi-cli agent loop (3 steps, parallel tool calls, thinking on)
using the config in C:/dev/gpt.json, and captures:

1. Every raw streamed part emitted by `OpenAIResponsesStreamedMessage`
   (to detect duplicate ToolCall emissions / argument corruption).
2. The exact `input` payload sent on every request (dumped to JSON files).
3. The full 400 error body when the API rejects a request.

Usage:
    uv run python tools/probe_openai_responses.py
"""

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any

import httpx

OUT_DIR = Path(__file__).parent / "probe_out"
OUT_DIR.mkdir(exist_ok=True)

from kosong.chat_provider import APIStatusError, ChatProviderError  # noqa: E402
from kosong.contrib.chat_provider import openai_responses as oresp  # noqa: E402
from kosong.contrib.chat_provider.openai_responses import OpenAIResponses  # noqa: E402
from kosong.message import Message, TextPart, ThinkPart, ToolCall, ToolCallPart  # noqa: E402
from kosong.tooling import Tool  # noqa: E402

CFG = json.loads(Path("C:/dev/gpt.json").read_text(encoding="utf-8"))

SYSTEM_PROMPT = "You are a helpful assistant. Always use tools when asked about weather."

TOOLS = [
    Tool(
        name="get_weather",
        description="Get the weather of a city.",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "The city name."},
            },
            "required": ["city"],
        },
    ),
    Tool(
        name="get_time",
        description="Get the current local time of a city.",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "The city name."},
            },
            "required": ["city"],
        },
    ),
]

step_no = 0

# ---------------------------------------------------------------------------
# Patch 1: dump the exact request payload of every responses.create call.
# ---------------------------------------------------------------------------
_orig_generate = OpenAIResponses.generate


async def _patched_generate(self: OpenAIResponses, system_prompt, tools, history):
    global step_no
    step_no += 1
    inputs: list[Any] = []
    if system_prompt:
        inputs.append({"role": "system", "content": system_prompt})
    for message in history:
        inputs.extend(self._convert_message(message))
    payload_path = OUT_DIR / f"step{step_no}_payload.json"
    payload_path.write_text(
        json.dumps(inputs, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print(f"[probe] dumped request payload for step {step_no} -> {payload_path}")
    return await _orig_generate(self, system_prompt, tools, history)


OpenAIResponses.generate = _patched_generate  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Patch 2: log every raw SSE event type seen by the streamed message.
# ---------------------------------------------------------------------------
_orig_convert_stream = oresp.OpenAIResponsesStreamedMessage._convert_stream_response


async def _patched_convert_stream(self, response):
    events_log = OUT_DIR / f"step{step_no}_events.jsonl"
    with events_log.open("w", encoding="utf-8") as f:

        async def wrapped():
            async for chunk in response:
                record: dict[str, Any] = {"type": chunk.type}
                if chunk.type in ("response.output_item.added", "response.output_item.done"):
                    item = chunk.item
                    record["item_type"] = item.type
                    record["item_id"] = getattr(item, "id", None)
                    if item.type == "function_call":
                        record["call_id"] = getattr(item, "call_id", None)
                        record["name"] = getattr(item, "name", None)
                        record["arguments"] = getattr(item, "arguments", None)
                    elif item.type == "reasoning":
                        record["summary_count"] = len(getattr(item, "summary", []) or [])
                        record["encrypted_len"] = len(getattr(item, "encrypted_content", None) or "")
                elif chunk.type in (
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                ):
                    record["item_id"] = getattr(chunk, "item_id", None)
                    record["delta"] = getattr(chunk, "delta", None)
                    record["arguments"] = getattr(chunk, "arguments", None)
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                yield chunk

        async for part in _orig_convert_stream(self, wrapped()):
            yield part


oresp.OpenAIResponsesStreamedMessage._convert_stream_response = _patched_convert_stream


def describe_part(part: Any) -> str:
    if isinstance(part, ThinkPart):
        enc = part.encrypted
        return f"ThinkPart(think={part.think[:40]!r}..., encrypted={'<none>' if enc is None else f'<len={len(enc)}>'})"
    if isinstance(part, TextPart):
        return f"TextPart({part.text[:60]!r})"
    if isinstance(part, ToolCall):
        return (
            f"ToolCall(id={part.id!r}, name={part.function.name!r}, "
            f"arguments={part.function.arguments!r})"
        )
    if isinstance(part, ToolCallPart):
        return f"ToolCallPart({part.arguments_part!r})"
    return repr(part)


async def run_step(provider: OpenAIResponses, history: list[Message], label: str) -> Message:
    print(f"\n================ {label} ================")
    stream = await provider.generate(SYSTEM_PROMPT, TOOLS, history)
    content: list[Any] = []
    tool_calls: list[ToolCall] = []
    async for part in stream:
        print(f"[part] {describe_part(part)}")
        if isinstance(part, (TextPart, ThinkPart)):
            content.append(part)
        elif isinstance(part, ToolCall):
            tool_calls.append(part)
    print(f"[probe] stream.id={stream.id!r} usage={stream.usage}")
    msg = Message(role="assistant", content=content, tool_calls=tool_calls or None)
    return msg


async def main() -> int:
    provider = OpenAIResponses(
        model=CFG["model"],
        base_url=CFG["url"],
        api_key=CFG["api_key"],
    ).with_thinking("xhigh")

    history: list[Message] = [
        Message(
            role="user",
            content=(
                "Call get_weather AND get_time for both Beijing and Shanghai "
                "(4 tool calls in parallel). Do not answer in text, just call the tools."
            ),
        )
    ]

    try:
        # Step 1: expect parallel tool calls
        msg1 = await run_step(provider, history, "STEP 1 (fresh)")
        history.append(msg1)
        for tc in msg1.tool_calls or []:
            history.append(
                Message(role="tool", tool_call_id=tc.id, content=f"{tc.function.name} result: ok")
            )

        # Step 2: ask for another round of parallel tool calls
        history.append(
            Message(
                role="user",
                content=(
                    "Now call get_weather AND get_time for Tokyo and Seoul "
                    "(4 tool calls in parallel)."
                ),
            )
        )
        msg2 = await run_step(provider, history, "STEP 2 (1 assistant turn in history)")
        history.append(msg2)
        for tc in msg2.tool_calls or []:
            history.append(
                Message(role="tool", tool_call_id=tc.id, content=f"{tc.function.name} result: ok")
            )

        # Step 3: this is the shape of request that 400s in the field
        history.append(Message(role="user", content="Summarize what you did in one sentence."))
        await run_step(provider, history, "STEP 3 (2 assistant turns in history)")
    except APIStatusError as e:
        print(f"\n[probe] APIStatusError status={e.status_code}")
        print(f"[probe] message: {e.message}")
        cause = e.__cause__
        if cause is not None:
            print(f"[probe] cause: {type(cause).__name__}: {cause}")
            body = getattr(cause, "body", None)
            print(f"[probe] body: {json.dumps(body, indent=2, ensure_ascii=False, default=str)}")
        return 1
    except ChatProviderError as e:
        print(f"\n[probe] ChatProviderError: {e}")
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
