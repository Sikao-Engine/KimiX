"""L4 Procedural Memory: scars (negative learning) and rules (policies)."""

from __future__ import annotations

import heapq
import re
import time
from dataclasses import dataclass, field
from itertools import chain
from typing import Any

from kimix.memory.types import MemoryEntry, MemoryType


_WORD_RE = re.compile(r"\b\w+\b")


@dataclass(slots=True)
class ScarEntry:
    """Negative learning record — a failure or boundary experience."""

    failure_pattern: str          # Description of what went wrong
    lesson: str                   # What to avoid / how to fix
    trigger_conditions: list[str] = field(default_factory=list)  # Keywords/patterns
    severity: float = 5.0         # 0–10
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_memory_entry(self) -> MemoryEntry:
        return MemoryEntry(
            content=f"SCAR: {self.failure_pattern} | LESSON: {self.lesson}",
            memory_type=MemoryType.SCAR,
            importance=self.severity,
            tags=["scar"] + self.trigger_conditions,
            metadata={
                "failure_pattern": self.failure_pattern,
                "lesson": self.lesson,
                "severity": self.severity,
                **self.metadata,
            },
        )


@dataclass(slots=True)
class RuleEntry:
    """Operational policy / decision rule."""

    condition: str                # When this applies (text description or pattern)
    action: str                   # What to do
    priority: float = 5.0         # 0–10, higher wins
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_memory_entry(self) -> MemoryEntry:
        return MemoryEntry(
            content=f"RULE: IF {self.condition} THEN {self.action}",
            memory_type=MemoryType.RULE,
            importance=self.priority,
            tags=["rule"] + self.tags,
            metadata={
                "condition": self.condition,
                "action": self.action,
                "priority": self.priority,
                **self.metadata,
            },
        )


class ProceduralMemory:
    """L4 memory: scars and rules with trigger-aware retrieval."""

    def __init__(self) -> None:
        self.scars: list[ScarEntry] = []
        self.rules: list[RuleEntry] = []
        self._rules_dirty: bool = False

    # --- Scars ---

    def add_scar(
        self,
        failure_pattern: str,
        lesson: str,
        trigger_conditions: list[str] | None = None,
        severity: float = 5.0,
        metadata: dict[str, Any] | None = None,
    ) -> ScarEntry:
        """Record a new scar (negative learning)."""
        scar = ScarEntry(
            failure_pattern=failure_pattern,
            lesson=lesson,
            trigger_conditions=trigger_conditions or [],
            severity=severity,
            metadata=metadata or {},
        )
        self.scars.append(scar)
        return scar

    def match_scars(self, query: str, top_k: int = 3) -> list[ScarEntry]:
        """Return scars whose trigger conditions match *query*."""
        scored: list[tuple[float, ScarEntry]] = []
        query_lower = query.lower()
        for scar in self.scars:
            score = 0.0
            for cond in scar.trigger_conditions:
                if cond.lower() in query_lower:
                    score += 1.0
            if score:
                scored.append((score * scar.severity, scar))
        return [s[1] for s in heapq.nlargest(top_k, scored)]

    # --- Rules ---

    def add_rule(
        self,
        condition: str,
        action: str,
        priority: float = 5.0,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RuleEntry:
        """Add a new operational rule."""
        rule = RuleEntry(
            condition=condition,
            action=action,
            priority=priority,
            tags=tags or [],
            metadata=metadata or {},
        )
        self.rules.append(rule)
        self._rules_dirty = True
        return rule

    def _ensure_rules_sorted(self) -> None:
        if self._rules_dirty:
            self.rules.sort(key=lambda r: r.priority, reverse=True)
            self._rules_dirty = False

    def match_rules(self, context: str, top_k: int = 3) -> list[RuleEntry]:
        """Return rules whose condition text appears in *context*."""
        self._ensure_rules_sorted()
        scored: list[tuple[float, RuleEntry]] = []
        ctx_lower = context.lower()
        ctx_words = set(_WORD_RE.findall(ctx_lower))
        for rule in self.rules:
            score = 0.0
            cond_lower = rule.condition.lower()
            # Exact phrase match
            if cond_lower in ctx_lower:
                score += 2.0
            # Word overlap
            cond_words = set(_WORD_RE.findall(cond_lower))
            if cond_words:
                overlap = len(cond_words & ctx_words) / len(cond_words)
                score += overlap
            if score:
                scored.append((score * rule.priority, rule))
        return [r[1] for r in heapq.nlargest(top_k, scored)]

    # --- Unified ---

    def check_triggers(self, query: str) -> dict[str, list[Any]]:
        """Check both scars and rules against a query."""
        return {
            "scars": self.match_scars(query),
            "rules": self.match_rules(query),
        }

    def to_entries(self) -> list[MemoryEntry]:
        """Export all scars and rules as MemoryEntries."""
        return list(chain(
            (s.to_memory_entry() for s in self.scars),
            (r.to_memory_entry() for r in self.rules),
        ))

    def reflect(self) -> str:
        """Status report."""
        return (
            f"Procedural Memory: {len(self.scars)} scars, {len(self.rules)} rules"
        )
