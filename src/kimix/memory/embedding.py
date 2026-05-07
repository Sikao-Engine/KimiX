"""Embedding vector provider."""

import threading
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

    def _normalize_key(self, text: str) -> str:
        return " ".join(text.lower().split())

    def _hash_token(self, token: str) -> int:
        # Deterministic FNV-1a 64-bit hash
        h = 14695981039346656037
        for c in token.encode("utf-8"):
            h ^= c
            h = (h * 1099511628211) & 0xFFFFFFFFFFFFFFFF
        return h

    def _compute(self, text: str) -> np.ndarray:
        vec = np.zeros(self.dim, dtype=np.float32)
        norm_text = text.lower()
        tokens = norm_text.split()

        # 1. Feature hashing from unigrams
        for token in tokens:
            h = self._hash_token(token)
            idx = h % self.dim
            sign = 1 if (h >> 32) & 1 else -1
            vec[idx] += sign

        # 2. Feature hashing from bigrams (lower weight for locality)
        for i in range(len(tokens) - 1):
            bigram = tokens[i] + " " + tokens[i + 1]
            h = self._hash_token(bigram)
            idx = h % self.dim
            sign = 1 if (h >> 32) & 1 else -1
            vec[idx] += sign * 0.5

        # 3. Statistical features
        if text:
            # Prefix / suffix
            prefix = text[: min(3, len(text))].lower()
            suffix = text[-min(3, len(text)) :].lower()
            vec[self._hash_token(prefix) % self.dim] += 0.3
            vec[self._hash_token(suffix) % self.dim] += 0.3

            # Length and word count
            vec[self._hash_token("len:" + str(len(text))) % self.dim] += 0.2
            vec[self._hash_token("words:" + str(len(tokens))) % self.dim] += 0.2

            # Digit and punctuation ratios
            digits = sum(c.isdigit() for c in text)
            puncts = sum(not c.isalnum() and not c.isspace() for c in text)
            vec[self._hash_token("dig:" + str(digits)) % self.dim] += 0.15
            vec[self._hash_token("pun:" + str(puncts)) % self.dim] += 0.15

            # Letter distribution histogram (26 letters)
            total_letters = 0
            for c in norm_text:
                if "a" <= c <= "z":
                    total_letters += 1
            if total_letters:
                for i in range(26):
                    ch = chr(ord("a") + i)
                    count = norm_text.count(ch)
                    if count:
                        idx = self._hash_token("hist:" + ch) % self.dim
                        vec[idx] += (count / total_letters) * 0.1

        # Normalize
        norm = np.linalg.norm(vec)
        if norm:
            vec /= norm
        return vec

    def embed(self, text: str) -> np.ndarray:
        key = self._normalize_key(text)
        with self._lock:
            vec = self._cache.get(key)
            if vec is not None:
                self._cache.move_to_end(key)
                return vec

        vec = self._compute(text)

        with self._lock:
            self._cache[key] = vec
            if len(self._cache) > self._max_cache_size:
                self._cache.popitem(last=False)
        return vec

    def embed_batch(self, texts: list[str]) -> list[np.ndarray]:
        results: list[np.ndarray | None] = [None] * len(texts)
        missing_texts: list[str] = []
        missing_indices: list[int] = []

        with self._lock:
            for i, text in enumerate(texts):
                key = self._normalize_key(text)
                vec = self._cache.get(key)
                if vec is not None:
                    self._cache.move_to_end(key)
                    results[i] = vec
                else:
                    missing_texts.append(text)
                    missing_indices.append(i)

        if missing_texts:
            computed = [self._compute(t) for t in missing_texts]
            with self._lock:
                for text, idx, vec in zip(missing_texts, missing_indices, computed):
                    key = self._normalize_key(text)
                    results[idx] = vec
                    self._cache[key] = vec
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
