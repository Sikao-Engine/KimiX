"""Tests for kimi_cli.soul.history_index — BM25 index over conversation turns."""

from __future__ import annotations

import time
from pathlib import Path

from kosong.message import Message

from kimi_cli.soul.history_index import HistoryIndex
from kimi_cli.wire.types import TextPart


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[TextPart(text=text)])


class TestHistoryIndex:
    def test_search_empty_index(self):
        idx = HistoryIndex()
        assert idx.search("anything") == []

    def test_index_and_search(self):
        idx = HistoryIndex()
        idx.index_messages([_msg("user", "How do I compile Python?")])
        idx.index_messages([_msg("assistant", "Use pyinstaller or cx_Freeze.")])

        results = idx.search("compile Python", top_k=2)
        # Only the user message should match the query
        assert len(results) == 1
        assert results[0]["role"] == "user"
        assert "compile Python" in results[0]["text"]

    def test_skips_system_and_tool_roles(self):
        idx = HistoryIndex()
        idx.index_messages([
            Message(role="system", content=[TextPart(text="System prompt")]),
            _msg("user", "Hello"),
        ])
        assert len(idx._turns) == 1
        assert idx._turns[0]["role"] == "user"

    def test_skips_empty_messages(self):
        idx = HistoryIndex()
        idx.index_messages([_msg("user", "   ")])
        assert len(idx._turns) == 0

    def test_mark_compacted(self):
        idx = HistoryIndex()
        idx.index_messages([_msg("user", "Question 1")])
        idx.mark_compacted()
        idx.index_messages([_msg("user", "Question 2")])

        assert idx._turns[0]["is_compacted"] is True
        assert idx._turns[1]["is_compacted"] is False

    def test_max_turns_bound(self):
        idx = HistoryIndex()
        for i in range(510):
            idx.index_messages([_msg("user", f"Message {i}")])
        assert len(idx._turns) == 500

    def test_persistence_roundtrip(self, tmp_path: Path):
        persist_path = tmp_path / "history.json"
        idx = HistoryIndex(persist_path=persist_path)
        # Index at least 2 docs so BM25 stop-word pruning doesn't remove all terms
        idx.index_messages([_msg("user", "Persistent question")])
        idx.index_messages([_msg("assistant", "Unrelated answer about Java")])
        idx.save()

        idx2 = HistoryIndex(persist_path=persist_path)
        assert idx2.load() is True
        assert len(idx2._turns) == 2
        assert idx2._turns[0]["text"] == "Persistent question"

        # Search should work after reload
        results = idx2.search("persistent", top_k=1)
        assert len(results) == 1
        assert results[0]["text"] == "Persistent question"

    def test_clear(self, tmp_path: Path):
        persist_path = tmp_path / "history.json"
        idx = HistoryIndex(persist_path=persist_path)
        idx.index_messages([_msg("user", "To be cleared")])
        idx.save()
        idx.clear()

        assert len(idx._turns) == 0
        assert not persist_path.exists()

    def test_load_missing_file(self):
        idx = HistoryIndex(persist_path=Path("/nonexistent/path.json"))
        assert idx.load() is False

    def test_search_filters_by_compacted(self):
        idx = HistoryIndex()
        idx.index_messages([_msg("user", "First question about Python")])
        idx.mark_compacted()
        idx.index_messages([_msg("user", "Second question about Java")])

        results = idx.search("Python", top_k=3)
        compacted = [r for r in results if r.get("is_compacted")]
        assert len(compacted) >= 1
        assert compacted[0]["text"] == "First question about Python"

    def test_search_returns_verbatim(self):
        idx = HistoryIndex()
        text = "The exact original text must be preserved."
        msgs = [
            Message(role="user", content=[TextPart(text=text)]),
            Message(role="assistant", content=[TextPart(text="Something completely different.")]),
        ]
        idx.index_messages(msgs)
        results = idx.search("original text", top_k=1)
        assert len(results) >= 1
        assert results[0]["text"] == text

    def test_search_with_recency_boosts_newer_docs(self):
        idx = HistoryIndex()
        # Index docs with a shared rare term plus unrelated docs so BM25 IDF
        # for the shared term is non-zero (needs enough docs for IDF > 0).
        idx.index_messages([_msg("user", "older document about alpha programming")])
        idx._turns[0]["timestamp"] = time.time() - 7200  # 2 hours ago
        idx.index_messages([_msg("assistant", "unrelated answer about java")])
        idx.index_messages([_msg("user", "newer document about alpha programming")])
        idx.index_messages([_msg("assistant", "another unrelated answer about golang")])

        results = idx.search_with_recency("alpha programming", top_k=2, recency_weight=1.0)
        # Filter to the two matching docs
        matching = [r for r in results if "alpha" in r["text"]]
        assert len(matching) == 2
        # With the default 24-hour decay, the 2-hour-old doc gets ~1.92 boost, the new doc gets ~2.0 boost
        # so the newer doc should rank first despite identical BM25 scores
        assert matching[0]["turn_id"] == 2
        assert matching[1]["turn_id"] == 0
        assert matching[0]["boosted_score"] > matching[1]["boosted_score"]

    def test_search_with_recency_weight_zero_falls_back_to_bm25(self):
        idx = HistoryIndex()
        idx.index_messages([_msg("user", "older document about alpha programming")])
        idx._turns[0]["timestamp"] = time.time() - 7200
        idx.index_messages([_msg("assistant", "unrelated answer about java")])
        idx.index_messages([_msg("user", "newer document about alpha programming")])
        idx.index_messages([_msg("assistant", "another unrelated answer about golang")])

        results = idx.search_with_recency("alpha programming", top_k=2, recency_weight=0.0)
        # With zero recency weight, ordering should match pure BM25
        bm25_results = idx.search("alpha programming", top_k=2)
        assert [r["turn_id"] for r in results] == [r["turn_id"] for r in bm25_results]

    def test_search_with_recency_preserves_original_score(self):
        idx = HistoryIndex()
        idx.index_messages([_msg("user", "unique search term xyz")])
        idx.index_messages([_msg("assistant", "something unrelated about java")])
        results = idx.search_with_recency("unique search term xyz", top_k=1)
        assert len(results) == 1
        assert "score" in results[0]
        assert "boosted_score" in results[0]
        assert results[0]["boosted_score"] >= results[0]["score"]
