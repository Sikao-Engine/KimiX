"""Append-only JSONL context storage and replay tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import orjson
import pytest
from kosong.message import Message, Role, ToolCall
from kosong.utils.jsonx import loads_relaxed

from kimi_cli.soul.compaction import estimate_text_tokens
from kimi_cli.soul.context import Context, JsonlContextStorage, replay_context
from kimi_cli.wire.types import TextPart


def _write_lines(path: Path, lines: list[dict]) -> None:
    path.write_bytes(b"".join(orjson.dumps(line, option=orjson.OPT_APPEND_NEWLINE) for line in lines))


def _read_lines(path: Path) -> list[dict]:
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def _message_dict(role: Role, text: str) -> dict:
    return Message(role=role, content=[TextPart(text=text)]).model_dump(
        mode="json", exclude_none=True
    )


def _msg(role: Role, text: str) -> Message:
    return Message(role=role, content=[TextPart(text=text)])


def _assistant_tool_call_dict(call_id: str, name: str = "some_tool") -> dict:
    return Message(
        role="assistant",
        content=[],
        tool_calls=[
            ToolCall(
                id=call_id,
                function=ToolCall.FunctionBody(name=name, arguments="{}"),
            )
        ],
    ).model_dump(mode="json", exclude_none=True)


def _tool_result_dict(call_id: str, text: str = "done") -> dict:
    return Message(role="tool", content=text, tool_call_id=call_id).model_dump(
        mode="json", exclude_none=True
    )


@pytest.mark.asyncio
async def test_prompt_appends_and_latest_active_prompt_wins(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(path, [_message_dict("user", "Hello")])
    original = path.read_bytes()
    ctx = Context(file_backend=path)
    await ctx.write_system_prompt("first prompt")
    after_first = path.read_bytes()
    await ctx.write_system_prompt("第二\nprompt")
    await ctx.close()

    assert after_first.startswith(original)
    assert path.read_bytes().startswith(after_first)
    lines = _read_lines(path)
    assert [line["role"] for line in lines] == ["user", "_system_prompt", "_system_prompt"]
    restored = Context(file_backend=path)
    assert await restored.restore()
    assert restored.system_prompt == "第二\nprompt"
    assert [m.extract_text() for m in restored.history] == ["Hello"]
    await restored.close()


@pytest.mark.asyncio
async def test_restore_all_record_types_and_logical_message_pagination(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            {"role": "_system_prompt", "content": "prompt"},
            _message_dict("user", "one"),
            {"role": "_usage", "token_count": 42},
            {"role": "_checkpoint", "id": 0},
            _message_dict("assistant", "two"),
            {"role": "_checkpoint", "id": 1},
        ],
    )
    ctx = Context(file_backend=path)
    await ctx.restore()
    assert ctx.system_prompt == "prompt"
    assert ctx.token_count == 42
    assert ctx.n_checkpoints == 2
    assert [m.extract_text() for m in ctx.history] == ["one", "two"]
    assert await ctx.storage.get_message_count() == 2
    assert (await ctx.storage.get_messages(after_rowid=1))[0].extract_text() == "two"
    assert (await ctx.storage.get_messages(limit=1))[0].extract_text() == "one"
    assert await ctx.storage.has_visible_messages()
    exported = await ctx.storage.export()
    assert exported.checkpoints == [0, 1]
    assert exported.usages == [42]
    await ctx.close()


@pytest.mark.asyncio
async def test_dense_unicode_multiline_tool_payload_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    messages = [
        Message(role="user", content="中文\nline two\tend"),
        Message(
            role="assistant",
            content=[],
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=ToolCall.FunctionBody(
                        name="ReadFile", arguments='{"path":"a.txt","line":"x"}'
                    ),
                )
            ],
        ),
        Message(role="tool", content=[TextPart(text="结果\nok")], tool_call_id="call-1"),
    ]
    ctx = Context(file_backend=path)
    await ctx.append_message(messages)
    await ctx.close()

    raw_lines = path.read_bytes().splitlines()
    assert len(raw_lines) == 3
    for raw in raw_lines:
        assert raw == orjson.dumps(orjson.loads(raw))
        assert b": " not in raw
        assert b"null" not in raw
        assert isinstance(orjson.loads(raw)["created_at"], float)
    restored = Context(file_backend=path)
    await restored.restore()
    assert [m.model_dump(mode="json", exclude_none=True) for m in restored.history] == [
        m.model_dump(mode="json", exclude_none=True) for m in messages
    ]
    await restored.close()


@pytest.mark.asyncio
async def test_replace_history_appends_reset_and_exports_only_active_view(
    tmp_path: Path,
) -> None:
    path = tmp_path / "context.jsonl"
    ctx = Context(file_backend=path)
    await ctx.write_system_prompt("prompt")
    await ctx.append_message(_msg("user", "old"))
    await ctx.update_token_count(100)
    await ctx.checkpoint(add_user_message=False)
    original = path.read_bytes()

    await ctx.replace_history([_msg("assistant", "new")])
    replaced_prefix = path.read_bytes()
    assert replaced_prefix.startswith(original)
    assert [r["role"] for r in _read_lines(path)][-3:] == ["_reset", "_system_prompt", "assistant"]
    exported = await ctx.storage.export()
    assert exported.system_prompt == "prompt"
    assert [m.extract_text() for m in exported.messages] == ["new"]
    assert exported.usages == []
    assert exported.checkpoints == []
    assert await ctx.storage.get_message_count() == 1

    await ctx.replace_history([])
    assert path.read_bytes().startswith(replaced_prefix)
    assert list(ctx.history) == []
    assert ctx.system_prompt == "prompt"
    active = await ctx.storage.export()
    assert active.messages == []
    assert active.system_prompt == "prompt"
    await ctx.close()

    replayed = replay_context(path)
    assert replayed.messages == []
    assert replayed.system_prompt == "prompt"


@pytest.mark.asyncio
async def test_clear_appends_reset_and_reopen_uses_active_view(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    ctx = Context(file_backend=path)
    await ctx.write_system_prompt("old prompt")
    await ctx.append_message(_msg("user", "old"))
    original = path.read_bytes()
    await ctx.clear()
    assert path.read_bytes() == original + b'{"role":"_reset"}\n'
    assert ctx.system_prompt is None
    assert list(ctx.history) == []
    assert await ctx.storage.get_latest_usage() is None
    assert await ctx.storage.get_latest_checkpoint_id() == -1
    await ctx.close()

    restored = Context(file_backend=path)
    assert not await restored.restore()
    assert restored.system_prompt is None
    assert list(restored.history) == []
    await restored.close()


@pytest.mark.asyncio
async def test_multiple_checkpoint_revert_generations_restore_state(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    ctx = Context(file_backend=path)
    await ctx.write_system_prompt("before")
    await ctx.append_message(_msg("user", "one"))
    await ctx.update_token_count(100)
    await ctx.checkpoint(add_user_message=False)  # 0
    await ctx.write_system_prompt("after")
    await ctx.append_message(_msg("assistant", "two"))
    await ctx.update_token_count(200)
    await ctx.checkpoint(add_user_message=False)  # 1
    await ctx.append_message(_msg("user", "three"))
    before_revert = path.read_bytes()

    await ctx.revert_to(1)
    assert path.read_bytes() == before_revert + b'{"role":"_revert","id":1}\n'
    assert [m.extract_text() for m in ctx.history] == ["one", "two"]
    assert ctx.system_prompt == "after"
    assert ctx.token_count == 200
    assert ctx.n_checkpoints == 1
    await ctx.revert_to(0)
    assert [m.extract_text() for m in ctx.history] == ["one"]
    assert ctx.system_prompt == "before"
    assert ctx.token_count == 100
    assert ctx.n_checkpoints == 0

    await ctx.checkpoint(add_user_message=True)
    assert ctx.n_checkpoints == 1
    await ctx.close()
    restored = Context(file_backend=path)
    await restored.restore()
    assert [m.extract_text() for m in restored.history] == [
        "one",
        "<system>CHECKPOINT 0</system>",
    ]
    assert restored.token_count == 100
    assert restored.n_checkpoints == 1
    await restored.close()


@pytest.mark.asyncio
async def test_unknown_revert_raises_before_appending(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    storage = JsonlContextStorage(path)
    await storage.initialize()
    with pytest.raises(ValueError, match="Checkpoint 99 not found"):
        await storage.revert_to_checkpoint(99)
    assert path.read_bytes() == b""
    await storage.close()


@pytest.mark.asyncio
async def test_legacy_usage_and_checkpoint_inherit_replay_position(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            _message_dict("user", "covered"),
            {"role": "_usage", "token_count": 50},
            {"role": "_checkpoint", "id": 4},
            _message_dict("assistant", "pending"),
        ],
    )
    ctx = Context(file_backend=path)
    await ctx.restore()
    assert ctx.token_count == 50
    assert ctx.n_checkpoints == 5
    assert ctx.token_count_with_pending == 50 + estimate_text_tokens([_msg("assistant", "pending")])
    await ctx.revert_to(4)
    assert ctx.token_count_with_pending == 50
    await ctx.close()


@pytest.mark.asyncio
async def test_reset_and_revert_restore_pending_and_usage(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    ctx = Context(file_backend=path)
    first = _msg("user", "a" * 400)
    second = _msg("assistant", "b" * 800)
    await ctx.append_message(first)
    await ctx.update_token_count(1000)
    await ctx.checkpoint(add_user_message=False)
    await ctx.append_message(second)
    await ctx.revert_to(0)
    assert ctx.token_count_with_pending == 1000
    await ctx.append_message(second)
    await ctx.clear()
    assert ctx.token_count_with_pending == 0
    await ctx.close()
    assert replay_context(path).pending_messages == []


@pytest.mark.asyncio
async def test_malformed_interior_and_incomplete_tail_keep_valid_records(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    path.write_bytes(
        orjson.dumps(_message_dict("user", "before"), option=orjson.OPT_APPEND_NEWLINE)
        + b'not json\n'
        + b'{"role":"assistant","content":\n'
        + orjson.dumps(_message_dict("assistant", "after"), option=orjson.OPT_APPEND_NEWLINE)
        + b'{"role":"user","content":'
    )
    original = path.read_bytes()
    ctx = Context(file_backend=path)
    await ctx.restore()
    assert [m.extract_text() for m in ctx.history] == ["before", "after"]
    assert path.read_bytes() == original
    await ctx.close()


@pytest.mark.asyncio
async def test_relaxed_legacy_line_and_truncated_utf8_are_read_only_compatible(
    tmp_path: Path,
) -> None:
    path = tmp_path / "context.jsonl"
    path.write_bytes(
        b'{"role":"user","content":"legacy",}\n'
        + b'{"role":"assistant","content":"\xe4\xb8\n'
    )
    original = path.read_bytes()
    ctx = Context(file_backend=path)
    await ctx.restore()
    assert [m.extract_text() for m in ctx.history] == ["legacy"]
    assert path.read_bytes() == original
    await ctx.close()


@pytest.mark.asyncio
async def test_malformed_replayed_revert_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            _message_dict("user", "kept"),
            {"role": "_revert", "id": 99},
            _message_dict("assistant", "also kept"),
        ],
    )
    state = replay_context(path)
    assert [m.extract_text() for m in state.messages] == ["kept", "also kept"]


@pytest.mark.asyncio
async def test_restore_repairs_interleaved_tool_pairing_without_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            _message_dict("user", "run the checks"),
            _assistant_tool_call_dict("c1"),
            _message_dict("user", "Background subagent completed."),
            _tool_result_dict("c1"),
        ],
    )
    original = path.read_bytes()
    ctx = Context(file_backend=path)
    await ctx.restore()
    assert [m.role for m in ctx.history] == ["user", "assistant", "tool", "user"]
    assert ctx.history[2].tool_call_id == "c1"
    assert path.read_bytes() == original
    await ctx.close()


@pytest.mark.asyncio
async def test_restore_synthesizes_missing_result_and_drops_orphan(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [_tool_result_dict("ghost"), _message_dict("user", "hi"), _assistant_tool_call_dict("c1")],
    )
    ctx = Context(file_backend=path)
    await ctx.restore()
    assert [m.role for m in ctx.history] == ["user", "assistant", "tool"]
    assert ctx.history[-1].tool_call_id == "c1"
    assert "interrupted" in ctx.history[-1].extract_text()
    await ctx.close()


@pytest.mark.asyncio
async def test_restore_leaves_valid_tool_sequence_untouched(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    _write_lines(
        path,
        [
            _message_dict("user", "hi"),
            _assistant_tool_call_dict("c1"),
            _tool_result_dict("c1", "result text"),
            _message_dict("assistant", "all done"),
        ],
    )
    ctx = Context(file_backend=path)
    await ctx.restore()
    assert [m.role for m in ctx.history] == ["user", "assistant", "tool", "assistant"]
    assert ctx.history[2].extract_text() == "result text"
    await ctx.close()


@pytest.mark.asyncio
async def test_replace_history_token_count_contract(tmp_path: Path) -> None:
    ctx = Context(file_backend=tmp_path / "context.jsonl")
    await ctx.append_message(_msg("user", "a" * 400))
    await ctx.update_token_count(10_000)
    replacement = _msg("user", "b" * 100)
    await ctx.replace_history([replacement])
    assert ctx.token_count == estimate_text_tokens([replacement])
    assert ctx.token_count_with_pending == ctx.token_count

    no_usage = Context(file_backend=tmp_path / "other.jsonl")
    await no_usage.append_message(_msg("user", "a" * 100))
    await no_usage.replace_history([_msg("user", "b" * 400)])
    assert no_usage.token_count == 0
    assert no_usage.token_count_with_pending == 0
    await ctx.close()
    await no_usage.close()


@pytest.mark.asyncio
async def test_message_metadata_uses_logical_rowids_and_timestamps(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"
    ctx = Context(file_backend=path)
    await ctx.write_system_prompt("not a message")
    await ctx.append_message([_msg("user", "one"), _msg("assistant", "two")])
    rows = await ctx.storage.get_messages_with_meta()  # type: ignore[attr-defined]
    assert [row["rowid"] for row in rows] == [1, 2]
    assert all(row["created_at"] > 0 for row in rows)
    assert all(row["content"] == orjson.dumps(orjson.loads(row["content"])).decode() for row in rows)
    await ctx.close()


def test_context_rejects_direct_sqlite_runtime_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be migrated"):
        Context.from_file_backend(tmp_path / "context.db")


def test_close_reopen_and_operations_across_successive_event_loops(tmp_path: Path) -> None:
    path = tmp_path / "context.jsonl"

    async def first() -> None:
        ctx = Context(file_backend=path)
        await ctx.append_message(_msg("user", "one"))
        await ctx.close()
        await ctx.close()

    async def second() -> None:
        ctx = Context(file_backend=path)
        await ctx.restore()
        await ctx.append_message(_msg("assistant", "two"))
        await ctx.close()

    asyncio.run(first())
    asyncio.run(second())
    assert [m.extract_text() for m in replay_context(path).messages] == ["one", "two"]


def test_relaxed_reader_fixture_remains_available() -> None:
    assert loads_relaxed('{"role":"user","content":"x",}')["content"] == "x"
