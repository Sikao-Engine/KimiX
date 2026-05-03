"""Complete Agent memory system — six-layer pyramid with hybrid retrieval."""

from __future__ import annotations

import time
from typing import Any, Optional

from kimix.memory.types import MemoryEntry, MemoryType
from kimix.memory.embedding import EmbeddingProvider
from kimix.memory.working_memory import WorkingMemory
from kimix.memory.short_term_memory import ShortTermMemory
from kimix.memory.long_term_memory import LongTermMemory
from kimix.memory.procedural_memory import ProceduralMemory
from kimix.memory.programmatic_memory import ProgrammaticMemory
from kimix.memory.cold_storage import ColdStorage


class AgentMemorySystem:
    """Complete Agent memory system — L1 through L6."""

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

        # Optional SQLite backend
        backend = None
        if use_sqlite:
            from kimix.memory.sqlite_backend import SQLiteBackend
            backend = SQLiteBackend(db_path)

        # L1–L3
        self.working = WorkingMemory(max_items=10)
        self.short_term = ShortTermMemory(max_size=100, ttl_seconds=3600)
        self.long_term = LongTermMemory(
            storage_path=ltm_path,
            dim=dim,
            backend=backend,
            agent_id=agent_id,
        )

        # L4–L6
        self.procedural = ProceduralMemory()
        self.programmatic = ProgrammaticMemory()
        self.cold_storage = ColdStorage(
            archive_dir=f".kimix_cache/cold_storage/{agent_id}"
        )

        # Config
        self.consolidation_interval = 100
        self.interaction_count = 0
        self.scar_trigger_enabled = True
        self.self_evolution_enabled = True

    # --- Perception ---

    def perceive(
        self,
        observation: str,
        importance: float = 5.0,
        tags: list[str] | None = None,
        source: str = "environment",
        expires_at: float | None = None,
    ) -> MemoryEntry:
        """Agent perceives input, stores in L1 and L2."""
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

    # --- Recall ---

    def recall(
        self,
        query: str,
        context_size: int = 5,
        use_working: bool = True,
        use_short: bool = True,
        use_long: bool = True,
        use_procedural: bool = False,
        tag_filter: list[str] | None = None,
    ) -> dict[str, list[MemoryEntry]]:
        """Multi-tier recall with optional scar-trigger elevation."""
        results: dict[str, list[MemoryEntry]] = {
            "working": [],
            "short_term": [],
            "long_term": [],
        }

        if context_size <= 0:
            return results

        if use_working:
            results["working"] = self.working.get_context(context_size)

        if use_short:
            results["short_term"] = self.short_term.search(
                query, self.embedding_provider, top_k=context_size
            )

        if use_long:
            results["long_term"] = self.long_term.retrieve(
                query, top_k=context_size, tag_filter=tag_filter
            )

        # L4 Scar Triggers: if query matches historical failure patterns,
        # promote the scar into working memory so the agent doesn't repeat mistakes.
        if use_procedural and self.scar_trigger_enabled:
            matched_scars = self.procedural.match_scars(query, top_k=3)
            matched_rules = self.procedural.match_rules(query, top_k=3)
            proc_entries: list[MemoryEntry] = []
            for scar in matched_scars:
                proc_entries.append(scar.to_memory_entry())
            for rule in matched_rules:
                proc_entries.append(rule.to_memory_entry())
            results["procedural"] = proc_entries
            # Elevate high-severity scars to working memory
            for scar in matched_scars:
                if scar.severity >= 7.0:
                    self.working.add(scar.to_memory_entry())

        return results

    # --- Active memorization ---

    def remember(
        self,
        fact: str,
        importance: float = 8.0,
        tags: list[str] | None = None,
        memory_type: MemoryType = MemoryType.SEMANTIC,
        expires_at: float | None = None,
    ) -> MemoryEntry:
        """Actively memorize fact/knowledge into L3."""
        return self.long_term.store(
            content=fact,
            importance=importance,
            tags=tags or [],
            memory_type=memory_type,
            source="agent_learning",
            expires_at=expires_at,
        )

    def add_scar(
        self,
        failure_pattern: str,
        lesson: str,
        trigger_conditions: list[str] | None = None,
        severity: float = 5.0,
    ) -> None:
        """Record a failure scar into L4."""
        self.procedural.add_scar(
            failure_pattern=failure_pattern,
            lesson=lesson,
            trigger_conditions=trigger_conditions or [],
            severity=severity,
        )

    def add_rule(
        self,
        condition: str,
        action: str,
        priority: float = 5.0,
        tags: list[str] | None = None,
    ) -> None:
        """Add an operational rule into L4."""
        self.procedural.add_rule(
            condition=condition,
            action=action,
            priority=priority,
            tags=tags or [],
        )

    # --- RAG context assembly ---

    def get_context_for_llm(self, query: str, max_tokens: int = 2000) -> str:
        """Generate context prompt for LLM (RAG style)."""
        memories = self.recall(query, context_size=5)

        context_parts: list[str] = []
        total_chars = 0

        for source, entries in (
            ("Current Focus", memories["working"]),
            ("Recent Events", memories["short_term"]),
            ("Relevant Knowledge", memories["long_term"]),
            ("Procedural Guidance", memories.get("procedural", [])),
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

    # --- Maintenance ---

    def _consolidate(self) -> None:
        """Execute memory consolidation (L2 → L3) and TTL cleanup."""
        self.long_term.consolidate(self.short_term, threshold=6.0)
        self.short_term.clear_expired()

    def archive_to_cold_storage(
        self,
        entries: list[MemoryEntry] | None = None,
        start_year: int | None = None,
        end_year: int | None = None,
    ) -> None:
        """Archive entries (or all L3) into L6 cold storage."""
        if entries is None:
            entries = [
                e for _, e in self.long_term._iter_entries()
                if e.get_effective_importance() < 3.0
            ]
        if entries:
            self.cold_storage.archive(entries, start_year=start_year, end_year=end_year)

    def self_reflect(self) -> str:
        """Self-evolution loop: analyse memory health and suggest actions.

        * Down-rank rarely-accessed memories.
        * Promote high-access memories.
        * Clear expired entries.
        * Return a human-readable report.
        """
        now = time.time()
        report_lines: list[str] = ["Self-Reflection Report:", "=" * 30]

        # L3 health check
        ltm_count = self.long_term.count()
        report_lines.append(f"Long-term entries: {ltm_count}")

        if self.long_term._backend is None:
            low_access = [
                e for e in self.long_term.entries.values()
                if e.access_count < 2 and (now - e.last_accessed) > 7 * 86400
            ]
            high_access = [
                e for e in self.long_term.entries.values()
                if e.access_count >= 5
            ]
            # Down-rank stale memories
            for entry in low_access:
                entry.importance = max(0.1, entry.importance * 0.8)
            # Up-rank hot memories
            for entry in high_access:
                entry.importance = min(10.0, entry.importance * 1.1)

            report_lines.append(f"  Down-ranked stale: {len(low_access)}")
            report_lines.append(f"  Up-ranked hot: {len(high_access)}")

        # TTL cleanup across tiers
        self.short_term.clear_expired()
        report_lines.append(f"Short-term buffer: {len(self.short_term.buffer)} items")

        # L4 / L5 / L6 summaries
        report_lines.append(self.procedural.reflect())
        report_lines.append(self.programmatic.reflect())
        report_lines.append(self.cold_storage.reflect())

        return "\n".join(report_lines)

    def reflect(self) -> str:
        """Generate memory system status report."""
        report = f"""
Memory System Status Report:
===========================
Agent ID: {self.agent_id}
Working Memory: {len(self.working.items)} items (capacity: {self.working.max_items})
Short-term Memory: {len(self.short_term.buffer)} items (capacity: {self.short_term.max_size})
Long-term Memory: {self.long_term.count()} items
Procedural Memory: {len(self.procedural.scars)} scars, {len(self.procedural.rules)} rules
Programmatic Memory: {len(self.programmatic.workflows)} workflows
Cold Storage: {len(self.cold_storage.list_archives())} archives
Interactions: {self.interaction_count}
        """
        return report
