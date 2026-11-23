from __future__ import annotations

from typing import Literal

from kosong.message import Message
from pydantic import BaseModel, ConfigDict


class SystemPromptRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal["_system_prompt"]
    content: str


class UsageRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal["_usage"]
    token_count: int
    # Legacy input omitted the mapping and acquired its replay position.
    message_count: int | None = None


class CheckpointRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal["_checkpoint"]
    id: int
    # Legacy input omitted the mapping and acquired its replay position.
    message_count: int | None = None


class ResetRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal["_reset"]


class RevertRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    role: Literal["_revert"]
    id: int


class ExportedContext(BaseModel):
    system_prompt: str | None = None
    messages: list[Message] = []
    checkpoints: list[int] = []
    usages: list[int] = []
