"""Tests for L5 ProgrammaticMemory (workflows, triggers, tasks)."""

import time

import pytest

from kimix.memory.programmatic_memory import (
    ProgrammaticMemory,
    Workflow,
    Task,
    Trigger,
    TriggerType,
)
from kimix.memory.types import MemoryType


class TestTrigger:
    def test_schedule_trigger_fires(self):
        t = Trigger(trigger_type=TriggerType.SCHEDULE, condition="0.01")
        assert t.should_fire(now=time.time() + 1)

    def test_schedule_trigger_not_fired_yet(self):
        t = Trigger(trigger_type=TriggerType.SCHEDULE, condition="100")
        t.mark_fired()
        assert not t.should_fire(now=time.time() + 1)

    def test_event_trigger_never_fires_on_schedule(self):
        t = Trigger(trigger_type=TriggerType.EVENT, condition="user_login")
        assert not t.should_fire()


class TestWorkflow:
    def test_add_task_and_trigger(self):
        wf = Workflow(name="backup")
        wf.add_trigger(Trigger(TriggerType.SCHEDULE, "3600"))
        wf.add_task(Task(name="dump_db"))
        assert len(wf.triggers) == 1
        assert len(wf.tasks) == 1

    def test_pending_tasks(self):
        wf = Workflow(name="cleanup")
        wf.add_task(Task(name="step1", status="completed"))
        wf.add_task(Task(name="step2", status="pending"))
        assert len(wf.pending_tasks()) == 1
        assert wf.pending_tasks()[0].name == "step2"

    def test_to_memory_entry(self):
        wf = Workflow(name="etl")
        entry = wf.to_memory_entry()
        assert entry.memory_type == MemoryType.WORKFLOW
        assert "etl" in entry.content


class TestProgrammaticMemory:
    def test_register_and_list(self):
        pm = ProgrammaticMemory()
        wf = Workflow(name="daily_report")
        pm.register_workflow(wf)
        assert "daily_report" in pm.list_workflows()

    def test_unregister(self):
        pm = ProgrammaticMemory()
        pm.register_workflow(Workflow(name="x"))
        assert pm.unregister_workflow("x") is True
        assert pm.unregister_workflow("x") is False

    def test_run_pending_schedule(self):
        pm = ProgrammaticMemory()
        wf = Workflow(name="heartbeat")
        wf.add_trigger(Trigger(TriggerType.SCHEDULE, condition="0"))
        wf.add_task(Task(name="ping"))
        pm.register_workflow(wf)
        results = pm.run_pending()
        assert "heartbeat" in results
        assert "ping" in results["heartbeat"]

    def test_run_pending_no_fire(self):
        pm = ProgrammaticMemory()
        wf = Workflow(name="future")
        trig = Trigger(TriggerType.SCHEDULE, condition="999999")
        trig.mark_fired()  # just fired, shouldn't fire again for a long time
        wf.add_trigger(trig)
        wf.add_task(Task(name="later"))
        pm.register_workflow(wf)
        results = pm.run_pending()
        assert "future" not in results

    def test_run_pending_with_runner(self):
        pm = ProgrammaticMemory()
        wf = Workflow(name="compute")
        wf.add_trigger(Trigger(TriggerType.SCHEDULE, condition="0"))
        wf.add_task(Task(name="add"))
        pm.register_workflow(wf)

        def runner(w, t):
            return 42

        results = pm.run_pending(task_runner=runner)
        assert results["compute"] == ["add"]
        assert wf.tasks[0].result == 42

    def test_trigger_event(self):
        pm = ProgrammaticMemory()
        wf = Workflow(name="alert")
        wf.add_trigger(Trigger(TriggerType.EVENT, condition="high_cpu"))
        wf.add_task(Task(name="notify"))
        pm.register_workflow(wf)

        results = pm.trigger_event("high_cpu", {"cpu": 95})
        assert "alert" in results
        assert wf.tasks[0].result == {"cpu": 95}

    def test_trigger_event_no_match(self):
        pm = ProgrammaticMemory()
        pm.register_workflow(Workflow(name="x"))
        assert pm.trigger_event("nonexistent") == {}

    def test_to_entries(self):
        pm = ProgrammaticMemory()
        pm.register_workflow(Workflow(name="wf1"))
        entries = pm.to_entries()
        assert len(entries) == 1
        assert entries[0].memory_type == MemoryType.WORKFLOW

    def test_reflect(self):
        pm = ProgrammaticMemory()
        wf = Workflow(name="a")
        wf.add_task(Task(name="t1"))
        wf.add_task(Task(name="t2"))
        pm.register_workflow(wf)
        assert "1 workflows" in pm.reflect()
        assert "2 tasks" in pm.reflect()
