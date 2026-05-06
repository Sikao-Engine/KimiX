"""Tests to cover missing branches from coverage report."""

import json
import os
import tempfile
import time
from unittest.mock import patch

import numpy as np
import pytest

from kimi_agent_sdk import ToolError
from kimix.memory.embedding import EmbeddingProvider
from kimix.memory.long_term_memory import LongTermMemory
from kimix.memory.retrieval import (
    BM25Scorer,
    InvertedIndex,
    LevenshteinAutomaton,
    NgramTokenizer,
    Searcher,
)
from kimix.memory.short_term_memory import ShortTermMemory
from kimix.memory.sqlite_backend import SQLiteBackend
from kimix.memory.system import AgentMemorySystem
from kimix.memory.tools import (
    GetContext,
    Recall,
    Reflect,
    Remember,
    _get_memory_system,
)
from kimix.memory.types import MemoryEntry, MemoryType
from kimix.memory.working_memory import WorkingMemory


class TestEmbeddingMissing:
    def test_embed_batch_cache_hit_moves_to_end(self):
        provider = EmbeddingProvider(dim=16, max_cache_size=3)
        provider.embed("a")
        provider.embed("b")
        provider.embed_batch(["a", "c"])  # 'a' is cache hit, should move_to_end
        assert len(provider._cache) == 3

    def test_embed_batch_cache_eviction(self):
        provider = EmbeddingProvider(dim=16, max_cache_size=2)
        provider.embed_batch(["a", "b", "c"])  # exceeds max_cache_size
        assert len(provider._cache) == 2

    def test_similarity_zero_norm_second_vector(self):
        provider = EmbeddingProvider(dim=2)
        sim = provider.similarity([1.0, 0.0], [0.0, 0.0])
        assert sim == 0.0


class TestShortTermMemoryMissing:
    def test_evict_empty_buffer(self):
        stm = ShortTermMemory(max_size=3)
        stm._evict_least_valuable()  # should not raise

    def test_active_buffer_now_none(self):
        stm = ShortTermMemory(max_size=10)
        stm.add(MemoryEntry(content="test", memory_type=MemoryType.EPISODIC))
        active = stm._active_buffer()
        assert len(active) == 1


class TestWorkingMemoryMissing:
    def test_get_context_n_zero(self):
        wm = WorkingMemory(max_items=5)
        wm.add(MemoryEntry(content="item", memory_type=MemoryType.EPISODIC))
        assert wm.get_context(0) == []
        assert wm.get_context(-1) == []


class TestSystemMissing:
    def test_sqlite_backend_init(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys = AgentMemorySystem(ltm_path=f"{tmpdir}/ltm.json", use_sqlite=True, db_path=f"{tmpdir}/mem.db")
            assert sys.long_term._backend is not None
            sys.long_term._backend.close()

    def test_recall_context_size_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.perceive("test")
            results = sys.recall("test", context_size=0)
            assert results == {"working": [], "short_term": [], "long_term": []}
        finally:
            os.unlink(path)

    def test_get_context_for_llm_empty_section(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            # No memories at all
            context = sys.get_context_for_llm("test")
            assert context == ""
        finally:
            os.unlink(path)

    def test_get_context_for_llm_max_tokens_break(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.perceive("short")
            # Very low max_tokens to trigger break
            context = sys.get_context_for_llm("short", max_tokens=5)
            # Should be empty because even header exceeds max_tokens
            assert context == ""
        finally:
            os.unlink(path)

    def test_self_reflect_rankings(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            # Store old low-access entry
            old_time = time.time() - 10 * 86400
            entry = sys.remember("old low access", importance=5.0)
            entry.last_accessed = old_time
            entry.access_count = 0
            # Store high-access entry
            entry2 = sys.remember("hot entry", importance=5.0)
            entry2.access_count = 10
            entry2.last_accessed = time.time()
            report = sys.self_reflect()
            assert "Down-ranked stale" in report
            assert "Up-ranked hot" in report
        finally:
            os.unlink(path)


class TestToolsMissing:
    @pytest.mark.asyncio
    async def test_remember_error_path(self):
        tool = Remember()
        with patch("kimix.memory.tools._get_memory_system", side_effect=Exception("boom")):
            result = await tool(Remember.params(content="test", long_term=False))
            assert result.is_error

    @pytest.mark.asyncio
    async def test_recall_error_handler(self):
        tool = Recall()
        with patch("kimix.memory.tools._get_memory_system", side_effect=Exception("boom")):
            result = await tool(Recall.params(query="test"))
            assert result.is_error

    @pytest.mark.asyncio
    async def test_get_context_error_handler(self):
        tool = GetContext()
        with patch("kimix.memory.tools._get_memory_system", side_effect=Exception("boom")):
            result = await tool(GetContext.params(query="test"))
            assert result.is_error

    @pytest.mark.asyncio
    async def test_reflect_error_handler(self):
        tool = Reflect()
        with patch("kimix.memory.tools._get_memory_system", side_effect=Exception("boom")):
            result = await tool(Reflect.params())
            assert result.is_error


class TestSQLiteBackendMissing:
    def test_embedding_to_blob_with_list(self):
        backend = SQLiteBackend._embedding_to_blob([0.1, 0.2, 0.3])
        assert isinstance(backend, bytes)

    def test_insert_tags_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db._insert_tags("id1", [])  # should not raise
            db.close()

    def test_store_many_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store_many([], dim=384)  # should not raise
            db.close()

    def test_store_many_no_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            entry = MemoryEntry(content="no tags", memory_type=MemoryType.SEMANTIC)
            db.store_many([("id1", entry)], dim=384)
            fetched = db.get("id1")
            assert fetched is not None
            assert fetched.content == "no tags"
            db.close()

    def test_iter_rows_with_filters(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="a", memory_type=MemoryType.SEMANTIC, agent_id="ag1"), "id1")
            db.store(MemoryEntry(content="b", memory_type=MemoryType.EPISODIC, agent_id="ag1", expires_at=time.time() - 1), "id2")
            rows = list(db.iter_rows(agent_id="ag1", memory_type=MemoryType.SEMANTIC, exclude_expired=True))
            assert len(rows) == 1
            assert rows[0][1] == "a"
            db.close()

    def test_update_access_many_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.update_access_many([])  # should not raise
            db.close()

    def test_count_with_memory_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="a", memory_type=MemoryType.SEMANTIC), "id1")
            db.store(MemoryEntry(content="b", memory_type=MemoryType.EPISODIC), "id2")
            assert db.count(memory_type=MemoryType.SEMANTIC) == 1
            db.close()

    def test_search_by_tag_empty_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="a", memory_type=MemoryType.SEMANTIC), "id1")
            results = db.search_by_tag([], agent_id=None, exclude_expired=True, dim=384)
            assert len(results) == 1
            db.close()

    def test_store_many_with_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            entry1 = MemoryEntry(content="a", memory_type=MemoryType.SEMANTIC, tags=["t1"])
            entry2 = MemoryEntry(content="b", memory_type=MemoryType.SEMANTIC, tags=["t2"])
            db.store_many([("id1", entry1), ("id2", entry2)], dim=384)
            results = db.search_by_tag(["t1"])
            assert len(results) == 1
            db.close()

    def test_list_all_with_memory_type(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = SQLiteBackend(db_path=f"{tmpdir}/test.db")
            db.store(MemoryEntry(content="a", memory_type=MemoryType.SEMANTIC), "id1")
            db.store(MemoryEntry(content="b", memory_type=MemoryType.EPISODIC), "id2")
            results = db.list_all(memory_type=MemoryType.SEMANTIC)
            assert len(results) == 1
            assert results[0][1].content == "a"
            db.close()


class TestLongTermMemoryMissing:
    def test_invalid_storage_path(self):
        with pytest.raises(ValueError):
            LongTermMemory(storage_path=123, backend=None)  # type: ignore[arg-type]

    def test_build_bm25_skips_expired_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.store("fresh", importance=5.0)
            ltm.store("expired", importance=5.0, expires_at=time.time() - 1)
            searcher = ltm._build_bm25()
            assert ltm._next_doc_id == 1
            backend.close()

    def test_get_entry_backend_wrong_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.store("secret", importance=5.0)
            eid = ltm._hash("secret")
            # Simulate different agent by creating new LTM with same backend
            ltm2 = LongTermMemory(backend=backend, agent_id="other")
            assert ltm2._get_entry(eid) is None
            backend.close()

    def test_load_skips_different_agent(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm1 = LongTermMemory(storage_path=path, agent_id="agent1")
            ltm1.store("secret", importance=5.0)
            del ltm1
            ltm2 = LongTermMemory(storage_path=path, agent_id="agent2")
            assert ltm2.count() == 0
        finally:
            os.unlink(path)

    def test_save_not_dirty(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("item", importance=5.0)
            ltm._save()  # saves
            assert not ltm._dirty
            ltm._save()  # should return early
        finally:
            os.unlink(path)

    def test_store_many(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            results = ltm.store_many([
                {"content": "item1", "importance": 5.0},
                {"content": "item2", "importance": 6.0},
            ])
            assert len(results) == 2
            assert ltm.count() == 2
        finally:
            os.unlink(path)

    def test_store_many_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            results = ltm.store_many([
                {"content": "item1", "importance": 5.0},
                {"content": "item2", "importance": 6.0},
            ])
            assert len(results) == 2
            assert ltm.count() == 2
            backend.close()

    def test_retrieve_tag_filter_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.store("tagged", importance=5.0, tags=["py"])
            ltm.store("tagged2", importance=5.0, tags=["py"])
            ltm.store("expired", importance=5.0, tags=["py"], expires_at=time.time() - 1)
            results = ltm.retrieve("tagged", tag_filter=["py"])
            assert len(results) == 2
            backend.close()

    def test_retrieve_tag_filter_intersection(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("a", importance=5.0, tags=["x", "y"])
            ltm.store("b", importance=5.0, tags=["x"])
            # Two tags, both exist, intersection has "a"
            results = ltm.retrieve("a", tag_filter=["x", "y"])
            assert len(results) == 1
            assert results[0].content == "a"
        finally:
            os.unlink(path)

    def test_retrieve_tag_filter_empty_intersection(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("a", importance=5.0, tags=["x"])
            ltm.store("b", importance=5.0, tags=["y"])
            # Two tags, both exist, but intersection is empty
            results = ltm.retrieve("a", tag_filter=["x", "y"])
            assert results == []
        finally:
            os.unlink(path)

    def test_retrieve_tag_filter_missing_tag(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("item", importance=5.0, tags=["a"])
            results = ltm.retrieve("item", tag_filter=["nonexistent"])
            assert results == []
        finally:
            os.unlink(path)

    def test_retrieve_missing_embeddings(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            entry = MemoryEntry(content="no embedding yet", memory_type=MemoryType.SEMANTIC, importance=5.0)
            entry.embedding = None
            eid = ltm._hash("no embedding yet")
            ltm.entries[eid] = entry
            ltm._update_index(eid, entry)
            results = ltm.retrieve("no embedding")
            assert len(results) == 1
            assert results[0].embedding is not None
        finally:
            os.unlink(path)

    def test_retrieve_zero_query_norm(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("item", importance=5.0)
            results = ltm.retrieve("item", query_vec=np.zeros(384, dtype=np.float32))
            assert len(results) == 1  # zero query norm shouldn't crash
        finally:
            os.unlink(path)

    def test_retrieve_argpartition_path(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            for i in range(20):
                ltm.store(f"item {i}", importance=5.0)
            results = ltm.retrieve("item", top_k=2)
            assert len(results) == 2
        finally:
            os.unlink(path)

    def test_consolidate_type_error(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            with pytest.raises(TypeError):
                ltm.consolidate("not a ShortTermMemory", threshold=7.0)
        finally:
            os.unlink(path)

    def test_get_entry_in_memory(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("item", importance=5.0)
            eid = ltm._hash("item")
            assert ltm._get_entry(eid) is not None
            assert ltm._get_entry("nonexistent") is None
        finally:
            os.unlink(path)

    def test_forget_backend_none_entry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.forget("nonexistent")  # should not raise
            backend.close()

    def test_forget_many_backend_with_store_and_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.store("a", importance=0.5)  # 0.5 * 0.5 = 0.25 >= 0.1 => store back
            eid_a = ltm._hash("a")
            ltm.forget_many([eid_a, "nonexistent"])  # covers continue and store
            entry = ltm._get_entry(eid_a)
            assert entry is not None
            assert entry.importance == 0.25
            backend.close()

    def test_forget_many_in_memory_with_none(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("a", importance=0.5)
            eid_a = ltm._hash("a")
            ltm.forget_many([eid_a, "nonexistent"])  # covers continue in-memory
            entry = ltm._get_entry(eid_a)
            assert entry is not None
            assert entry.importance == 0.25
        finally:
            os.unlink(path)

    def test_forget_cleanup_tags(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("forgettable", importance=1.0, tags=["unique_tag"])
            eid = ltm._hash("forgettable")
            # importance 1.0 -> forget -> 0.5 -> forget -> 0.25 -> forget -> 0.125 -> forget -> 0.0625 < 0.1
            for _ in range(4):
                ltm.forget(eid)
                if eid not in ltm.entries:
                    break
            assert eid not in ltm.entries
            assert "unique_tag" not in ltm.index
        finally:
            os.unlink(path)

    def test_forget_many_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.store("a", importance=0.15)
            ltm.store("b", importance=0.15)
            eid_a = ltm._hash("a")
            eid_b = ltm._hash("b")
            ltm.forget_many([eid_a, eid_b])  # 0.15 * 0.5 = 0.075 < 0.1 => deleted
            assert ltm.count() == 0
            backend.close()

    def test_forget_many_in_memory(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("a", importance=0.15, tags=["t1"])
            ltm.store("b", importance=0.15, tags=["t1"])
            eid_a = ltm._hash("a")
            eid_b = ltm._hash("b")
            ltm.forget_many([eid_a, eid_b])  # 0.15 * 0.5 = 0.075 < 0.1 => deleted
            assert ltm.count() == 0
            assert "t1" not in ltm.index
        finally:
            os.unlink(path)


class TestRetrievalMissingExtra:
    def test_accumulate_candidate_docs_not_none_postings_none(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.finalize(stop_threshold=1.0)
        scorer = BM25Scorer(idx)
        arr = scorer._accumulate(["missing"], {0})
        assert np.all(arr == 0)

    def test_dl_2x2_no_match(self):
        dl = LevenshteinAutomaton._damerau_levenshtein
        assert dl("ab", "cd") == 2

    def test_match_terms_by_length_no_prefix(self):
        class MockDict:
            _terms_by_length = {
                4: ("helo", "help"),
                5: ("hello",),
            }
        auto = LevenshteinAutomaton("helo", max_edits=1, prefix_length=0)
        results = auto.match(MockDict(), max_expansions=50)
        assert "helo" in results
        assert "hello" in results

    def test_match_generic_dict_max_expansions(self):
        auto = LevenshteinAutomaton("he", max_edits=1, prefix_length=1)
        results = auto.match(["he", "ha", "hb", "hc"], max_expansions=2)
        assert len(results) == 2

    def test_match_generic_dict_length_skip(self):
        auto = LevenshteinAutomaton("he", max_edits=0, prefix_length=1)
        results = auto.match(["he", "hello", "h"], max_expansions=50)
        assert results == ["he"]

    def test_match_generic_dict_prefix_filter(self):
        auto = LevenshteinAutomaton("he", max_edits=1, prefix_length=1)
        results = auto.match(["he", "ha", "xe"], max_expansions=50)
        assert "he" in results
        assert "ha" in results
        assert "xe" not in results

    def test_match_prefix_length_1_inner_return(self):
        idx = InvertedIndex()
        idx.add_document(0, ["aa", "ab", "ac"])
        idx.finalize(stop_threshold=1.0)
        auto = LevenshteinAutomaton("a", max_edits=1, prefix_length=1)
        results = auto.match(idx, max_expansions=1)
        assert len(results) == 1

    def test_match_prefix_length_2_inner_return(self):
        class MockDict:
            _terms_by_length_prefix = {
                (3, "ab"): ("abc", "abd", "abe"),
            }
            _terms_by_length = {}
        auto = LevenshteinAutomaton("abc", max_edits=1, prefix_length=2)
        results = auto.match(MockDict(), max_expansions=1)
        assert len(results) == 1

    def test_match_prefix_length_2_prefix_mismatch(self):
        class MockDict:
            _terms_by_length_prefix = {
                (3, "ab"): ("abc", "xyz"),
            }
            _terms_by_length = {}
        auto = LevenshteinAutomaton("abc", max_edits=1, prefix_length=2)
        results = auto.match(MockDict(), max_expansions=50)
        assert "abc" in results
        assert "xyz" not in results

    def test_match_terms_by_length_prefix_1_mismatch(self):
        class MockDict:
            _terms_by_length = {
                2: ("he", "ha", "xe"),
            }
        auto = LevenshteinAutomaton("he", max_edits=1, prefix_length=1)
        results = auto.match(MockDict(), max_expansions=50)
        assert "he" in results
        assert "ha" in results
        assert "xe" not in results

    def test_match_terms_by_length_prefix_2_mismatch(self):
        class MockDict:
            _terms_by_length = {
                3: ("abc", "abd", "xyz"),
            }
        auto = LevenshteinAutomaton("abc", max_edits=1, prefix_length=2)
        results = auto.match(MockDict(), max_expansions=50)
        assert "abc" in results
        assert "abd" in results
        assert "xyz" not in results

    def test_match_terms_by_length_max_expansions(self):
        class MockDict:
            _terms_by_length = {
                2: ("he", "ha", "hb"),
            }
        auto = LevenshteinAutomaton("he", max_edits=1, prefix_length=0)
        results = auto.match(MockDict(), max_expansions=2)
        assert len(results) == 2

    def test_match_prefix_length_2_freq_filter(self):
        class MockDict:
            _terms_by_length_prefix = {
                (4, "ab"): ("abcd", "abZZ"),
            }
            _terms_by_length = {}
        auto = LevenshteinAutomaton("abcd", max_edits=1, prefix_length=2)
        results = auto.match(MockDict(), max_expansions=50)
        assert "abcd" in results
        assert "abZZ" not in results

    def test_match_prefix_length_2_outer_return(self):
        class MockDict:
            _terms_by_length_prefix = {
                (4, "ab"): ("abcd",),
                (5, "ab"): ("abcde",),
            }
            _terms_by_length = {}
        auto = LevenshteinAutomaton("abcd", max_edits=1, prefix_length=2)
        results = auto.match(MockDict(), max_expansions=1)
        assert len(results) == 1

    def test_match_terms_by_length_freq_filter(self):
        class MockDict:
            _terms_by_length = {
                4: ("abcd", "abZZ"),
            }
        auto = LevenshteinAutomaton("abcd", max_edits=1, prefix_length=0)
        results = auto.match(MockDict(), max_expansions=50)
        assert "abcd" in results
        assert "abZZ" not in results

    def test_match_terms_by_length_outer_return(self):
        class MockDict:
            _terms_by_length = {
                4: ("abcd",),
                5: ("abcde",),
            }
        auto = LevenshteinAutomaton("abcd", max_edits=1, prefix_length=0)
        results = auto.match(MockDict(), max_expansions=1)
        assert len(results) == 1


class TestLongTermMemoryRetrievalMissing:
    def test_retrieve_tag_filter_sqlite_min_importance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SQLiteBackend(db_path=f"{tmpdir}/ltm.db")
            ltm = LongTermMemory(backend=backend, agent_id="test")
            ltm.store("tagged low", importance=3.0, tags=["py"])
            ltm.store("tagged high", importance=8.0, tags=["py"])
            results = ltm.retrieve("tagged", tag_filter=["py"], min_importance=5.0)
            assert len(results) == 1
            assert results[0].content == "tagged high"
            backend.close()


class TestRetrievalMissing:
    def test_detect_n_empty(self):
        t = NgramTokenizer(n=2)
        assert t._detect_n("") == 2

    def test_doc_lengths_property(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a", "b"])
        idx.finalize()
        assert idx.doc_lengths == [2]

    def test_is_stop_ngram_empty(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.finalize()
        assert idx._is_stop_ngram("", df=1) is True

    def test_is_stop_ngram_punctuation(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.finalize()
        assert idx._is_stop_ngram("!!", df=1) is True

    def test_is_stop_ngram_df_threshold(self):
        idx = InvertedIndex()
        idx.add_document(0, ["aaa"])
        idx.add_document(1, ["aaa"])
        idx.finalize(stop_threshold=0.5)
        assert idx._is_stop_ngram("aaa", df=2) is True

    def test_finalize_prune_df(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a", "b"])
        idx.add_document(1, ["a", "b"])
        idx.finalize(prune_df=1)  # prune terms with df > 1
        assert not idx.has_term("a")

    def test_get_postings_auto_finalize(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.add_document(1, ["world"])
        idx.add_document(2, ["foo"])
        # Not explicitly finalized; term 'hello' has df=1, N=3, so 1 <= 3*0.5=1.5, not pruned
        postings = idx.get_postings("hello")
        assert postings is not None

    def test_is_stop_ngram_punctuation_reaches_check(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.add_document(1, ["b"])
        idx.finalize()
        # N=2, threshold=0.5, df=1, so 1 > 1.0 is False; reaches punctuation check
        assert idx._is_stop_ngram("!!", df=1) is True

    def test_is_stop_ngram_not_stop(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.add_document(1, ["b"])
        idx.finalize()
        assert idx._is_stop_ngram("hello", df=1) is False

    def test_finalize_already_finalized(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.finalize()
        idx.finalize()  # should return early without error

    def test_finalize_prune_df_reaches_line(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a", "b"])
        idx.add_document(1, ["a", "b"])
        idx.add_document(2, ["c", "d"])
        idx.add_document(3, ["c", "d"])
        # N=4, df('a')=2, stop_threshold=0.5 -> 2 > 2.0 is False, so not pruned by stop
        # prune_df=1 -> df=2 > 1, so pruned
        idx.finalize(stop_threshold=0.5, prune_df=1)
        assert not idx.has_term("a")

    def test_save_auto_finalize(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.add_document(1, ["world"])
        # Not explicitly finalized
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/sub/index.pkl"
            idx.save(path)
            assert os.path.exists(path)

    def test_terms(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.add_document(1, ["hello"])
        idx.finalize(stop_threshold=1.0)
        assert list(idx.terms()) == ["hello"]

    def test_save_parent_mkdir(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.finalize()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = f"{tmpdir}/sub/dir/index.pkl"
            idx.save(path)
            assert os.path.exists(path)

    def test_accumulate_no_candidates_postings_none(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.finalize()
        scorer = BM25Scorer(idx)
        # query with token not in index
        arr = scorer._accumulate(["missing"], None)
        assert np.all(arr == 0)

    def test_accumulate_isin_path(self):
        idx = InvertedIndex()
        for i in range(300):
            idx.add_document(i, ["word"] if i % 2 == 0 else ["other"])
        idx.finalize(stop_threshold=1.0)
        scorer = BM25Scorer(idx)
        candidates = set(range(300))
        arr = scorer._accumulate(["word"], candidates)
        assert np.any(arr > 0)

    def test_accumulate_df_zero_after_filter(self):
        idx = InvertedIndex()
        idx.add_document(0, ["word"])
        idx.finalize(stop_threshold=1.0)
        scorer = BM25Scorer(idx)
        # candidate docs that don't contain the word -> filtered out
        arr = scorer._accumulate(["word"], {999})
        assert np.all(arr == 0)

    def test_score_topk_zero(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.finalize()
        scorer = BM25Scorer(idx)
        assert scorer.score_topk(["a"], top_k=0) == []

    def test_dl_n_zero(self):
        dl = LevenshteinAutomaton._damerau_levenshtein
        assert dl("", "") == 0

    def test_dl_2x2_equal(self):
        dl = LevenshteinAutomaton._damerau_levenshtein
        assert dl("ab", "ab") == 0

    def test_dl_2x2_one_same(self):
        dl = LevenshteinAutomaton._damerau_levenshtein
        assert dl("ab", "ac") == 1

    def test_dl_2x2_transpose(self):
        dl = LevenshteinAutomaton._damerau_levenshtein
        assert dl("ab", "ba") == 1

    def test_match_prefix_length_1_early_return(self):
        idx = InvertedIndex()
        idx.add_document(0, ["apple", "apricot", "banana"])
        idx.finalize(stop_threshold=1.0)
        auto = LevenshteinAutomaton("app", max_edits=1, prefix_length=1)
        # Should trigger early return path when max_expansions reached
        results = auto.match(idx, max_expansions=1)
        assert len(results) <= 1

    def test_match_terms_by_length_prefix(self):
        class MockDict:
            _terms_by_length_prefix = {
                (4, "he"): ("helo", "help"),
                (5, "he"): ("hello",),
            }
            _terms_by_length = {
                4: ("helo", "help"),
                5: ("hello",),
            }
        auto = LevenshteinAutomaton("helo", max_edits=1, prefix_length=2)
        results = auto.match(MockDict(), max_expansions=50)
        assert "helo" in results
        assert "hello" in results

    def test_match_terms_by_length(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.finalize(stop_threshold=1.0)
        auto = LevenshteinAutomaton("helo", max_edits=1, prefix_length=0)
        results = auto.match(idx, max_expansions=50)
        assert "hello" in results

    def test_match_generic_dict(self):
        auto = LevenshteinAutomaton("helo", max_edits=1, prefix_length=0)
        results = auto.match(["hello", "world", "help"], max_expansions=50)
        assert "hello" in results

    def test_match_generic_dict_prefix_1(self):
        auto = LevenshteinAutomaton("helo", max_edits=1, prefix_length=1)
        results = auto.match(["hello", "world", "help"], max_expansions=50)
        assert "hello" in results

    def test_expand_token_max_edits_zero(self):
        tokenizer = NgramTokenizer(n=3)
        idx = InvertedIndex()
        tokens = tokenizer.tokenize("hello world")
        idx.add_document(0, tokens)
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx, tokenizer=tokenizer, fuzziness=0)
        results = searcher.search("hello world")
        assert len(results) == 1

    def test_search_expanded_tokens_empty(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx, fuzziness=0)
        # Token not in index and max_edits=0 => no expansion
        results = searcher.search("xyz")
        assert results == []


class TestIntegrationMissing:
    def test_memory_entry_from_dict(self):
        d = {
            "content": "test",
            "memory_type": "semantic",
            "timestamp": 1234567890.0,
            "importance": 5.0,
            "access_count": 3,
            "last_accessed": 1234567890.0,
            "embedding": [0.1, 0.2],
            "tags": ["a", "b"],
            "source": "src",
            "metadata": {"k": "v"},
            "expires_at": 1234567890.0,
            "agent_id": "agent1",
        }
        entry = MemoryEntry.from_dict(d)
        assert entry.content == "test"
        assert entry.memory_type == MemoryType.SEMANTIC
        assert entry.embedding == [0.1, 0.2]

    def test_memory_entry_to_dict_with_now(self):
        entry = MemoryEntry(content="test", memory_type=MemoryType.SEMANTIC)
        d = entry.to_dict(now=time.time())
        assert "effective_importance" in d
