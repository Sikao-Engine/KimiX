"""Memory system tool interface using CallableTool2 pattern."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Awaitable, Callable, TypeVar

from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryType
from kimix.tools.common import _maybe_export_output_async
from kimix.utils import close_session_async, _create_session_async, prompt_async
from kimix.utils.system_prompt import SystemPromptType
from kimi_cli.session import Session

T = TypeVar("T")

_memory_system: AgentMemorySystem | None = None
_init_lock = asyncio.Lock()


async def _get_memory_system() -> AgentMemorySystem:
    global _memory_system
    if _memory_system is None:
        async with _init_lock:
            if _memory_system is None:
                _memory_system = AgentMemorySystem(use_sqlite=True)
    return _memory_system


def _run_sync(func: Callable[..., T], *args: Any, **kwargs: Any) -> Awaitable[T]:
    return asyncio.get_running_loop().run_in_executor(None, partial(func, *args, **kwargs))


class RememberParams(BaseModel):
    content: str = Field(description="Content to store.")
    importance: float = Field(default=5.0, ge=0.0, le=10.0, description="Importance (0-10).")
    tags: list[str] = Field(default_factory=list, description="Categorization tags.")
    memory_type: MemoryType = Field(default=MemoryType.SEMANTIC, description="Memory type.")
    long_term: bool = Field(default=True, description="Store in long-term memory if True; otherwise short-term/working memory.")
    expires_at: float | None = Field(default=None, description="Absolute expiry timestamp.")


class Remember(CallableTool2):
    name: str = "Remember"
    description: str = "Save a fact or observation to memory."
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
    tags: list[str] = Field(default_factory=list, description="Filter long-term memories by these tags.")
    use_agent: bool = Field(default=False, description="If true, launch a sub-agent; query can be more specific and detailed than a few keywords.")


class Recall(CallableTool2):
    name: str = "Recall"
    description: str = "Search and retrieve memories across all tiers."
    params: type[BaseModel] = RecallParams

    def __init__(self, session: Session):
        super().__init__()
        self._session = session

    async def __call__(self, params: RecallParams) -> ToolReturnValue:
        if self._session.get_custom_data().get("is_sub_agent"):
            params.use_agent = False
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

            if not params.use_agent:
                return ToolOk(output=output)

            output_strs: list[str] = []

            def output_function(fn: str, is_thinking: bool) -> None:
                if fn and not is_thinking:
                    output_strs.append(fn)

            async def run_sub_agent(cancel_callable=None):
                session = None
                try:
                    import kimix.base as base
                    custom_data = self._session.get_custom_data()
                    provider_dict = custom_data.get("provider_dict")
                    if provider_dict is None:
                        provider_dict = dict(base._default_provider) if base._default_provider is not None else {}
                    origin_temp = provider_dict.get("temperature", 1.0)
                    provider_dict["temperature"] = origin_temp * 0.3
                    provider_dict["thinking_effort"] = 'off'
                    chat_provider = custom_data.get("chat_provider")
                    session = await _create_session_async(
                        agent_file=base._default_agent_file_dir / "agent_recall.yaml",
                        is_sub_agent=True,
                        agent_type=SystemPromptType.Recaller,
                        provider_dict=provider_dict,
                        chat_provider=chat_provider,
                    )
                    session.get_custom_data()['is_sub_agent'] = True
                    agent_prompt = (
                        f"Query: {params.query}\n\n"
                        f"Recalled memories:\n{output}"
                    )
                    await prompt_async(
                        prompt_str=agent_prompt,
                        session=session,
                        output_function=output_function,
                        cancel_callable=cancel_callable,
                    )
                except Exception as e:
                    return str(e)
                finally:
                    if session:
                        await close_session_async(session)
                return None

            err_msg = await run_sub_agent()
            agent_output = await _maybe_export_output_async("".join(output_strs))
            if err_msg:
                return ToolError(output=agent_output, message=err_msg, brief="")
            return ToolOk(output=agent_output)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to recall memories")


class GetContextParams(BaseModel):
    query: str = Field(description="Query for context generation.")
    max_tokens: int = Field(default=2000, ge=100, le=8000, description="Max characters for context.")


class GetContext(CallableTool2):
    name: str = "GetContext"
    description: str = "Build a context prompt from all memory tiers for the given query."
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
    description: str = "Show memory system status; optionally run deep self-reflection."
    params: type[BaseModel] = ReflectParams

    async def __call__(self, params: ReflectParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            if params.deep:
                report = memory.self_reflect()
                # Heuristic: optimize DB periodically (every 100 interactions)
                if (
                    memory.long_term._backend is not None
                    and memory.interaction_count > 0
                    and memory.interaction_count % 100 == 0
                ):
                    await _run_sync(memory.long_term._backend.optimize)
                    report += "\n[Database optimized]"
            else:
                report = memory.reflect()
            return ToolOk(output=report)
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to reflect on memory")


class ForgetParams(BaseModel):
    content: str = Field(description="Content of the memory to forget.")


class Forget(CallableTool2):
    name: str = "Forget"
    description: str = "Lower the importance of or delete a long-term memory."
    params: type[BaseModel] = ForgetParams

    async def __call__(self, params: ForgetParams) -> ToolReturnValue:
        try:
            memory = await _get_memory_system()
            entry_id = memory.long_term._hash(params.content)
            entry = memory.long_term._get_entry(entry_id)
            if entry is None:
                return ToolOk(output="No matching memory found.")
            old_importance = entry.importance
            await _run_sync(memory.long_term.forget, entry_id)
            entry_after = memory.long_term._get_entry(entry_id)
            if entry_after is None:
                return ToolOk(output=f"Forgotten and deleted: {params.content[:100]}...")
            return ToolOk(
                output=f"Forgotten (importance: {old_importance:.1f} -> {entry_after.importance:.1f}): {params.content[:100]}..."
            )
        except Exception as e:
            return ToolError(message=str(e), output="", brief="Failed to forget memory")
