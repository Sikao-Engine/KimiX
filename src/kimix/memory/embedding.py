"""Embedding vector provider."""

import threading
import zlib
from collections import OrderedDict
from typing import Sequence

import numpy as np


class EmbeddingProvider:
    __slots__ = ("dim", "_cache", "_max_cache_size", "_lock")

    def __init__(self, dim: int = 384, max_cache_size: int = 4096) -> None:
        self.dim = dim
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._max_cache_size = max_cache_size
        self._lock = threading.Lock()

    def _compute(self, text: str) -> np.ndarray:
        seed = zlib.crc32(text.encode()) & 0xFFFFFFFF
        rng = np.random.default_rng(seed)
        vec = rng.standard_normal(self.dim, dtype=np.float32)
        norm = np.linalg.norm(vec)
        if norm:
            vec /= norm
        return vec

    def embed(self, text: str) -> np.ndarray:
        with self._lock:
            vec = self._cache.get(text)
            if vec is not None:
                self._cache.move_to_end(text)
                return vec

        vec = self._compute(text)

        with self._lock:
            self._cache[text] = vec
            if len(self._cache) > self._max_cache_size:
                self._cache.popitem(last=False)
        return vec

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        results: list[np.ndarray | None] = [None] * len(texts)
        missing_texts: list[str] = []
        missing_indices: list[int] = []

        with self._lock:
            for i, text in enumerate(texts):
                vec = self._cache.get(text)
                if vec is not None:
                    self._cache.move_to_end(text)
                    results[i] = vec
                else:
                    missing_texts.append(text)
                    missing_indices.append(i)

        if missing_texts:
            computed = [self._compute(t) for t in missing_texts]
            with self._lock:
                for text, idx, vec in zip(missing_texts, missing_indices, computed):
                    results[idx] = vec
                    self._cache[text] = vec
                excess = len(self._cache) - self._max_cache_size
                for _ in range(excess):
                    self._cache.popitem(last=False)

        return results  # type: ignore[return-value]

    def similarity(self, vec1: Sequence[float] | np.ndarray, vec2: Sequence[float] | np.ndarray) -> float:
        v1 = vec1 if isinstance(vec1, np.ndarray) else np.asarray(vec1, dtype=np.float32)
        v2 = vec2 if isinstance(vec2, np.ndarray) else np.asarray(vec2, dtype=np.float32)
        dot = np.dot(v1, v2)
        if dot == 0:
            return 0.0
        norms = np.sqrt(np.dot(v1, v1) * np.dot(v2, v2))
        if norms == 0:
            return 0.0
        return float(dot / norms)
