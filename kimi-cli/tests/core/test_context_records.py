"""Tests for context.jsonl pydantic record models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimi_cli.soul.context_records import (
    CheckpointRecord,
    ExportedContext,
    SystemPromptRecord,
    UsageRecord,
)


class TestSystemPromptRecord:
    def test_valid(self) -> None:
        record = SystemPromptRecord.model_validate(
            {"role": "_system_prompt", "content": "You are helpful."}
        )
        assert record.content == "You are helpful."

    def test_missing_content(self) -> None:
        with pytest.raises(ValidationError):
            SystemPromptRecord.model_validate({"role": "_system_prompt"})

    def test_wrong_role(self) -> None:
        with pytest.raises(ValidationError):
            SystemPromptRecord.model_validate({"role": "user", "content": "hello"})

    def test_extra_fields_allowed(self) -> None:
        record = SystemPromptRecord.model_validate(
            {"role": "_system_prompt", "content": "test", "extra": 1}
        )
        assert record.content == "test"


class TestUsageRecord:
    def test_valid(self) -> None:
        record = UsageRecord.model_validate({"role": "_usage", "token_count": 42})
        assert record.token_count == 42

    def test_missing_token_count(self) -> None:
        with pytest.raises(ValidationError):
            UsageRecord.model_validate({"role": "_usage"})

    def test_string_token_count(self) -> None:
        with pytest.raises(ValidationError):
            UsageRecord.model_validate({"role": "_usage", "token_count": "42"})


class TestCheckpointRecord:
    def test_valid(self) -> None:
        record = CheckpointRecord.model_validate({"role": "_checkpoint", "id": 0})
        assert record.id == 0

    def test_missing_id(self) -> None:
        with pytest.raises(ValidationError):
            CheckpointRecord.model_validate({"role": "_checkpoint"})

    def test_string_id(self) -> None:
        with pytest.raises(ValidationError):
            CheckpointRecord.model_validate({"role": "_checkpoint", "id": "0"})


class TestExportedContext:
    def test_defaults(self) -> None:
        ctx = ExportedContext()
        assert ctx.system_prompt is None
        assert ctx.messages == []
        assert ctx.checkpoints == []
        assert ctx.usages == []

    def test_populated(self) -> None:
        from kosong.message import Message

        from kimi_cli.wire.types import TextPart

        ctx = ExportedContext(
            system_prompt="hi",
            messages=[Message(role="user", content=[TextPart(text="hello")])],
            checkpoints=[0, 1],
            usages=[10, 20],
        )
        assert ctx.system_prompt == "hi"
        assert len(ctx.messages) == 1
        assert ctx.checkpoints == [0, 1]
        assert ctx.usages == [10, 20]
