"""Memory system tool interface using CallableTool2 pattern."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Awaitable, Callable, TypeVar

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryType

T = TypeVar("T")

_memory_system: AgentMemorySystem | None = None
_init_lock = asyncio.Lock()


async def _get_memory_system() -> AgentMemorySystem:
    global _memory_system
    if _memory_system is None:
        async with _init_lock:
            if _memory_system is None:
                _memory_system = AgentMemorySystem()
    return _memory_system


def _run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> Awaitable[T]:
    return asyncio.get_running_loop().run_in_executor(None, partial(func, *args, **kwargs))


class RememberParams(BaseModel):
    content: str = Field(description="Content to store.")
    importance: float = Field(default=5.0, ge=0.0, le=10.0, description="Importance (0-10).")
    tags: list[str] = Field(default_factory=list, description="Categorization tags.")
    memory_type: MemoryType = Field(default=MemoryType.SEMANTIC, description="Memory type.")
    long_term: bool = Field(default=True, description="L3 if True, else L1+L2.")
    expires_at: float | None = Field(default=None, description="Absolute expiry timestamp.")


class Remember(CallableTool2):
    name: str = "Remember"
    description: str = "Store a fact, observation, or knowledge in memory."
    params: type[BaseModel] = RememberParams

    async def __call__(self, params: RememberParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            if params.long_term:
                entry = await _run_sync(
                    memory.remember,
                    params.content,
                    params.importance,
                    params.tags,
                    params.memory_type,
                    params.expires_at,
                )
                return ToolOk(output=f"Remembered: {entry.content[:100]}... (importance: {entry.importance})")
            else:
                entry = await _run_sync(
                    memory.perceive,
                    params.content,
                    params.importance,
                    params.tags,
                    "environment",
                    params.expires_at,
                )
                return ToolOk(output=f"Perceived: {entry.content[:100]}... (importance: {entry.importance})")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to store memory")


class RecallParams(BaseModel):
    query: str = Field(description="Search query.")
    context_size: int = Field(default=5, ge=1, le=20, description="Results per tier.")
    use_working: bool = Field(default=True, description="Include working memory.")
    use_short: bool = Field(default=True, description="Include short-term memory.")
    use_long: bool = Field(default=True, description="Include long-term memory.")
    tags: list[str] = Field(default_factory=list, description="Tags to filter long-term memory.")


class Recall(CallableTool2):
    name: str = "Recall"
    description: str = "Retrieve memories from all tiers."
    params: type[BaseModel] = RecallParams

    async def __call__(self, params: RecallParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            results = await _run_sync(
                memory.recall,
                params.query,
                params.context_size,
                params.use_working,
                params.use_short,
                params.use_long,
                params.tags or None,
            )
            output_parts: list[str] = []
            for tier in ("working", "short_term", "long_term"):
                entries = results[tier]
                if not entries:
                    continue
                output_parts.append(f"\n=== {tier.upper()} ===")
                output_parts.extend(f"- [{e.memory_type.value}] {e.content}" for e in entries)
            output = "\n".join(output_parts) if output_parts else "No memories found."
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to recall memories")


class GetContextParams(BaseModel):
    query: str = Field(description="Query for context generation.")
    max_tokens: int = Field(default=2000, ge=100, le=8000, description="Max characters for context.")


class GetContext(CallableTool2):
    name: str = "GetContext"
    description: str = "Generate context prompt from all memory tiers."
    params: type[BaseModel] = GetContextParams

    async def __call__(self, params: GetContextParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            context = await _run_sync(memory.get_context_for_llm, params.query, params.max_tokens)
            return ToolOk(output=context)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to generate context")


class ReflectParams(BaseModel):
    deep: bool = Field(default=False, description="Run deep self-reflection.")


class Reflect(CallableTool2):
    name: str = "Reflect"
    description: str = "Memory system status report. Optionally runs self-reflection."
    params: type[BaseModel] = ReflectParams

    async def __call__(self, params: ReflectParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            if params.deep:
                report = memory.self_reflect()
            else:
                report = memory.reflect()
            return ToolOk(output=report)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to reflect on memory")
