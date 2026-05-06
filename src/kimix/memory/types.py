"""Memory types and data structures."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

_DECAY_COEFF = -0.1 / 86400.0


class MemoryType(Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


@dataclass(slots=True)
class MemoryEntry:
    content: str
    memory_type: MemoryType
    timestamp: float = field(default_factory=time.time)
    importance: float = 1.0
    access_count: int = 0
    last_accessed: float = field(default_factory=time.time)
    embedding: list[float] | np.ndarray | None = None
    tags: list[str] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    expires_at: float | None = None
    agent_id: str = "default"

    def is_expired(self, now: float | None = None) -> bool:
        if self.expires_at is None:
            return False
        if now is None:
            now = time.time()
        return now > self.expires_at

    def get_effective_importance(self, now: float | None = None) -> float:
        if now is None:
            now = time.time()
        recency = math.exp(_DECAY_COEFF * (now - self.timestamp))
        boost = self.access_count * 0.1 if self.access_count < 20 else 2.0
        return self.importance * recency * (1.0 + boost)

    def touch(self, now: float | None = None) -> None:
        self.access_count += 1
        if now is None:
            now = time.time()
        self.last_accessed = now

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        embedding = self.embedding
        if isinstance(embedding, np.ndarray):
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
            "effective_importance": self.get_effective_importance(now),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryEntry:
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
