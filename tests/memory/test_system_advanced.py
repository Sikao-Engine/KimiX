"""Advanced tests for AgentMemorySystem: procedural, programmatic, cold storage, self-evolution."""

import os
import tempfile
import time

import pytest

from kimix.memory.system import AgentMemorySystem
from kimix.memory.types import MemoryType


class TestSystemProceduralIntegration:
    def test_add_scar(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.add_scar("division by zero", "check denominator", ["divide", "zero"], severity=8.0)
            assert len(sys.procedural.scars) == 1
            assert sys.procedural.scars[0].failure_pattern == "division by zero"
        finally:
            os.unlink(path)

    def test_add_rule(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            sys = AgentMemorySystem(ltm_path=path)
            sys.add_rule("deploy on friday", "reject deployment", 10.0, ["ops"])
            assert len(sys.procedural.rules) == 1
            assert sys.procedural.rules[0].condition == "deploy on friday"
        finally:
            os.unlink(path)


class TestSystemProgrammaticIntegration:
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


class TestSystemColdStorageIntegration:
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
