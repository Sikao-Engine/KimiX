"""Agent Memory System - Tiered memory architecture for Kimi Agent."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kimix.memory.types import MemoryEntry, MemoryType
    from kimix.memory.embedding import EmbeddingProvider
    from kimix.memory.working_memory import WorkingMemory
    from kimix.memory.short_term_memory import ShortTermMemory
    from kimix.memory.long_term_memory import LongTermMemory
    from kimix.memory.retrieval import NgramTokenizer, InvertedIndex, BM25Scorer, Searcher
    from kimix.memory.system import AgentMemorySystem
    from kimix.memory.procedural_memory import ProceduralMemory, ScarEntry, RuleEntry
    from kimix.memory.programmatic_memory import ProgrammaticMemory, Workflow, Task, Trigger, TriggerType
    from kimix.memory.cold_storage import ColdStorage
    from kimix.memory.sqlite_backend import SQLiteBackend

__all__ = [
    "MemoryEntry",
    "MemoryType",
    "EmbeddingProvider",
    "WorkingMemory",
    "ShortTermMemory",
    "LongTermMemory",
    "NgramTokenizer",
    "InvertedIndex",
    "BM25Scorer",
    "Searcher",
    "AgentMemorySystem",
    "ProceduralMemory",
    "ScarEntry",
    "RuleEntry",
    "ProgrammaticMemory",
    "Workflow",
    "Task",
    "Trigger",
    "TriggerType",
    "ColdStorage",
    "SQLiteBackend",
]

_IMPORT_MAP = {
    "MemoryEntry": "kimix.memory.types",
    "MemoryType": "kimix.memory.types",
    "EmbeddingProvider": "kimix.memory.embedding",
    "WorkingMemory": "kimix.memory.working_memory",
    "ShortTermMemory": "kimix.memory.short_term_memory",
    "LongTermMemory": "kimix.memory.long_term_memory",
    "NgramTokenizer": "kimix.memory.retrieval",
    "InvertedIndex": "kimix.memory.retrieval",
    "BM25Scorer": "kimix.memory.retrieval",
    "Searcher": "kimix.memory.retrieval",
    "AgentMemorySystem": "kimix.memory.system",
    "ProceduralMemory": "kimix.memory.procedural_memory",
    "ScarEntry": "kimix.memory.procedural_memory",
    "RuleEntry": "kimix.memory.procedural_memory",
    "ProgrammaticMemory": "kimix.memory.programmatic_memory",
    "Workflow": "kimix.memory.programmatic_memory",
    "Task": "kimix.memory.programmatic_memory",
    "Trigger": "kimix.memory.programmatic_memory",
    "TriggerType": "kimix.memory.programmatic_memory",
    "ColdStorage": "kimix.memory.cold_storage",
    "SQLiteBackend": "kimix.memory.sqlite_backend",
}


def __getattr__(name: str) -> object:
    if name in _IMPORT_MAP:
        import importlib

        module = importlib.import_module(_IMPORT_MAP[name])
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return __all__
