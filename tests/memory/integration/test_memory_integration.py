"""Integration tests for full memory system workflow."""

import os
import tempfile

import pytest

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryType


class TestMemoryIntegration:
    def test_full_workflow(self):
        """End-to-end workflow: perceive, remember, recall, consolidate."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            memory = AgentMemorySystem(dim=384, ltm_path=path)

            # 1. Pre-load long-term knowledge
            memory.remember(
                "Python's GIL limits multi-threaded parallel execution efficiency",
                importance=9.0,
                tags=["python", "concurrency", "gil"],
                memory_type=MemoryType.SEMANTIC,
            )
            memory.remember(
                "Async programming uses async/await keywords, based on event loops",
                importance=8.5,
                tags=["python", "async", "concurrency"],
                memory_type=MemoryType.SEMANTIC,
            )
            memory.remember(
                "User Alice prefers using FastAPI framework to build web services",
                importance=7.0,
                tags=["user_preference", "alice", "fastapi"],
                memory_type=MemoryType.SEMANTIC,
            )
            assert len(memory.long_term.entries) == 3

            # 2. Perceive environment
            memory.perceive(
                "User asks: 'How to handle high concurrency requests in Python?'",
                importance=8.0,
                tags=["query", "concurrency", "python"],
                source="user_interaction",
            )
            memory.perceive(
                "System detects current CPU usage at 85%",
                importance=6.0,
                tags=["system_status", "performance"],
                source="system_monitor",
            )
            assert len(memory.working.items) == 2
            assert len(memory.short_term.buffer) == 2

            # 3. Recall
            results = memory.recall(
                query="Python async programming and concurrency handling",
                use_working=True,
                use_short=True,
                use_long=True,
            )
            assert len(results["working"]) > 0
            assert len(results["short_term"]) > 0
            assert len(results["long_term"]) > 0

            # 4. Generate LLM context
            context = memory.get_context_for_llm(
                "How to optimize Python concurrency performance", max_tokens=1000
            )
            assert "Python" in context or "async" in context or "concurrency" in context

            # 5. Tag-filtered retrieval
            user_prefs = memory.long_term.retrieve(
                query="User preference",
                tag_filter=["user_preference"],
                top_k=3,
            )
            assert len(user_prefs) == 1
            assert "Alice" in user_prefs[0].content

            # 6. Reflect
            report = memory.reflect()
            assert "Working Memory" in report
            assert "Long-term Memory: 3 items" in report

        finally:
            os.unlink(path)

    def test_memory_consolidation(self):
        """Test automatic consolidation from STM to LTM."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            memory = AgentMemorySystem(ltm_path=path)
            memory.consolidation_interval = 10

            # Add high-importance items to short-term
            for i in range(10):
                memory.perceive(
                    f"Important event {i}",
                    importance=9.0,
                    tags=["important"],
                )

            # After 10 perceptions, consolidation triggers
            assert memory.interaction_count == 10
            # High-importance items should have been consolidated to LTM
            assert len(memory.long_term.entries) > 0

        finally:
            os.unlink(path)

    def test_ttl_expiration(self):
        """Test that expired STM entries are cleared."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            memory = AgentMemorySystem(ltm_path=path)
            memory.short_term.ttl = 0.01  # 10ms TTL

            memory.perceive("old event", importance=5.0)
            import time
            time.sleep(0.05)

            memory._consolidate()  # This calls clear_expired
            assert len(memory.short_term.buffer) == 0

        finally:
            os.unlink(path)

    def test_working_memory_fifo(self):
        """Test working memory FIFO behavior."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            memory = AgentMemorySystem(ltm_path=path)
            # Recreate working memory with capacity 3
            memory.working = memory.working.__class__(max_items=3)

            for i in range(5):
                memory.perceive(f"item {i}", importance=5.0)

            context = memory.working.get_context(10)
            assert len(context) == 3
            assert context[0].content == "item 2"
            assert context[-1].content == "item 4"

        finally:
            os.unlink(path)

    def test_persistence_across_sessions(self):
        """Test that LTM persists across system instances."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            # Session 1
            memory1 = AgentMemorySystem(ltm_path=path)
            memory1.remember("persistent fact", importance=9.0, tags=["test"])
            del memory1

            # Session 2
            memory2 = AgentMemorySystem(ltm_path=path)
            results = memory2.long_term.retrieve("persistent")
            assert len(results) == 1
            assert results[0].content == "persistent fact"

        finally:
            os.unlink(path)

    def test_forgetting_curve(self):
        """Test active forgetting reduces importance."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            memory = AgentMemorySystem(ltm_path=path)
            entry = memory.remember("forgettable", importance=1.0)
            entry_id = memory.long_term._hash("forgettable")

            # After first forget: 1.0 * 0.5 = 0.5 (not < 0.1)
            # After second forget: 0.5 * 0.5 = 0.25 (not < 0.1)
            # After third forget: 0.25 * 0.5 = 0.125 (not < 0.1)
            # After fourth forget: 0.125 * 0.5 = 0.0625 (< 0.1 -> removed)
            for _ in range(4):
                memory.long_term.forget(entry_id)
                if entry_id not in memory.long_term.entries:
                    break
            assert entry_id not in memory.long_term.entries

        finally:
            os.unlink(path)
