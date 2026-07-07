"""Tests for the LLM request-trace recorder (wire.jsonl observability records)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Self

import pytest
from kosong.chat_provider import StreamedMessagePart, ThinkingEffort, TokenUsage
from kosong.message import Message, TextPart
from kosong.tooling import Tool

from kimi_cli.soul.compaction import SimpleCompaction
from kimi_cli.soul.llm_request_recorder import (
    _KIMI_DEFAULT_MAX_TOKENS,
    LLMRequestRecorder,
)
from kimi_cli.soul.toolset import KimiToolset, PendingMCPDiscovery
from kimi_cli.wire.file import WireFile
from kimi_cli.wire.types import (
    LLMRequest,
    LLMToolSchema,
    LLMToolsSnapshot,
    MCPToolsDiscovered,
    WireMessage,
)


class _StaticStreamedMessage:
    def __init__(self, parts: Sequence[StreamedMessagePart]) -> None:
        self._iter = self._to_stream(parts)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> StreamedMessagePart:
        return await self._iter.__anext__()

    async def _to_stream(
        self, parts: Sequence[StreamedMessagePart]
    ) -> AsyncIterator[StreamedMessagePart]:
        for part in parts:
            yield part

    @property
    def id(self) -> str | None:
        return "static"

    @property
    def usage(self) -> TokenUsage | None:
        return None


class _StaticProvider:
    name = "static"

    @property
    def model_name(self) -> str:
        return "static-model"

    @property
    def thinking_effort(self) -> ThinkingEffort | None:
        return None

    async def generate(
        self,
        system_prompt: str,
        tools: Sequence[Tool],
        history: Sequence[Message],
    ) -> _StaticStreamedMessage:
        return _StaticStreamedMessage([TextPart(text="summary")])

    def with_thinking(self, effort: ThinkingEffort) -> Self:
        return self


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[WireMessage]:
    captured: list[WireMessage] = []
    monkeypatch.setattr("kimi_cli.soul.wire_send", captured.append)
    return captured


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"tool {name}",
        parameters={"type": "object", "properties": {}},
    )


def _history(n: int = 1) -> list[Message]:
    return [Message(role="user", content=[TextPart(text=f"msg {i}")]) for i in range(n)]


async def test_snapshot_once_and_request_per_call(sent: list[WireMessage]):
    recorder = LLMRequestRecorder()
    provider = _StaticProvider()
    tools = [_tool("a"), _tool("b")]

    recorder.record(provider, "prompt", tools, _history(2), turn_step=1, attempt=1)
    recorder.record(provider, "prompt", tools, _history(3), turn_step=2, attempt=1)

    snapshots = [m for m in sent if isinstance(m, LLMToolsSnapshot)]
    requests = [m for m in sent if isinstance(m, LLMRequest)]
    assert len(snapshots) == 1
    assert len(requests) == 2
    assert [t.name for t in snapshots[0].tools] == ["a", "b"]
    assert requests[0].tools_hash == snapshots[0].hash
    assert requests[1].tools_hash == snapshots[0].hash
    assert requests[0].kind == "loop"
    assert requests[0].provider == "static"
    assert requests[0].model == "static-model"
    assert requests[0].message_count == 2
    assert requests[1].message_count == 3
    assert requests[0].turn_step == 1
    assert requests[1].turn_step == 2
    # System prompt inlined only on the first occurrence of its hash.
    assert requests[0].system_prompt == "prompt"
    assert requests[1].system_prompt is None
    assert requests[0].system_prompt_hash == requests[1].system_prompt_hash


async def test_attempt_increments_across_retries(sent: list[WireMessage]):
    recorder = LLMRequestRecorder()
    provider = _StaticProvider()
    tools = [_tool("a")]

    recorder.record(provider, "p", tools, _history(), turn_step=1, attempt=1)
    recorder.record(provider, "p", tools, _history(), turn_step=1, attempt=2)

    requests = [m for m in sent if isinstance(m, LLMRequest)]
    assert [r.attempt for r in requests] == [1, 2]
    # Retries do not produce duplicate durable snapshots.
    assert len([m for m in sent if isinstance(m, LLMToolsSnapshot)]) == 1


async def test_new_snapshot_when_tool_table_changes(sent: list[WireMessage]):
    recorder = LLMRequestRecorder()
    provider = _StaticProvider()

    recorder.record(provider, "p", [_tool("a")], _history())
    recorder.record(provider, "p", [_tool("a"), _tool("b")], _history())

    snapshots = [m for m in sent if isinstance(m, LLMToolsSnapshot)]
    requests = [m for m in sent if isinstance(m, LLMRequest)]
    assert len(snapshots) == 2
    assert snapshots[0].hash != snapshots[1].hash
    assert requests[0].tools_hash == snapshots[0].hash
    assert requests[1].tools_hash == snapshots[1].hash


async def test_new_prompt_hash_inlines_prompt_again(sent: list[WireMessage]):
    recorder = LLMRequestRecorder()
    provider = _StaticProvider()
    tools = [_tool("a")]

    recorder.record(provider, "prompt one", tools, _history())
    recorder.record(provider, "prompt two", tools, _history())

    requests = [m for m in sent if isinstance(m, LLMRequest)]
    assert requests[0].system_prompt == "prompt one"
    assert requests[1].system_prompt == "prompt two"
    assert requests[0].system_prompt_hash != requests[1].system_prompt_hash


async def test_restore_from_resumed_session(
    tmp_path: Path, sent: list[WireMessage], monkeypatch: pytest.MonkeyPatch
):
    provider = _StaticProvider()
    tools = [_tool("a")]

    # First run: record into a wire file (via the captured wire_send).
    first = LLMRequestRecorder()
    first.record(provider, "prompt", tools, _history())
    first.record_mcp_discovery(
        "srv", [LLMToolSchema(name="t", description="d", parameters={})], ["t"], []
    )
    wire_file = WireFile(path=tmp_path / "wire.jsonl")
    for msg in sent:
        await wire_file.append_message(msg)
    sent.clear()

    # Resumed run: restore dedup cursors from the existing wire.jsonl.
    resumed = LLMRequestRecorder()
    await resumed.restore_from(wire_file)
    resumed.record(provider, "prompt", tools, _history())
    resumed.record_mcp_discovery(
        "srv", [LLMToolSchema(name="t", description="d", parameters={})], ["t"], []
    )

    # No durable snapshot or MCP discovery is re-logged; the prompt is not re-inlined.
    assert [type(m) for m in sent] == [LLMRequest]
    request = sent[0]
    assert isinstance(request, LLMRequest)
    assert request.system_prompt is None


async def test_kimi_max_tokens_default_and_explicit(sent: list[WireMessage]):
    from kosong.chat_provider.kimi import Kimi

    recorder = LLMRequestRecorder()
    tools = [_tool("a")]

    kimi = Kimi(model="kimi-test", api_key="test-key")
    recorder.record(kimi, "p", tools, _history())

    kimi_capped = kimi.with_generation_kwargs(max_tokens=1234, temperature=0.6, top_p=0.9)
    recorder.record(kimi_capped, "p", tools, _history())

    requests = [m for m in sent if isinstance(m, LLMRequest)]
    assert requests[0].provider == "kimi"
    assert requests[0].max_tokens == _KIMI_DEFAULT_MAX_TOKENS
    assert requests[0].temperature is None
    assert requests[1].max_tokens == 1234
    assert requests[1].temperature == 0.6
    assert requests[1].top_p == 0.9


async def test_cancelled_before_call_records_nothing(sent: list[WireMessage]):
    recorder = LLMRequestRecorder()
    provider = _StaticProvider()

    async def body() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        recorder.record(provider, "p", [_tool("a")], _history())
        task.uncancel()

    await asyncio.create_task(body())
    assert sent == []


async def test_compaction_records_kind_and_dropped_count(sent: list[WireMessage]):
    from kimi_cli.llm import LLM

    provider = _StaticProvider()
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    messages = [
        Message(role="system", content=[TextPart(text="System note")]),
        Message(role="user", content=[TextPart(text="Old question")]),
        Message(role="assistant", content=[TextPart(text="Old answer")]),
        Message(role="user", content=[TextPart(text="Latest question")]),
        Message(role="assistant", content=[TextPart(text="Latest answer")]),
    ]
    recorder = LLMRequestRecorder()

    result = await SimpleCompaction(max_preserved_messages=2).compact(
        messages, llm, recorder=recorder
    )
    assert result.messages

    requests = [m for m in sent if isinstance(m, LLMRequest)]
    assert len(requests) == 1
    assert requests[0].kind == "compaction"
    # 5 messages, 3 preserved (first + last 2) -> 2 dropped into the summary.
    assert requests[0].dropped_count == 2
    assert requests[0].turn_step is None
    assert requests[0].message_count == 1
    # The (empty) compaction tool table gets its own snapshot.
    snapshots = [m for m in sent if isinstance(m, LLMToolsSnapshot)]
    assert len(snapshots) == 1
    assert snapshots[0].tools == []


async def test_compaction_without_recorder_records_nothing(sent: list[WireMessage]):
    from kimi_cli.llm import LLM

    provider = _StaticProvider()
    llm = LLM(chat_provider=provider, max_context_size=100_000, capabilities=set())
    messages = [
        Message(role="system", content=[TextPart(text="System note")]),
        Message(role="user", content=[TextPart(text="Old question")]),
        Message(role="assistant", content=[TextPart(text="Old answer")]),
        Message(role="user", content=[TextPart(text="Latest question")]),
        Message(role="assistant", content=[TextPart(text="Latest answer")]),
    ]

    await SimpleCompaction(max_preserved_messages=2).compact(messages, llm)
    assert sent == []


async def test_mcp_discovery_recorded_once_and_deduped(sent: list[WireMessage]):
    recorder = LLMRequestRecorder()
    tools = [
        LLMToolSchema(name="t1", description="d1", parameters={"type": "object"}),
        LLMToolSchema(name="t2", description="d2", parameters={"type": "object"}),
    ]

    recorder.record_mcp_discovery("srv", tools, ["t1", "t2"], [])
    # Unchanged re-registration dedups.
    recorder.record_mcp_discovery("srv", tools, ["t1", "t2"], [])

    records = [m for m in sent if isinstance(m, MCPToolsDiscovered)]
    assert len(records) == 1
    assert records[0].server_name == "srv"
    assert [t.name for t in records[0].tools] == ["t1", "t2"]
    assert records[0].enabled_names == ["t1", "t2"]
    assert records[0].collisions == []

    # A collision-outcome-only change produces a new record.
    recorder.record_mcp_discovery("srv", tools, ["t1"], ["t2"])
    records = [m for m in sent if isinstance(m, MCPToolsDiscovered)]
    assert len(records) == 2
    assert records[1].enabled_names == ["t1"]
    assert records[1].collisions == ["t2"]
    assert records[0].hash != records[1].hash


async def test_parked_mcp_discovery_drained_and_deduped(sent: list[WireMessage]):
    toolset = KimiToolset()
    discovery = PendingMCPDiscovery(
        server_name="srv",
        tools=[LLMToolSchema(name="t", description="d", parameters={})],
        enabled_names=["t"],
        collisions=[],
    )
    toolset._pending_mcp_discoveries.append(discovery)

    recorder = LLMRequestRecorder()
    for pending in toolset.drain_pending_mcp_discoveries():
        recorder.record_mcp_discovery(
            pending.server_name, pending.tools, pending.enabled_names, pending.collisions
        )
    # Draining clears the parking list.
    assert toolset.drain_pending_mcp_discoveries() == []

    records = [m for m in sent if isinstance(m, MCPToolsDiscovered)]
    assert len(records) == 1

    # A duplicate parked discovery (e.g. after reconnect) is deduped at drain.
    toolset._pending_mcp_discoveries.append(discovery)
    for pending in toolset.drain_pending_mcp_discoveries():
        recorder.record_mcp_discovery(
            pending.server_name, pending.tools, pending.enabled_names, pending.collisions
        )
    assert len([m for m in sent if isinstance(m, MCPToolsDiscovered)]) == 1
