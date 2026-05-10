"""Comprehensive tests for system_prompt generation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kimix.utils.system_prompt import get_system_prompt, SystemPromptType


@pytest.fixture
def mock_args() -> MagicMock:
    """Create a mock BuiltinSystemPromptArgs."""
    args = MagicMock()
    args.KIMI_NOW = "2024-01-01 00:00:00"
    args.KIMI_WORK_DIR = Path("/tmp")
    args.KIMI_WORK_DIR_LS = ""
    args.KIMI_AGENTS_MD = ""
    args.KIMI_SKILLS = ""
    args.KIMI_ADDITIONAL_DIRS_INFO = ""
    args.KIMI_OS = "Linux"
    args.KIMI_SHELL = "/bin/bash"
    return args


@pytest.fixture
def windows_args(mock_args: MagicMock) -> MagicMock:
    mock_args.KIMI_OS = "Windows"
    return mock_args


class TestSystemPromptType:
    def test_enum_values(self) -> None:
        assert SystemPromptType.Worker.value == 0
        assert SystemPromptType.TodoMaker.value == 1
        assert SystemPromptType.SwarmCoordinator.value == 3
        assert SystemPromptType.Thinker.value == 2


class TestWorkerPrompt:
    def test_basic_worker(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(yolo=False, agent_role=SystemPromptType.Worker)
        prompt = fn(mock_args)
        assert "You are a terse coder" in prompt
        assert "Use `Run`" in prompt
        assert "Yolo:" not in prompt

    def test_worker_sub_agent(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(is_sub_agent=True, agent_role=SystemPromptType.Worker)
        prompt = fn(mock_args)
        assert "You are a terse sub-agent" in prompt
        assert "Use `Agent`" not in prompt  # sub-agent doesn't get Agent rule

    def test_worker_yolo(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(yolo=True, agent_role=SystemPromptType.Worker)
        prompt = fn(mock_args)
        assert "Yolo:" in prompt

    def test_worker_windows(self, windows_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.Worker)
        prompt = fn(windows_args)
        assert "No Shell, use `Run`" in prompt

    def test_worker_linux_shell(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.Worker)
        prompt = fn(mock_args)
        assert "Shell: /bin/bash" in prompt

    def test_extra_system_prompt(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(
            extra_system_prompt="Extra: be kind.", agent_role=SystemPromptType.Worker
        )
        prompt = fn(mock_args)
        assert "Extra: be kind." in prompt

    def test_skills_included(self, mock_args: MagicMock) -> None:
        mock_args.KIMI_SKILLS = "- skillA"
        fn = get_system_prompt(agent_role=SystemPromptType.Worker)
        prompt = fn(mock_args)
        assert "Skills:" in prompt
        assert "- skillA" in prompt


class TestTodoMakerPrompt:
    def test_todo_maker_role(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.TodoMaker)
        prompt = fn(mock_args)
        assert "You are a planner" in prompt
        assert "Plan only. Do not implement." in prompt


class TestSwarmCoordinatorPrompt:
    def test_swarm_role(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.SwarmCoordinator)
        prompt = fn(mock_args)
        assert "You are a swarm coordinator" in prompt
        assert "AddNode" in prompt
        assert "AddEdge" in prompt


class TestThinkerPrompt:
    def test_thinker_role_doc(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        assert "You are a terse coder" in prompt

    def test_thinker_explicit_cot(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        assert "Think in <thinking>" in prompt
        assert "<thinking>" in prompt
        assert "<quit/>" in prompt

    def test_thinker_no_no_cot(self, mock_args: MagicMock) -> None:
        """The 'No chain-of-thought' rule must be overridden."""
        fn = get_system_prompt(agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        # Should not contain the original anti-CoT directive
        assert "No chain-of-thought. No analysis" not in prompt
        # But other terse rules should remain
        assert "No step-by-step" not in prompt or "No preamble outside tags" in prompt

    def test_thinker_self_verify(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        assert "Self-verify" in prompt
        assert "errors" in prompt
        assert "bad assumptions" in prompt

    def test_thinker_continue(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        assert "think in <thinking>" in prompt.lower()

    def test_thinker_sub_agent(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(is_sub_agent=True, agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        assert "You are a terse sub-agent" in prompt

    def test_thinker_worker_rules_preserved(self, mock_args: MagicMock) -> None:
        fn = get_system_prompt(agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        assert "Use `Run`" in prompt
        assert "SetTodoList" in prompt
        assert "Remember" in prompt
        assert "Recall" in prompt
        assert "Forget" in prompt
        assert "SkillSearch" in prompt

    def test_thinker_memory_and_skills(self, mock_args: MagicMock) -> None:
        mock_args.KIMI_SKILLS = "- test_skill"
        fn = get_system_prompt(agent_role=SystemPromptType.Thinker)
        prompt = fn(mock_args)
        assert "SkillSearch" in prompt
        assert "- test_skill" in prompt


class TestAgentsMdHandling:
    def test_agents_md_not_present(self, mock_args: MagicMock, tmp_path: Path) -> None:
        with patch("kimix.utils.system_prompt.Path.is_file", return_value=False):
            fn = get_system_prompt(work_dir=tmp_path, agent_role=SystemPromptType.Worker)
            prompt = fn(mock_args)
        assert "AGENTS.md" not in prompt

    def test_agents_md_present(self, mock_args: MagicMock, tmp_path: Path) -> None:
        agents_md = tmp_path / "AGENTS.md"
        agents_md.write_text("# Rules\nBe nice.")
        fn = get_system_prompt(work_dir=tmp_path, agent_role=SystemPromptType.Worker)
        prompt = fn(mock_args)
        assert "AGENTS.md:" in prompt
        assert "Be nice" in prompt
