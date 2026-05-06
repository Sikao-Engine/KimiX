"""Short-term memory: detailed current session records with temporal validity."""

from __future__ import annotations

import heapq
import time

import numpy as np

from kimix.memory.types import MemoryEntry, MemoryType
from kimix.memory.embedding import EmbeddingProvider


class ShortTermMemory:
    __slots__ = ("max_size", "ttl", "buffer")

    def __init__(self, max_size: int = 100, ttl_seconds: float = 3600) -> None:
        self.max_size = max_size
        self.ttl = ttl_seconds
        self.buffer: list[MemoryEntry] = []

    def add(self, entry: MemoryEntry) -> None:
        entry.memory_type = MemoryType.EPISODIC
        self.buffer.append(entry)
        if len(self.buffer) > self.max_size:
            self._evict_least_valuable()

    def _evict_least_valuable(self) -> None:
        if not self.buffer:
            return
        now = time.time()
        min_idx = min(range(len(self.buffer)), key=lambda i: self.buffer[i].get_effective_importance(now))
        self.buffer[min_idx] = self.buffer[-1]
        self.buffer.pop()

    def _active_buffer(self, now: float | None = None) -> list[MemoryEntry]:
        if now is None:
            now = time.time()
        cutoff = now - self.ttl
        return [
            e for e in self.buffer
            if e.timestamp > cutoff and (e.expires_at is None or e.expires_at > now)
        ]

    def search(
        self,
        query: str,
        embedding_provider: EmbeddingProvider,
        top_k: int = 5,
        query_vec: np.ndarray | None = None,
    ) -> list[MemoryEntry]:
        now = time.time()
        active = self._active_buffer(now)
        if not active:
            return []

        if query_vec is None:
            query_vec = embedding_provider.embed(query)

        missing = [(i, entry.content) for i, entry in enumerate(active) if entry.embedding is None]
        if missing:
            indices, texts = zip(*missing)
            embeddings = embedding_provider.embed_batch(texts)
            for i, emb in zip(indices, embeddings):
                active[i].embedding = emb

        scored = [
            (embedding_provider.similarity(query_vec, entry.embedding) * entry.get_effective_importance(now), entry)
            for entry in active
        ]
        results = [entry for _, entry in heapq.nlargest(top_k, scored, key=lambda x: x[0])]
        for entry in results:
            entry.touch(now)
        return results

    def get_recent(self, n: int = 10) -> list[MemoryEntry]:
        now = time.time()
        active = self._active_buffer(now)
        return heapq.nlargest(n, active, key=lambda e: e.timestamp)

    def clear_expired(self) -> None:
        now = time.time()
        self.buffer[:] = self._active_buffer(now)
