"""Advanced tests for memory tools: AddScar, AddRule, temporal params, deep reflect."""

import pytest

from kimi_agent_sdk import ToolOk
from kimix.memory.tools import (
    Remember,
    Recall,
    GetContext,
    Reflect,
    AddScar,
    AddRule,
    RememberParams,
    RecallParams,
    ReflectParams,
    AddScarParams,
    AddRuleParams,
)


@pytest.mark.asyncio
class TestAddScarTool:
    async def test_add_scar_success(self):
        tool = AddScar()
        result = await tool(AddScarParams(failure_pattern="timeout", lesson="increase delay"))
        assert isinstance(result, ToolOk)
        assert "Scar recorded" in result.output

    async def test_add_scar_with_conditions(self):
        tool = AddScar()
        result = await tool(
            AddScarParams(
                failure_pattern="oom kill",
                lesson="reduce batch size",
                trigger_conditions=["memory", "oom"],
                severity=9.0,
            )
        )
        assert isinstance(result, ToolOk)


@pytest.mark.asyncio
class TestAddRuleTool:
    async def test_add_rule_success(self):
        tool = AddRule()
        result = await tool(
            AddRuleParams(condition="deploy on friday", action="reject", priority=10.0)
        )
        assert isinstance(result, ToolOk)
        assert "Rule recorded" in result.output

    async def test_add_rule_with_tags(self):
        tool = AddRule()
        result = await tool(
            AddRuleParams(condition="high cpu", action="scale out", tags=["ops"])
        )
        assert isinstance(result, ToolOk)


@pytest.mark.asyncio
class TestRememberTemporalParams:
    async def test_remember_with_expires_at(self):
        import time
        tool = Remember()
        result = await tool(
            RememberParams(
                content="temporary fact",
                importance=5.0,
                long_term=True,
                expires_at=time.time() + 3600,
            )
        )
        assert isinstance(result, ToolOk)


@pytest.mark.asyncio
class TestRecallProceduralFlag:
    async def test_recall_without_procedural(self):
        tool = Recall()
        result = await tool(RecallParams(query="test"))
        assert isinstance(result, ToolOk)
        # Should not contain procedural section when default False
        assert "PROCEDURAL" not in result.output

    async def test_recall_with_procedural(self):
        tool = Recall()
        # First add a scar so procedural has something
        scar_tool = AddScar()
        await scar_tool(AddScarParams(failure_pattern="fail", lesson="avoid", trigger_conditions=["fail"]))
        result = await tool(RecallParams(query="fail", use_procedural=True))
        assert isinstance(result, ToolOk)
        assert "PROCEDURAL" in result.output


@pytest.mark.asyncio
class TestReflectDeep:
    async def test_reflect_default(self):
        tool = Reflect()
        result = await tool(ReflectParams())
        assert isinstance(result, ToolOk)
        assert "Memory System Status Report" in result.output

    async def test_reflect_deep(self):
        tool = Reflect()
        result = await tool(ReflectParams(deep=True))
        assert isinstance(result, ToolOk)
        assert "Self-Reflection Report" in result.output
