"""Tests for Defects 12.1-12.3: AgentSwarm improvements."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimix.tools.swarm import AgentSwarmParams


class TestAgentSwarmParams:
    def test_prompt_prefix_suffix_accepted(self) -> None:
        params = AgentSwarmParams(
            description="Test",
            prompt_prefix="Fix errors in ",
            prompt_suffix=" and report.",
            items=["file_a.py", "file_b.py"],
        )
        assert params.prompt_prefix == "Fix errors in "

    def test_template_and_prefix_mutually_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="not both"):
            AgentSwarmParams(
                description="Test",
                prompt_template="Fix {{item}}",
                prompt_prefix="Fix ",
                items=["a", "b"],
            )

    def test_neither_template_nor_prefix_rejected(self) -> None:
        with pytest.raises(ValidationError):
            AgentSwarmParams(
                description="Test",
                prompt_template="Missing placeholder",
                items=["a", "b"],
            )

    def test_custom_subagent_type_accepted(self) -> None:
        params = AgentSwarmParams(
            description="Test",
            prompt_template="Do {{item}}",
            items=["a", "b"],
            subagent_type="custom_agent",
        )
        assert params.subagent_type == "custom_agent"
