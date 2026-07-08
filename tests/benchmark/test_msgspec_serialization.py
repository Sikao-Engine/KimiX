"""Benchmark: msgspec-based index serialization round-trip."""

import tempfile
import time
from pathlib import Path

import pytest

from kimix.retrieval import InvertedIndex


@pytest.mark.slow
class TestMsgspecSerialization:
    """Tests for msgspec-based InvertedIndex serialization."""

    def test_msgspec_serialization_roundtrip(self) -> None:
        """Index serialized with msgspec should round-trip correctly."""
        idx = InvertedIndex()
        idx.add_document(1, ["ab", "bc"])
        idx.add_document(2, ["ab"])
        idx.finalize()

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            path = Path(f.name)

        try:
            idx.save(str(path))
            idx2 = InvertedIndex()
            idx2.load(str(path))

            assert idx2._N == idx._N
            assert idx2._avgdl == idx._avgdl
            assert idx2._term_to_id == idx._term_to_id
            assert idx2._doc_lengths == idx._doc_lengths
        finally:
            path.unlink(missing_ok=True)

    def test_msgspec_serialization_with_forward_index(self) -> None:
        """Round-trip with forward index included."""
        idx = InvertedIndex()
        idx.add_document(1, ["ab", "bc", "cd"])
        idx.add_document(2, ["ab", "de"])
        idx.finalize(stop_threshold=1.0)

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            path = Path(f.name)

        try:
            idx.save(str(path), include_forward_index=True)
            idx2 = InvertedIndex()
            idx2.load(str(path))

            assert idx2._N == idx._N
            assert idx2._avgdl == idx._avgdl
            assert len(idx2._doc_term_freqs) > 0

            # Verify forward index data is present
            for doc_idx, tf_map in enumerate(idx._doc_term_freqs):
                if doc_idx < len(idx2._doc_term_freqs):
                    for term, tf in tf_map.items():
                        assert idx2._doc_term_freqs[doc_idx].get(term) == tf
        finally:
            path.unlink(missing_ok=True)

    def test_msgspec_serialization_speed(self) -> None:
        """msgspec serialization should be reasonably fast."""
        idx = InvertedIndex()
        for i in range(100):
            idx.add_document(i, [f"token_{j}" for j in range(20)])
        idx.finalize()

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            path = Path(f.name)

        try:
            # Benchmark save
            t0 = time.perf_counter()
            for _ in range(10):
                idx.save(str(path))
            t_save = time.perf_counter() - t0

            # Benchmark load
            t0 = time.perf_counter()
            for _ in range(10):
                idx2 = InvertedIndex()
                idx2.load(str(path))
            t_load = time.perf_counter() - t0

            # Should complete within reasonable time
            assert t_save < 5.0
            assert t_load < 5.0
        finally:
            path.unlink(missing_ok=True)
