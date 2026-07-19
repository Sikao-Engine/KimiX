"""Tests for kimi_agent_sdk.Session.export() — JSONL and Markdown formats."""

from __future__ import annotations

import json as stdjson
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import orjson
import pendulum
import pytest
from kaos.path import KaosPath
from kosong.message import Message, TextPart, ThinkPart, ToolCall

from kimi_agent_sdk._session import ExportFormat, Session


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeContext:
    history: list[Message]
    token_count: int = 100


@dataclass
class _FakeSoul:
    context: _FakeContext


@dataclass
class _FakeCLISession:
    id: str = "test-session-id-12345678"
    work_dir: KaosPath | Path = field(default_factory=lambda: KaosPath("/tmp/work"))


@dataclass
class _FakeCLI:
    soul: _FakeSoul
    session: _FakeCLISession


def _make_session(history: list[Message] | None = None) -> Session:
    """Create a Session with fake internals for testing export()."""
    messages = history or []
    context = _FakeContext(history=messages, token_count=100)
    soul = _FakeSoul(context=context)
    cli_session = _FakeCLISession()
    cli = _FakeCLI(soul=soul, session=cli_session)
    return Session(cli)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    """Parse a JSONL string into a list of dicts."""
    lines = text.strip().split("\n")
    return [orjson.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_jsonl_basic(tmp_path: Path) -> None:
    """Export session with messages -> verify .jsonl structure."""
    history = [
        Message(role="user", content=[TextPart(text="Hello")]),
        Message(role="assistant", content=[TextPart(text="Hi there!")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, count = await session.export(format=ExportFormat.Jsonl)

    assert out_path.suffix == ".jsonl"
    assert count == 2

    content = out_path.read_text(encoding="utf-8")
    records = _parse_jsonl(content)
    assert len(records) == 3  # metadata + 2 messages

    # Line 1: metadata
    meta = records[0]
    assert meta["type"] == "session_metadata"
    assert meta["session_id"] == "test-session-id-12345678"
    assert "exported_at" in meta
    assert meta["message_count"] == 2
    assert meta["token_count"] == 100

    # Lines 2+: messages
    assert records[1]["role"] == "user"
    assert records[2]["role"] == "assistant"


@pytest.mark.asyncio
async def test_export_jsonl_no_messages(tmp_path: Path) -> None:
    """Export with empty history -> ValueError."""
    session = _make_session([])
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    with pytest.raises(ValueError, match="No messages to export"):
        await session.export()


@pytest.mark.asyncio
async def test_export_jsonl_custom_path(tmp_path: Path) -> None:
    """Export to a custom path."""
    history = [
        Message(role="user", content=[TextPart(text="Hello")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    custom = tmp_path / "my-export.jsonl"
    out_path, count = await session.export(str(custom), format=ExportFormat.Jsonl)

    assert out_path == custom
    assert out_path.exists()
    assert out_path.suffix == ".jsonl"
    assert count == 1


@pytest.mark.asyncio
async def test_export_jsonl_custom_directory(tmp_path: Path) -> None:
    """Export to a custom directory -> file created inside with default name."""
    history = [
        Message(role="user", content=[TextPart(text="Hello")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_dir = tmp_path / "exports"
    out_dir.mkdir()
    out_path, count = await session.export(str(out_dir), format=ExportFormat.Jsonl)

    assert out_path.parent == out_dir
    assert out_path.suffix == ".jsonl"
    assert out_path.name.startswith("kimi-export-")
    assert count == 1


@pytest.mark.asyncio
async def test_export_jsonl_internal_messages_filtered(tmp_path: Path) -> None:
    """Internal messages (checkpoint, system reminder) should be excluded."""
    history = [
        # A checkpoint message – should be filtered
        Message(role="user", content=[TextPart(text="<system>CHECKPOINT")]),
        Message(role="user", content=[TextPart(text="Real question")]),
        Message(role="assistant", content=[TextPart(text="Answer")]),
        # A system-reminder message – should also be filtered
        Message(role="user", content=[TextPart(text="<system-reminder>some context")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, count = await session.export(format=ExportFormat.Jsonl)

    content = out_path.read_text(encoding="utf-8")
    records = _parse_jsonl(content)

    # metadata + 2 non-internal messages (Real question, Answer)
    assert len(records) == 3
    assert records[1]["role"] == "user"
    assert records[2]["role"] == "assistant"


@pytest.mark.asyncio
async def test_export_jsonl_with_tool_calls(tmp_path: Path) -> None:
    """Messages with tool_calls should be serialized correctly."""
    tool_call = ToolCall(
        id="call_1",
        type="function",
        function={"name": "read_file", "arguments": '{"path": "test.py"}'},
    )
    history = [
        Message(role="user", content=[TextPart(text="Read file")]),
        Message(
            role="assistant",
            content=[ThinkPart(think="Let me check..."), TextPart(text="Here it is:")],
            tool_calls=[tool_call],
        ),
        Message(
            role="tool",
            content=[TextPart(text="file contents")],
            tool_call_id="call_1",
        ),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, count = await session.export(format=ExportFormat.Jsonl)

    content = out_path.read_text(encoding="utf-8")
    records = _parse_jsonl(content)

    assert len(records) == 4  # metadata + 3 messages
    assert records[1]["role"] == "user"
    assert records[2]["role"] == "assistant"
    assert records[2]["tool_calls"] is not None
    assert records[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert records[3]["role"] == "tool"
    assert records[3]["tool_call_id"] == "call_1"


@pytest.mark.asyncio
async def test_export_jsonl_each_line_valid_json(tmp_path: Path) -> None:
    """Every line of the output must be valid JSON."""
    history = [
        Message(role="user", content=[TextPart(text="msg1")]),
        Message(role="assistant", content=[TextPart(text="msg2")]),
        Message(role="user", content=[TextPart(text="msg3")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, _ = await session.export(format=ExportFormat.Jsonl)

    content = out_path.read_text(encoding="utf-8")
    for i, line in enumerate(content.strip().split("\n"), 1):
        try:
            stdjson.loads(line)
        except stdjson.JSONDecodeError as e:
            pytest.fail(f"Line {i} is not valid JSON: {e}")


@pytest.mark.asyncio
async def test_export_jsonl_trailing_newline(tmp_path: Path) -> None:
    """Output file should end with a trailing newline."""
    history = [
        Message(role="user", content=[TextPart(text="Hello")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, _ = await session.export(format=ExportFormat.Jsonl)

    content = out_path.read_bytes()
    assert content.endswith(b"\n"), "Output should end with a newline"


@pytest.mark.asyncio
async def test_export_jsonl_roundtrip(tmp_path: Path) -> None:
    """Export to JSONL, read back, verify messages can be round-tripped."""
    original = [
        Message(role="user", content=[TextPart(text="Hello")]),
        Message(
            role="assistant",
            content=[ThinkPart(think="Thinking..."), TextPart(text="Reply")],
        ),
        Message(
            role="tool",
            content=[TextPart(text="result data")],
            tool_call_id="call_xyz",
        ),
    ]
    session = _make_session(original)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, count = await session.export(format=ExportFormat.Jsonl)
    assert count == 3

    # Read back and parse
    content = out_path.read_text(encoding="utf-8")
    lines = content.strip().split("\n")
    assert len(lines) == 4  # metadata + 3 messages

    # Validate each message line can be reconstructed
    records = [orjson.loads(line) for line in lines]
    for rec in records[1:]:
        reconstructed = Message.model_validate(rec)
        assert isinstance(reconstructed, Message)
        assert reconstructed.role == rec["role"]


# ---------------------------------------------------------------------------
# Markdown format tests (default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_markdown_default(tmp_path: Path) -> None:
    """Default export format is Markdown -> .md file."""
    history = [
        Message(role="user", content=[TextPart(text="Hello")]),
        Message(role="assistant", content=[TextPart(text="Hi! to you!")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, count = await session.export()

    assert out_path.suffix == ".markdown"
    assert count == 2
    content = out_path.read_text(encoding="utf-8")
    assert "# Kimi Session Export" in content


@pytest.mark.asyncio
async def test_export_markdown_explicit(tmp_path: Path) -> None:
    """Pass ExportFormat.Markdown explicitly."""
    history = [
        Message(role="user", content=[TextPart(text="Hello")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, count = await session.export(format=ExportFormat.Markdown)

    assert out_path.suffix == ".markdown"
    assert count == 1
    content = out_path.read_text(encoding="utf-8")
    assert "# Kimi Session Export" in content


@pytest.mark.asyncio
async def test_export_markdown_internal_filtered(tmp_path: Path) -> None:
    """Markdown export also filters internal messages."""
    history = [
        Message(role="user", content=[TextPart(text="<system>CHECKPOINT")]),
        Message(role="user", content=[TextPart(text="Real question")]),
        Message(role="assistant", content=[TextPart(text="The answer.")]),
    ]
    session = _make_session(history)
    session._cli.session.work_dir = KaosPath.unsafe_from_local_path(tmp_path)

    out_path, count = await session.export(format=ExportFormat.Markdown)

    # count is total messages (unfiltered), content filters internal messages
    assert count == 3
    content = out_path.read_text(encoding="utf-8")
    assert "Real question" in content
    assert "The answer." in content
    assert "CHECKPOINT" not in content
