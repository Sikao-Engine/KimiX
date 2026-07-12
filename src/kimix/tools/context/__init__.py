"""Tools for introspecting and compacting conversation context."""
from __future__ import annotations

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from kimi_cli.soul import get_current_soul_or_none
from kimi_cli.soul.compaction import CompactMode
from kimi_cli.soul.kimisoul import KimiSoul
from pydantic import BaseModel, Field


class ContextUsageParams(BaseModel):
    """No parameters required."""

    pass


class ContextUsage(CallableTool2):
    name = "ContextUsage"
    description = (
        "Report the current conversation context usage: percentage, used tokens, "
        "and maximum context size. Use this to decide whether to call Compact."
    )
    params = ContextUsageParams

    async def __call__(self, params: ContextUsageParams) -> ToolReturnValue:
        soul = get_current_soul_or_none()
        if soul is None:
            return ToolError(
                message="No active soul/session.",
                output="",
                brief="No active session",
            )
        status = soul.status
        return ToolOk(
            output=(
                f"Context usage: {status.context_usage:.1%} "
                f"({status.context_tokens:,} / {status.max_context_tokens:,} tokens)"
            )
        )


class CompactParams(BaseModel):
    instruction: str | None = Field(
        default=None,
        description="Optional instruction guiding what to preserve during compaction.",
    )
    mode: CompactMode = Field(
        default=CompactMode.BALANCED,
        description=(
            "High-level compaction style / emphasis. "
            "One of: balanced (default structured summary), aggressive (shorter), "
            "retentive (keep more detail), technical (emphasize code/errors/design). "
            "Does not affect preserve depth or cascade behavior."
        ),
    )


class Compact(CallableTool2):
    name = "Compact"
    description = (
        "Compact / summarize the conversation context to reduce token usage. "
        "Call this when ContextUsage shows usage is high and you want to free up context. "
        "Optionally pass an instruction and a compaction mode (balanced, aggressive, "
        "retentive, technical) to control the summary style."
    )
    params = CompactParams

    async def __call__(self, params: CompactParams) -> ToolReturnValue:
        soul = get_current_soul_or_none()
        if not isinstance(soul, KimiSoul):
            return ToolError(
                message="No active KimiSoul to compact.",
                output="",
                brief="No active soul",
            )
        try:
            await soul.compact_context(
                manual=True,
                custom_instruction=params.instruction or "",
                avoid_cascade=True,
                mode=params.mode,
            )
        except Exception as exc:
            return ToolError(
                message=str(exc),
                output="",
                brief="Compaction failed",
            )
        return ToolOk(output='Compaction success. [WARNING] DO NOT call `Compact` frequently.')
