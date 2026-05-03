"""Edge-case tests for LongTermMemory: duplicates, empty filters, CJK, thresholds."""

import os
import tempfile
import time

import pytest

from kimix.memory.long_term_memory import LongTermMemory
from kimix.memory.types import MemoryEntry, MemoryType


class TestLTMDuplicates:
    def test_store_same_content_overwrites(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            e1 = ltm.store("duplicate", importance=3.0)
            e2 = ltm.store("duplicate", importance=8.0)
            # Same hash => should overwrite in dict backend
            assert ltm.count() == 1
            results = ltm.retrieve("duplicate")
            assert results[0].importance == 8.0
        finally:
            os.unlink(path)

    def test_store_empty_content(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            entry = ltm.store("", importance=5.0)
            assert entry.content == ""
            results = ltm.retrieve("")
            # Empty query on empty content may or may not match depending on embedding
            assert isinstance(results, list)
        finally:
            os.unlink(path)


class TestLTMRetrieveEdgeCases:
    def test_retrieve_tag_filter_no_match(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("item", tags=["a"])
            results = ltm.retrieve("item", tag_filter=["nonexistent"])
            assert results == []
        finally:
            os.unlink(path)

    def test_retrieve_min_importance_exact_boundary(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("exactly five", importance=5.0)
            results = ltm.retrieve("exactly five", min_importance=5.0)
            assert len(results) == 1
        finally:
            os.unlink(path)

    def test_retrieve_top_k_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("something", importance=5.0)
            results = ltm.retrieve("something", top_k=0)
            assert results == []
        finally:
            os.unlink(path)

    def test_retrieve_empty_store(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            assert ltm.retrieve("anything") == []
        finally:
            os.unlink(path)

    def test_retrieve_with_bm25_weight_zero(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("python asyncio", importance=5.0)
            results = ltm.retrieve("python", bm25_weight=0.0)
            assert len(results) == 1
        finally:
            os.unlink(path)

    def test_retrieve_with_bm25_weight_one(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("python asyncio", importance=5.0)
            results = ltm.retrieve("python", bm25_weight=1.0)
            assert len(results) == 1
        finally:
            os.unlink(path)


class TestLTMCJKContent:
    def test_store_and_retrieve_cjk(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("Python异步编程指南", importance=8.0, tags=["python", "中文"])
            results = ltm.retrieve("异步")
            assert len(results) == 1
            assert "异步" in results[0].content
        finally:
            os.unlink(path)


class TestLTMForgetEdgeCases:
    def test_forget_missing_entry_id(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.forget("nonexistent_id")  # should not raise
            assert True
        finally:
            os.unlink(path)

    def test_forget_drops_below_threshold(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            ltm = LongTermMemory(storage_path=path)
            ltm.store("borderline", importance=0.15)
            eid = ltm._hash("borderline")
            ltm.forget(eid)  # 0.15 * 0.5 = 0.075 < 0.1 => deleted
            assert eid not in ltm.entries
        finally:
            os.unlink(path)
