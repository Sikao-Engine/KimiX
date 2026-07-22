"""Live probe v2: reproduce the step-N 400 with reasoning summaries in history.

Differences from v1:
- Uses `kosong.generate` (proper ToolCall/ToolCallPart merging), mirroring the
  real kimi-cli agent loop.
- Forces the model to produce *visible* reasoning summaries + parallel tool
  calls in step 1 (like the failing session did).
- Once a history with ThinkPart(summary>0, encrypted) exists, sends mutated
  payload variants directly to isolate the 400 trigger:
    A: exact kosong payload (baseline)
    B: reasoning items dropped entirely
    C: reasoning summary text emptied (encrypted kept)
    D: reasoning encrypted_content dropped (summary kept)

Usage:
    uv run python tools/probe_openai_responses2.py
"""

import asyncio
import copy
import json
import sys
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).parent / "probe_out"
OUT_DIR.mkdir(exist_ok=True)

import kosong  # noqa: E402
from kosong.contrib.chat_provider import openai_responses as oresp  # noqa: E402
from kosong.contrib.chat_provider.openai_responses import OpenAIResponses  # noqa: E402
from kosong.message import Message  # noqa: E402
from kosong.tooling import Tool  # noqa: E402

CFG = json.loads(Path("C:/dev/gpt.json").read_text(encoding="utf-8"))

SYSTEM_PROMPT = "You are a helpful assistant. Always use tools when asked about weather."

TOOLS = [
    Tool(
        name="get_weather",
        description="Get the weather of a city.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    ),
    Tool(
        name="get_time",
        description="Get the current local time of a city.",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    ),
]

request_no = 0

# Dump every request payload + log SSE events, as in v1.
_orig_generate = OpenAIResponses.generate


async def _patched_generate(self: OpenAIResponses, system_prompt, tools, history):
    global request_no
    request_no += 1
    inputs: list[Any] = []
    if system_prompt:
        inputs.append({"role": "system", "content": system_prompt})
    for message in history:
        inputs.extend(self._convert_message(message))
    (OUT_DIR / f"v2_req{request_no}_payload.json").write_text(
        json.dumps(inputs, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return await _orig_generate(self, system_prompt, tools, history)


OpenAIResponses.generate = _patched_generate  # type: ignore[assignment]

_orig_convert_stream = oresp.OpenAIResponsesStreamedMessage._convert_stream_response


async def _patched_convert_stream(self, response):
    log_path = OUT_DIR / f"v2_req{request_no}_events.jsonl"
    with log_path.open("w", encoding="utf-8") as f:

        async def wrapped():
            async for chunk in response:
                record: dict[str, Any] = {"type": chunk.type}
                if chunk.type in ("response.output_item.added", "response.output_item.done"):
                    item = chunk.item
                    record["item_type"] = item.type
                    if item.type == "function_call":
                        record["arguments"] = getattr(item, "arguments", None)
                    elif item.type == "reasoning":
                        record["summary"] = [
                            getattr(s, "text", None) for s in (getattr(item, "summary", []) or [])
                        ]
                        record["encrypted_len"] = len(getattr(item, "encrypted_content", None) or "")
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                yield chunk

        async for part in _orig_convert_stream(self, wrapped()):
            yield part


oresp.OpenAIResponsesStreamedMessage._convert_stream_response = _patched_convert_stream


async def send_raw(provider: OpenAIResponses, inputs: list[dict[str, Any]], label: str) -> None:
    """Send a raw payload variant and report status."""
    try:
        resp = await provider.client.responses.create(
            stream=False,
            model=provider.model_name,
            input=inputs,  # type: ignore[arg-type]
            tools=[oresp._convert_tool(t) for t in TOOLS],
            store=False,
            extra_body={"reasoning": {"effort": "xhigh", "summary": "auto"}},
            include=["reasoning.encrypted_content"],  # type: ignore[arg-type]
        )
        kinds = [item.type for item in resp.output]
        print(f"[variant {label}] OK output_items={kinds}")
    except Exception as e:  # noqa: BLE001
        body = getattr(e, "body", None)
        print(f"[variant {label}] {type(e).__name__}: {getattr(e, 'message', str(e))[:500]}")
        print(f"[variant {label}] body: {json.dumps(body, ensure_ascii=False, default=str)[:1000]}")


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
                "Think carefully step-by-step (explain your reasoning in detail) about which "
                "tool calls are needed, then call get_weather AND get_time for both Beijing "
                "and Shanghai (4 calls in parallel). nonce=ax71"
            ),
        )
    ]

    # Step 1
    r1 = await kosong.generate(provider, SYSTEM_PROMPT, TOOLS, history)
    history.append(r1.message)
    think = [p for p in r1.message.content if p.type == "think"]
    print(f"[step1] think_parts={[(len(p.think), bool(p.encrypted)) for p in think]}")
    print(f"[step1] tool_calls={[tc.function.name for tc in r1.message.tool_calls or []]}")
    for tc in r1.message.tool_calls or []:
        history.append(
            Message(role="tool", tool_call_id=tc.id, content=f"{tc.function.name} result: ok")
        )

    # Step 2 — the request shape that 400s in the field (reasoning w/ summary + calls)
    history.append(Message(role="user", content="Now do the same for Tokyo. nonce=bx92"))
    try:
        r2 = await kosong.generate(provider, SYSTEM_PROMPT, TOOLS, history)
        print("[step2] OK")
        history.append(r2.message)
        for tc in r2.message.tool_calls or []:
            history.append(
                Message(role="tool", tool_call_id=tc.id, content=f"{tc.function.name} result: ok")
            )
    except Exception as e:  # noqa: BLE001
        print(f"[step2] {type(e).__name__}: {getattr(e, 'message', str(e))[:500]}")
        body = getattr(getattr(e, "__cause__", None), "body", None)
        print(f"[step2] body: {json.dumps(body, ensure_ascii=False, default=str)[:1000]}")

    # ---- Isolation experiments on the step-2-shaped payload ----------------
    inputs: list[Any] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in history[:-1]:  # exclude the last user msg; rebuild exact step-2 payload
        inputs.extend(provider._convert_message(m))
    inputs.append({"role": "user", "content": "Ping. nonce=cx13", "type": "message"})

    def strip_reasoning(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [it for it in items if it.get("type") != "reasoning"]

    def mutate_reasoning(items, *, empty_summary=False, drop_encrypted=False):
        out = copy.deepcopy(items)
        for it in out:
            if it.get("type") == "reasoning":
                if empty_summary:
                    for s in it.get("summary", []):
                        s["text"] = ""
                if drop_encrypted:
                    it.pop("encrypted_content", None)
        return out

    await send_raw(provider, inputs, "A: exact kosong payload")
    await send_raw(provider, strip_reasoning(inputs), "B: reasoning dropped")
    await send_raw(provider, mutate_reasoning(inputs, empty_summary=True), "C: summary emptied")
    await send_raw(provider, mutate_reasoning(inputs, drop_encrypted=True), "D: encrypted dropped")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
