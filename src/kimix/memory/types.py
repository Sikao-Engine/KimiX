"""Memory types and data structures."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np


class MemoryType(Enum):
    """Memory type enumeration — six-layer pyramid."""

    WORKING = "working"          # L1: current context window
    EPISODIC = "episodic"        # L2: session / event memory
    SEMANTIC = "semantic"        # L3: facts / knowledge
    PROCEDURAL = "procedural"    # L4: skills / methods (legacy alias)
    SCAR = "scar"                # L4: negative learning (failure/lesson)
    RULE = "rule"                # L4: operational policy / decision rule
    COMPILED_TRUTH = "compiled_truth"  # L3: validated aggregated truth
    ENTITY = "entity"            # L3: extracted entity node
    FACT = "fact"                # L3: atomic factual statement
    WORKFLOW = "workflow"        # L5: automated workflow definition
    TASK = "task"                # L5: unit of work inside a workflow
    TRIGGER = "trigger"          # L5: event/schedule trigger
    PROGRAMMATIC = "programmatic"  # L5: generic programmatic memory
    COLD_ARCHIVE = "cold_archive"  # L6: compressed long-term archive


@dataclass(slots=True)
class MemoryEntry:
    """Single memory entry with temporal validity and multi-agent support."""

    content: str
    memory_type: MemoryType
    timestamp: float = field(default_factory=time.time)
    importance: float = 1.0               # 0–10
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)
    embedding: list[float] | np.ndarray | None = None
    tags: list[str] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    expires_at: float | None = None       # Temporal validity (absolute timestamp)
    agent_id: str = "default"             # Multi-tenant isolation

    def is_expired(self, now: float | None = None) -> bool:
        """Check whether this memory has passed its expiry time."""
        if self.expires_at is None:
            return False
        if now is None:
            now = time.time()
        return now > self.expires_at

    def get_effective_importance(self, now: float | None = None) -> float:
        """Calculate effective importance (time decay + access frequency)."""
        if now is None:
            now = time.time()
        days_old = (now - self.timestamp) / 86400
        recency_factor = math.exp(-0.1 * days_old)
        access_boost = min(self.access_count * 0.1, 2.0)
        return self.importance * recency_factor * (1 + access_boost)

    def touch(self, now: float | None = None) -> None:
        """Mark as accessed."""
        self.access_count += 1
        self.last_accessed = time.time() if now is None else now

    def to_dict(self) -> dict[str, Any]:
        embedding = self.embedding
        if embedding is not None and hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        return {
            "content": self.content,
            "memory_type": self.memory_type.value,
            "timestamp": self.timestamp,
            "importance": self.importance,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "embedding": embedding,
            "tags": self.tags,
            "source": self.source,
            "metadata": self.metadata,
            "expires_at": self.expires_at,
            "agent_id": self.agent_id,
            "effective_importance": self.get_effective_importance(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
        """Reconstruct a MemoryEntry from a plain dict."""
        return cls(
            content=data["content"],
            memory_type=MemoryType(data["memory_type"]),
            timestamp=data.get("timestamp", time.time()),
            importance=data.get("importance", 1.0),
            access_count=data.get("access_count", 0),
            last_accessed=data.get("last_accessed", time.time()),
            embedding=data.get("embedding"),
            tags=data.get("tags", []),
            source=data.get("source", ""),
            metadata=data.get("metadata", {}),
            expires_at=data.get("expires_at"),
            agent_id=data.get("agent_id", "default"),
        )
