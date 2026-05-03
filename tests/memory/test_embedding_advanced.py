"""Advanced tests for EmbeddingProvider: cache, determinism, similarity edge cases."""

import numpy as np
import pytest

from kimix.memory.embedding import EmbeddingProvider


class TestEmbeddingCache:
    def test_cache_returns_same_object(self):
        provider = EmbeddingProvider(dim=384)
        v1 = provider.embed("cache me")
        v2 = provider.embed("cache me")
        assert v1 is v2

    def test_cache_eviction_on_overflow(self):
        provider = EmbeddingProvider(dim=16, max_cache_size=3)
        provider.embed("a")
        provider.embed("b")
        provider.embed("c")
        provider.embed("d")  # should evict oldest
        assert len(provider._cache) == 3

    def test_different_texts_different_vectors(self):
        provider = EmbeddingProvider(dim=384)
        v1 = provider.embed("hello")
        v2 = provider.embed("world")
        assert not np.allclose(v1, v2)

    def test_determinism(self):
        provider = EmbeddingProvider(dim=384)
        v1 = provider.embed("deterministic")
        v2 = provider.embed("deterministic")
        assert np.allclose(v1, v2)


class TestEmbeddingSimilarity:
    def test_similarity_identical_vectors(self):
        provider = EmbeddingProvider(dim=384)
        v = provider.embed("same")
        assert provider.similarity(v, v) == pytest.approx(1.0, abs=1e-5)

    def test_similarity_opposite_vectors(self):
        provider = EmbeddingProvider(dim=384)
        v = np.ones(384, dtype=np.float32)
        v = v / np.linalg.norm(v)
        neg = -v
        assert provider.similarity(v, neg) == pytest.approx(-1.0, abs=1e-5)

    def test_similarity_orthogonal(self):
        provider = EmbeddingProvider(dim=384)
        a = np.zeros(384, dtype=np.float32)
        a[0] = 1.0
        b = np.zeros(384, dtype=np.float32)
        b[1] = 1.0
        assert provider.similarity(a, b) == pytest.approx(0.0, abs=1e-5)

    def test_similarity_zero_norm(self):
        provider = EmbeddingProvider(dim=384)
        zero = np.zeros(384, dtype=np.float32)
        v = provider.embed("text")
        assert provider.similarity(zero, v) == 0.0
        assert provider.similarity(zero, zero) == 0.0

    def test_similarity_list_input(self):
        provider = EmbeddingProvider(dim=3)
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert provider.similarity(a, b) == pytest.approx(0.0, abs=1e-5)
