"""Complete Agent memory system with hybrid retrieval."""

from __future__ import annotations

import time
from kimix.memory.types import MemoryEntry, MemoryType
from kimix.memory.embedding import EmbeddingProvider
from kimix.memory.working_memory import WorkingMemory
from kimix.memory.short_term_memory import ShortTermMemory
from kimix.memory.long_term_memory import LongTermMemory


class AgentMemorySystem:
    def __init__(
        self,
        dim: int = 384,
        ltm_path: str = ".kimix_cache/ltm.json",
        agent_id: str = "default",
        use_sqlite: bool = False,
        db_path: str = ".kimix_cache/memory.db",
    ) -> None:
        self.agent_id = agent_id
        self.embedding_provider = EmbeddingProvider(dim)

        backend = None
        if use_sqlite:
            from kimix.memory.sqlite_backend import SQLiteBackend
            backend = SQLiteBackend(db_path)

        self.working = WorkingMemory(max_items=10)
        self.short_term = ShortTermMemory(max_size=100, ttl_seconds=3600)
        self.long_term = LongTermMemory(
            storage_path=ltm_path,
            dim=dim,
            backend=backend,
            agent_id=agent_id,
        )

        self.consolidation_interval = 100
        self.interaction_count = 0

    def perceive(
        self,
        observation: str,
        importance: float = 5.0,
        tags: list[str] | None = None,
        source: str = "environment",
        expires_at: float | None = None,
    ) -> MemoryEntry:
        entry = MemoryEntry(
            content=observation,
            memory_type=MemoryType.EPISODIC,
            importance=importance,
            tags=tags or [],
            source=source,
            expires_at=expires_at,
            agent_id=self.agent_id,
        )
        self.working.add(entry)
        self.short_term.add(entry)
        self.interaction_count += 1

        if self.interaction_count % self.consolidation_interval == 0:
            self._consolidate()

        return entry

    def recall(
        self,
        query: str,
        context_size: int = 5,
        use_working: bool = True,
        use_short: bool = True,
        use_long: bool = True,
        tag_filter: list[str] | None = None,
    ) -> dict[str, list[MemoryEntry]]:
        results: dict[str, list[MemoryEntry]] = {
            "working": [],
            "short_term": [],
            "long_term": [],
        }
        if context_size <= 0:
            return results

        query_vec = self.embedding_provider.embed(query)

        if use_working:
            results["working"] = self.working.get_context(context_size)
        if use_short:
            results["short_term"] = self.short_term.search(
                query, self.embedding_provider, top_k=context_size, query_vec=query_vec
            )
        if use_long:
            results["long_term"] = self.long_term.retrieve(
                query, top_k=context_size, tag_filter=tag_filter, query_vec=query_vec
            )

        return results

    def remember(
        self,
        fact: str,
        importance: float = 8.0,
        tags: list[str] | None = None,
        memory_type: MemoryType = MemoryType.SEMANTIC,
        expires_at: float | None = None,
    ) -> MemoryEntry:
        return self.long_term.store(
            content=fact,
            importance=importance,
            tags=tags or [],
            memory_type=memory_type,
            source="agent_learning",
            expires_at=expires_at,
        )

    def get_context_for_llm(self, query: str, max_tokens: int = 2000) -> str:
        memories = self.recall(query, context_size=5)

        context_parts: list[str] = []
        total_chars = 0

        for source, entries in (
            ("Current Focus", memories["working"]),
            ("Recent Events", memories["short_term"]),
            ("Relevant Knowledge", memories["long_term"]),
        ):
            if not entries:
                continue

            header = f"\n=== {source} ===\n"
            section_items: list[str] = []
            section_len = len(header)

            for entry in entries:
                item = f"- [{entry.memory_type.value}] {entry.content}\n"
                item_len = len(item)
                if total_chars + section_len + item_len > max_tokens:
                    break
                section_items.append(item)
                section_len += item_len

            if section_items:
                context_parts.append(header + "".join(section_items))
                total_chars += section_len

        return "\n".join(context_parts)

    def _consolidate(self) -> None:
        self.long_term.consolidate(self.short_term, threshold=6.0)
        self.short_term.clear_expired()

    def self_reflect(self) -> str:
        now = time.time()
        report_lines: list[str] = ["Self-Reflection Report:", "=" * 30]

        ltm_count = self.long_term.count()
        report_lines.append(f"Long-term entries: {ltm_count}")

        if self.long_term._backend is None:
            low_access: list[MemoryEntry] = []
            high_access: list[MemoryEntry] = []
            week_ago = now - 7 * 86400
            for e in self.long_term.entries.values():
                if e.access_count < 2 and e.last_accessed < week_ago:
                    low_access.append(e)
                elif e.access_count >= 5:
                    high_access.append(e)
            for entry in low_access:
                entry.importance = max(0.1, entry.importance * 0.8)
            for entry in high_access:
                entry.importance = min(10.0, entry.importance * 1.1)

            report_lines.append(f"  Down-ranked stale: {len(low_access)}")
            report_lines.append(f"  Up-ranked hot: {len(high_access)}")

        self.short_term.clear_expired()
        report_lines.append(f"Short-term buffer: {len(self.short_term.buffer)} items")

        return "\n".join(report_lines)

    def reflect(self) -> str:
        return f"""Memory System Status Report:
===========================
Agent ID: {self.agent_id}
Working Memory: {len(self.working.items)} items (capacity: {self.working.max_items})
Short-term Memory: {len(self.short_term.buffer)} items (capacity: {self.short_term.max_size})
Long-term Memory: {self.long_term.count()} items
Interactions: {self.interaction_count}
"""
