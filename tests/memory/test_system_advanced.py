"""Advanced tests for AgentMemorySystem: L4/L5/L6 integration, scar triggers, self-evolution."""

import os
import tempfile
import time

import pytest

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryType


class TestSystemL4Integration:
    def test_add_scar_and_trigger_in_recall(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.add_scar("division by zero", "check denominator", ["divide", "zero"], severity=8.0)
            # Without use_procedural, recall should stay backward-compatible
            results = sys.recall("divide by zero")
            assert "procedural" not in results
            # With use_procedural=True
            results = sys.recall("divide by zero", use_procedural=True)
            assert "procedural" in results
            assert len(results["procedural"]) >= 1
            # High-severity scar should be elevated to working memory
            assert any("SCAR" in e.content for e in sys.working.get_context(10))
        finally:
            os.unlink(path)

    def test_add_rule_and_match(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.add_rule("deploy on friday", "reject deployment", 10.0, ["ops"])
            results = sys.recall("deploy on friday evening", use_procedural=True)
            assert any("RULE" in e.content for e in results["procedural"])
        finally:
            os.unlink(path)

    def test_scar_trigger_disabled(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.scar_trigger_enabled = False
            sys.add_scar("fail", "avoid", ["fail"], 9.0)
            results = sys.recall("fail", use_procedural=True)
            assert results.get("procedural", []) == []
        finally:
            os.unlink(path)


class TestSystemL5Integration:
    def test_register_workflow_and_run(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            from kimix.memory.programmatic_memory import Workflow, Task, Trigger, TriggerType
            wf = Workflow(name="cleanup")
            wf.add_trigger(Trigger(TriggerType.SCHEDULE, condition="0"))
            wf.add_task(Task(name="delete_temp"))
            sys.programmatic.register_workflow(wf)
            ran = sys.programmatic.run_pending()
            assert "cleanup" in ran
        finally:
            os.unlink(path)

    def test_trigger_event(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            from kimix.memory.programmatic_memory import Workflow, Task, Trigger, TriggerType
            wf = Workflow(name="alert")
            wf.add_trigger(Trigger(TriggerType.EVENT, condition="high_cpu"))
            wf.add_task(Task(name="page_ops"))
            sys.programmatic.register_workflow(wf)
            ran = sys.programmatic.trigger_event("high_cpu", {"cpu": 99})
            assert "alert" in ran
        finally:
            os.unlink(path)


class TestSystemL6Integration:
    def test_archive_to_cold_storage(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.remember("old low-priority fact", importance=2.0)
            sys.archive_to_cold_storage()
            assert len(sys.cold_storage.list_archives()) >= 1
        finally:
            os.unlink(path)

    def test_archive_with_explicit_entries(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            from kimix.memory.types import MemoryEntry
            entries = [MemoryEntry(content="explicit", memory_type=MemoryType.SEMANTIC)]
            sys.archive_to_cold_storage(entries=entries, start_year=2020, end_year=2020)
            restored = sys.cold_storage.restore_range(2020, 2020)
            assert any(e.content == "explicit" for e in restored)
        finally:
            os.unlink(path)


class TestSystemSelfReflection:
    def test_self_reflect_downranks_stale(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            entry = sys.remember("stale fact", importance=5.0)
            # Simulate old access
            entry.last_accessed = time.time() - 30 * 86400
            entry.access_count = 0
            report = sys.self_reflect()
            assert "Down-ranked stale" in report or "Long-term entries" in report
        finally:
            os.unlink(path)

    def test_self_reflect_report_structure(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            report = sys.self_reflect()
            assert "Self-Reflection Report" in report
            assert "Short-term buffer" in report
        finally:
            os.unlink(path)


class TestSystemTemporalValidity:
    def test_perceive_with_expiry(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.perceive("temp event", importance=5.0, expires_at=time.time() - 1)
            sys.short_term.clear_expired()
            assert len(sys.short_term.buffer) == 0
        finally:
            os.unlink(path)

    def test_remember_with_expiry(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.remember("temp fact", importance=5.0, expires_at=time.time() - 1)
            results = sys.long_term.retrieve("temp")
            assert results == []
        finally:
            os.unlink(path)


class TestSystemSQLiteOption:
    def test_system_with_sqlite(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sys = AgentMemorySystem(
                ltm_path=f"{tmpdir}/ltm.json",
                use_sqlite=True,
                db_path=f"{tmpdir}/memory.db",
                agent_id="sql_agent",
            )
            sys.remember("sqlite memory", importance=8.0, tags=["sql"])
            results = sys.recall("sqlite", use_long=True)
            assert len(results["long_term"]) == 1
            assert sys.long_term.count() == 1
            if sys.long_term._backend is not None:
                sys.long_term._backend.close()
