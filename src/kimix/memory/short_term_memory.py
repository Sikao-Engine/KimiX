"""Short-term memory: detailed current session records with temporal validity."""

from __future__ import annotations

import heapq
import time
from typing import List

from kimix.memory.types import MemoryEntry, MemoryType
from kimix.memory.embedding import EmbeddingProvider


class ShortTermMemory:
    """Short-term memory: detailed current session records with temporal validity."""

    __slots__ = ("max_size", "ttl", "buffer")

    def __init__(self, max_size: int = 100, ttl_seconds: float = 3600) -> None:
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.buffer: List[MemoryEntry] = []

    def add(self, entry: MemoryEntry) -> None:
        """Add memory to short-term buffer."""
        entry.memory_type = MemoryType.EPISODIC
        self.buffer.append(entry)
        if len(self.buffer) > self.max_size:
            self._evict_least_valuable()

    def _evict_least_valuable(self) -> None:
        """Eviction policy: remove entry with lowest effective importance."""
        if not self.buffer:
            return
        now = time.time()
        min_idx, _ = min(
            enumerate(self.buffer),
            key=lambda x: x[1].get_effective_importance(now),
        )
        self.buffer[min_idx] = self.buffer[-1]
        self.buffer.pop()

    def _active_buffer(self, now: float | None = None) -> List[MemoryEntry]:
        """Return only non-expired entries."""
        if now is None:
            now = time.time()
        cutoff = now - self.ttl
        return [
            e
            for e in self.buffer
            if e.timestamp > cutoff and (e.expires_at is None or e.expires_at > now)
        ]

    def search(
        self,
        query: str,
        embedding_provider: EmbeddingProvider,
        top_k: int = 5,
    ) -> List[MemoryEntry]:
        """Semantic search in short-term memory (skips expired)."""
        now = time.time()
        active = self._active_buffer(now)
        if not active:
            return []

        query_vec = embedding_provider.embed(query)

        # Batch-compute missing embeddings instead of one-by-one calls
        missing_texts = [entry.content for entry in active if entry.embedding is None]
        if missing_texts:
            embeddings = embedding_provider.embed_batch(missing_texts)
            emb_iter = iter(embeddings)
            for entry in active:
                if entry.embedding is None:
                    entry.embedding = next(emb_iter)

        scored = [
            (
                embedding_provider.similarity(query_vec, entry.embedding)
                * entry.get_effective_importance(now),
                entry,
            )
            for entry in active
        ]
        results = [
            entry for _, entry in heapq.nlargest(top_k, scored, key=lambda x: x[0])
        ]

        for entry in results:
            entry.touch(now)

        return results

    def get_recent(self, n: int = 10) -> List[MemoryEntry]:
        """Get recent n entries (skips expired)."""
        now = time.time()
        active = self._active_buffer(now)
        return heapq.nlargest(n, active, key=lambda x: x.timestamp)

    def clear_expired(self) -> None:
        """Clean expired memories (both TTL and explicit expiry)."""
        self.buffer = self._active_buffer(time.time())
