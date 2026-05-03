"""Tests for AgentMemorySystem."""

import os
import tempfile

import pytest

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryType


class TestAgentMemorySystem:
    def test_perceive(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            entry = sys.perceive("test observation", importance=7.0)
            assert entry.content == "test observation"
            assert len(sys.working.items) == 1
            assert len(sys.short_term.buffer) == 1
        finally:
            os.unlink(path)

    def test_recall(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.perceive("python asyncio", importance=8.0, tags=["python", "async"])
            sys.remember("python threading guide", importance=7.0, tags=["python", "threading"])
            results = sys.recall("python concurrency")
            assert len(results["working"]) > 0 or len(results["short_term"]) > 0 or len(results["long_term"]) > 0
        finally:
            os.unlink(path)

    def test_recall_tier_selection(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.perceive("test")
            results = sys.recall("test", use_working=False, use_short=False, use_long=False)
            assert results == {"working": [], "short_term": [], "long_term": []}
        finally:
            os.unlink(path)

    def test_remember(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            entry = sys.remember("important fact", importance=9.0, tags=["fact"])
            assert entry.content == "important fact"
            assert sys.long_term.count() == 1
        finally:
            os.unlink(path)

    def test_get_context_for_llm(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.perceive("user query about python")
            sys.remember("python is a language")
            context = sys.get_context_for_llm("python", max_tokens=500)
            assert "python" in context.lower()
        finally:
            os.unlink(path)

    def test_reflect(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.perceive("test")
            report = sys.reflect()
            assert "Working Memory" in report
            assert "Short-term Memory" in report
            assert "Long-term Memory" in report
            assert "Interactions" in report
        finally:
            os.unlink(path)

    def test_consolidation_trigger(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.consolidation_interval = 5
            for i in range(5):
                sys.perceive(f"observation {i}", importance=8.0)
            # After 5 perceptions, consolidation should trigger
            assert sys.interaction_count == 5
        finally:
            os.unlink(path)

    def test_tag_filter_in_recall(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path, use_sqlite=False)
            sys.remember("user likes fastapi", tags=["user_preference"])
            sys.remember("python guide", tags=["guide"])
            results = sys.recall("user", tag_filter=["user_preference"])
            assert len(results["long_term"]) == 1
            assert results["long_term"][0].content == "user likes fastapi"
        finally:
            os.unlink(path)
