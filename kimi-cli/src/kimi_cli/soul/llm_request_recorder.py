"""Request-trace recorder for outbound LLM requests (wire.jsonl observability).

Ports the kimi-code ``wire.jsonl`` request-trace design into kimi-cli:

- `LLMToolsSnapshot`: content-addressed tool table, emitted once per unique hash.
- `LLMRequest`: one record per outbound LLM request (loop step or compaction).
- `MCPToolsDiscovered`: verbatim MCP ``tools/list`` result with registration outcome.

All records are emitted through :func:`kimi_cli.soul.wire_send`, which guarantees
ordered durable appends to ``wire.jsonl``. They are observability-only records:
persisted but never replayed to UI clients (see ``is_observability``).
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from typing import Literal

import orjson
from kosong.chat_provider import ChatProvider
from kosong.message import Message
from kosong.tooling import Tool

from kimi_cli.utils.logging import logger
from kimi_cli.wire.file import WireFile
from kimi_cli.wire.types import (
    LLMRequest,
    LLMToolSchema,
    LLMToolsSnapshot,
    MCPToolsDiscovered,
)

# The default max_tokens injected by the Kimi provider's generate()
# (see kosong.chat_provider.kimi.Kimi.generate).
_KIMI_DEFAULT_MAX_TOKENS = 32000


def _hash_json(obj: object) -> str:
    """sha256 over canonicalized (sorted-keys) JSON."""
    return hashlib.sha256(orjson.dumps(obj, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tool_schemas(tools: Sequence[Tool]) -> list[LLMToolSchema]:
    return [
        LLMToolSchema(
            name=tool.name,
            description=tool.description,
            parameters=dict(tool.parameters),
        )
        for tool in tools
    ]


def _canonical_tools(schemas: Sequence[LLMToolSchema]) -> list[dict[str, object]]:
    return [schema.model_dump(mode="json") for schema in schemas]


class LLMRequestRecorder:
    """Records outbound LLM request traces into the current wire.

    Dedup state is content-addressed: tool tables and system prompts are
    hashed, and durable snapshots are only re-emitted when the hash changes.
    On resumed sessions, :meth:`restore_from` seeds the dedup sets from the
    existing wire.jsonl so nothing durable is re-logged.
    """

    def __init__(self) -> None:
        self._seen_tools_hashes: set[str] = set()
        self._seen_prompt_hashes: set[str] = set()
        self._seen_mcp_discoveries: set[tuple[str, str]] = set()
        # Identity cache: skip re-hashing an identical tool table across steps.
        self._last_tools_key: tuple[int, ...] | None = None
        self._last_tools_hash: str | None = None

    async def restore_from(self, wire_file: WireFile) -> None:
        """Seed dedup sets from an existing wire.jsonl (resumed sessions).

        Reads hashes from raw envelope payloads by ``type`` string; payloads are
        never re-validated, so this stays cheap and forward compatible.
        """
        async for record in wire_file.iter_records():
            payload = record.message.payload
            match record.message.type:
                case "LLMToolsSnapshot":
                    if isinstance(h := payload.get("hash"), str):
                        self._seen_tools_hashes.add(h)
                case "LLMRequest":
                    if isinstance(h := payload.get("system_prompt_hash"), str):
                        self._seen_prompt_hashes.add(h)
                    if isinstance(h := payload.get("tools_hash"), str):
                        self._seen_tools_hashes.add(h)
                case "MCPToolsDiscovered":
                    server = payload.get("server_name")
                    h = payload.get("hash")
                    if isinstance(server, str) and isinstance(h, str):
                        self._seen_mcp_discoveries.add((server, h))
                case _:
                    pass

    def record(
        self,
        chat_provider: ChatProvider,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
        *,
        kind: Literal["loop", "compaction"] = "loop",
        turn_step: int | None = None,
        attempt: int = 1,
        dropped_count: int | None = None,
    ) -> None:
        """Record one outbound LLM request. Never raises."""
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # Pre-flight rule: the request will not actually be sent.
            return
        try:
            self._record(
                chat_provider,
                system_prompt,
                tools,
                history,
                kind=kind,
                turn_step=turn_step,
                attempt=attempt,
                dropped_count=dropped_count,
            )
        except Exception:
            logger.exception("Failed to record LLM request trace")

    def _record(
        self,
        chat_provider: ChatProvider,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
        *,
        kind: Literal["loop", "compaction"],
        turn_step: int | None,
        attempt: int,
        dropped_count: int | None,
    ) -> None:
        from kimi_cli.soul import wire_send

        # 1. Tools snapshot (content-addressed, emitted once per unique hash).
        tools_key = tuple(id(tool) for tool in tools)
        if tools_key == self._last_tools_key and self._last_tools_hash is not None:
            tools_hash = self._last_tools_hash
            schemas: list[LLMToolSchema] | None = None
        else:
            schemas = _tool_schemas(tools)
            tools_hash = _hash_json(_canonical_tools(schemas))
            self._last_tools_key = tools_key
            self._last_tools_hash = tools_hash
        if tools_hash not in self._seen_tools_hashes:
            self._seen_tools_hashes.add(tools_hash)
            if schemas is None:
                schemas = _tool_schemas(tools)
            wire_send(LLMToolsSnapshot(hash=tools_hash, tools=schemas))

        # 2. The request record itself.
        prompt_hash = _hash_text(system_prompt)
        inline_prompt: str | None = None
        if prompt_hash not in self._seen_prompt_hashes:
            self._seen_prompt_hashes.add(prompt_hash)
            inline_prompt = system_prompt

        provider, model, thinking_effort, temperature, top_p, max_tokens = (
            self._provider_fields(chat_provider)
        )
        wire_send(
            LLMRequest(
                kind=kind,
                provider=provider,
                model=model,
                thinking_effort=thinking_effort,
                temperature=temperature,
                top_p=top_p,
                max_tokens=max_tokens,
                system_prompt_hash=prompt_hash,
                system_prompt=inline_prompt,
                tools_hash=tools_hash,
                message_count=len(history),
                turn_step=turn_step,
                attempt=attempt,
                dropped_count=dropped_count,
            )
        )

    def record_mcp_discovery(
        self,
        server_name: str,
        tools: Sequence[LLMToolSchema],
        enabled_names: Sequence[str],
        collisions: Sequence[str] = (),
    ) -> None:
        """Record a verbatim MCP ``tools/list`` discovery. Never raises."""
        try:
            discovery_hash = _hash_json(
                {
                    "tools": _canonical_tools(tools),
                    "enabled_names": list(enabled_names),
                    "collisions": list(collisions),
                }
            )
            key = (server_name, discovery_hash)
            if key in self._seen_mcp_discoveries:
                return
            self._seen_mcp_discoveries.add(key)

            from kimi_cli.soul import wire_send

            wire_send(
                MCPToolsDiscovered(
                    server_name=server_name,
                    hash=discovery_hash,
                    tools=list(tools),
                    enabled_names=list(enabled_names),
                    collisions=list(collisions),
                )
            )
        except Exception:
            logger.exception("Failed to record MCP tools discovery trace")

    @staticmethod
    def _provider_fields(
        chat_provider: ChatProvider,
    ) -> tuple[str, str, str | None, float | None, float | None, int | None]:
        """Derive (provider, model, thinking_effort, temperature, top_p, max_tokens).

        The effective max completion tokens cap is derived defensively:
        - Kimi provider: ``_generation_kwargs["max_tokens"]`` if set, else the
          documented default injected by ``generate()`` (32000).
        - Other providers: ``max_completion_tokens`` attribute if present, else None.
        """
        provider = str(getattr(chat_provider, "name", type(chat_provider).__name__))
        model = str(getattr(chat_provider, "model_name", ""))

        thinking_effort: str | None = None
        effort = getattr(chat_provider, "thinking_effort", None)
        if isinstance(effort, str):
            thinking_effort = effort

        temperature: float | None = None
        top_p: float | None = None
        max_tokens: int | None = None

        gen_kwargs = getattr(chat_provider, "_generation_kwargs", None)
        if isinstance(gen_kwargs, dict):
            if isinstance(t := gen_kwargs.get("temperature"), (int, float)):
                temperature = float(t)
            if isinstance(p := gen_kwargs.get("top_p"), (int, float)):
                top_p = float(p)
            if isinstance(m := gen_kwargs.get("max_tokens"), int):
                max_tokens = m

        if max_tokens is None:
            if provider == "kimi":
                max_tokens = _KIMI_DEFAULT_MAX_TOKENS
            elif isinstance(m := getattr(chat_provider, "max_completion_tokens", None), int):
                max_tokens = m

        return provider, model, thinking_effort, temperature, top_p, max_tokens
