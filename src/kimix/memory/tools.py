"""Memory system tool interface using CallableTool2 pattern."""

from __future__ import annotations

import asyncio
from typing import Any

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryType


# Global memory system instance (initialized lazily)
_memory_system: AgentMemorySystem | None = None
_init_lock = asyncio.Lock()


async def _get_memory_system() -> AgentMemorySystem:
    """Get or initialize the global memory system instance."""
    global _memory_system
    if _memory_system is None:
        async with _init_lock:
            if _memory_system is None:
                _memory_system = AgentMemorySystem()
    return _memory_system


class RememberParams(BaseModel):
    """Parameters for remembering a fact or observation."""
    content: str = Field(description="Content to store.")
    importance: float = Field(default=5.0, ge=0.0, le=10.0, description="Importance (0-10).")
    tags: list[str] = Field(default_factory=list, description="Categorization tags.")
    memory_type: MemoryType = Field(default=MemoryType.SEMANTIC, description="Memory type.")
    long_term: bool = Field(default=True, description="L3 if True, else L1+L2.")
    expires_at: float | None = Field(default=None, description="Absolute expiry timestamp.")


class Remember(CallableTool2):
    """Store a fact or observation in memory."""
    name: str = "Remember"
    description: str = "Store a fact, observation, or knowledge in memory."
    params: type[BaseModel] = RememberParams
    def __init__(self):
        super().__init__(self.name, self.description, self.params)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_get_memory_system())
        except RuntimeError:
            asyncio.run(_get_memory_system())

    async def __call__(self, params: RememberParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            if params.long_term:
                entry = memory.remember(
                    fact=params.content,
                    importance=params.importance,
                    tags=params.tags,
                    memory_type=params.memory_type,
                    expires_at=params.expires_at,
                )
                return ToolOk(output=f"Remembered: {entry.content[:100]}... (importance: {entry.importance})")
            else:
                entry = memory.perceive(
                    observation=params.content,
                    importance=params.importance,
                    tags=params.tags,
                    expires_at=params.expires_at,
                )
                return ToolOk(output=f"Perceived: {entry.content[:100]}... (importance: {entry.importance})")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to store memory")


class RecallParams(BaseModel):
    """Parameters for recalling memories."""
    query: str = Field(description="Search query.")
    context_size: int = Field(default=5, ge=1, le=20, description="Results per tier.")
    use_working: bool = Field(default=True, description="Include working memory.")
    use_short: bool = Field(default=True, description="Include short-term memory.")
    use_long: bool = Field(default=True, description="Include long-term memory.")
    use_procedural: bool = Field(default=False, description="Include procedural memory.")
    tags: list[str] = Field(default_factory=list, description="Tags to filter long-term memory.")


class Recall(CallableTool2):
    """Recall memories from all tiers."""
    name: str = "Recall"
    description: str = "Retrieve memories from all tiers."
    params: type[BaseModel] = RecallParams

    async def __call__(self, params: RecallParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            results = memory.recall(
                query=params.query,
                context_size=params.context_size,
                use_working=params.use_working,
                use_short=params.use_short,
                use_long=params.use_long,
                use_procedural=params.use_procedural,
                tag_filter=params.tags or None,
            )
            output_parts: list[str] = []
            append = output_parts.append
            for tier, entries in results.items():
                if entries:
                    append(f"\n=== {tier.upper()} ===")
                    for e in entries:
                        append(f"- [{e.memory_type.value}] {e.content}")
            output = "\n".join(output_parts) if output_parts else "No memories found."
            return ToolOk(output=output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to recall memories")


class GetContextParams(BaseModel):
    """Parameters for LLM context."""
    query: str = Field(description="Query for context generation.")
    max_tokens: int = Field(default=2000, ge=100, le=8000, description="Max characters for context.")


class GetContext(CallableTool2):
    """Generate RAG-style context for LLM."""
    name: str = "GetContext"
    description: str = "Generate context prompt from all memory tiers."
    params: type[BaseModel] = GetContextParams

    async def __call__(self, params: GetContextParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            context = memory.get_context_for_llm(params.query, max_tokens=params.max_tokens)
            return ToolOk(output=context)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to generate context")


class ReflectParams(BaseModel):
    """Parameters for memory reflection."""
    deep: bool = Field(default=False, description="Run deep self-reflection.")


class Reflect(CallableTool2):
    """Get memory status report."""
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


class AddScarParams(BaseModel):
    """Parameters for recording a scar."""
    failure_pattern: str = Field(description="Failure pattern.")
    lesson: str = Field(description="Lesson learned.")
    trigger_conditions: list[str] = Field(default_factory=list, description="Trigger keywords.")
    severity: float = Field(default=5.0, ge=0.0, le=10.0, description="Severity (0-10).")


class AddScar(CallableTool2):
    """Record a failure experience in procedural memory."""
    name: str = "AddScar"
    description: str = "Record a failure to avoid repeating it."
    params: type[BaseModel] = AddScarParams

    async def __call__(self, params: AddScarParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            memory.add_scar(
                failure_pattern=params.failure_pattern,
                lesson=params.lesson,
                trigger_conditions=params.trigger_conditions,
                severity=params.severity,
            )
            return ToolOk(output=f"Scar recorded: {params.failure_pattern[:80]}...")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to record scar")


class AddRuleParams(BaseModel):
    """Parameters for adding an operational rule."""
    condition: str = Field(description="Rule condition.")
    action: str = Field(description="Action to take.")
    priority: float = Field(default=5.0, ge=0.0, le=10.0, description="Priority (0-10).")
    tags: list[str] = Field(default_factory=list, description="Rule tags.")


class AddRule(CallableTool2):
    """Add an operational rule to procedural memory."""
    name: str = "AddRule"
    description: str = "Add a decision rule to procedural memory."
    params: type[BaseModel] = AddRuleParams

    async def __call__(self, params: AddRuleParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            memory.add_rule(
                condition=params.condition,
                action=params.action,
                priority=params.priority,
                tags=params.tags,
            )
            return ToolOk(output=f"Rule recorded: IF {params.condition[:40]}... THEN {params.action[:40]}...")
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to record rule")
