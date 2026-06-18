"""Context introspection and compaction tools."""
from __future__ import annotations

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from kimi_cli.soul import get_current_soul_or_none
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


class Compact(CallableTool2):
    name = "Compact"
    description = (
        "Compact / summarize the conversation context to reduce token usage. "
        "Call this when ContextUsage shows usage is high and you want to free up context."
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
        before = soul.status
        try:
            await soul.compact_context(
                manual=True,
                custom_instruction=params.instruction or "",
            )
        except Exception as exc:
            return ToolError(
                message=str(exc),
                output="",
                brief="Compaction failed",
            )
        after = soul.status
        return ToolOk(
            output=(
                f"Context compacted from {before.context_tokens:,} tokens "
                f"({before.context_usage:.1%}) to {after.context_tokens:,} tokens "
                f"({after.context_usage:.1%})."
            )
        )
