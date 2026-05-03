"""Tests for L4 ProceduralMemory (scars and rules)."""

import pytest

from kimix.memory.procedural_memory import ProceduralMemory, ScarEntry, RuleEntry
from kimix.memory.types import MemoryType


class TestScarEntry:
    def test_scar_to_memory_entry(self):
        scar = ScarEntry(
            failure_pattern="Division by zero",
            lesson="Always check denominator",
            trigger_conditions=["divide", "zero"],
            severity=8.0,
        )
        entry = scar.to_memory_entry()
        assert entry.memory_type == MemoryType.SCAR
        assert "Division by zero" in entry.content
        assert entry.importance == 8.0
        assert "scar" in entry.tags


class TestRuleEntry:
    def test_rule_to_memory_entry(self):
        rule = RuleEntry(
            condition="user asks about pricing",
            action="refer to pricing page",
            priority=7.0,
            tags=["sales"],
        )
        entry = rule.to_memory_entry()
        assert entry.memory_type == MemoryType.RULE
        assert "pricing page" in entry.content
        assert entry.importance == 7.0
        assert "sales" in entry.tags


class TestProceduralMemory:
    def test_add_scar(self):
        pm = ProceduralMemory()
        scar = pm.add_scar("timeout", "increase retry delay", ["timeout"], 6.0)
        assert len(pm.scars) == 1
        assert pm.scars[0].failure_pattern == "timeout"

    def test_add_rule(self):
        pm = ProceduralMemory()
        rule = pm.add_rule("cpu high", "scale out", 9.0, ["ops"])
        assert len(pm.rules) == 1
        assert pm.rules[0].action == "scale out"

    def test_match_scars_by_keyword(self):
        pm = ProceduralMemory()
        pm.add_scar("db connection lost", "use pool", ["db", "connection"], 7.0)
        pm.add_scar("api timeout", "add circuit breaker", ["api", "timeout"], 5.0)
        results = pm.match_scars("database connection issue", top_k=2)
        assert len(results) >= 1
        assert "db connection lost" in results[0].failure_pattern

    def test_match_scars_no_match(self):
        pm = ProceduralMemory()
        pm.add_scar("x", "y", ["foo"])
        assert pm.match_scars("bar") == []

    def test_match_rules_by_condition(self):
        pm = ProceduralMemory()
        pm.add_rule("deploy on friday", "don't do it", 10.0)
        pm.add_rule("deploy on tuesday", "go ahead", 3.0)
        results = pm.match_rules("we want to deploy on friday evening", top_k=1)
        assert len(results) == 1
        assert "don't do it" in results[0].action

    def test_match_rules_no_match(self):
        pm = ProceduralMemory()
        pm.add_rule("a", "b")
        assert pm.match_rules("zzz") == []

    def test_check_triggers(self):
        pm = ProceduralMemory()
        pm.add_scar("oom", "reduce batch size", ["memory", "oom"], 9.0)
        pm.add_rule("memory leak", "restart pod", 8.0)
        triggers = pm.check_triggers("memory oom leak")
        assert len(triggers["scars"]) >= 1
        assert len(triggers["rules"]) >= 1

    def test_to_entries(self):
        pm = ProceduralMemory()
        pm.add_scar("a", "b")
        pm.add_rule("c", "d")
        entries = pm.to_entries()
        assert len(entries) == 2

    def test_reflect(self):
        pm = ProceduralMemory()
        pm.add_scar("a", "b")
        assert "1 scars" in pm.reflect()
