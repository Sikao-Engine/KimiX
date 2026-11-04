"""Tests for the worker-family PID line in the system prompt.

The worker prompts (Worker / Thinker / TrivialSubAgent) tell the model its own
PID and forbid killing that process, so a shell tool call can never take down
the agent running it. These tests pin that contract:

  - the line carries the live ``os.getpid()`` of the process building the
    prompt;
  - it says explicitly never to kill the own process;
  - non-worker roles (Reader / Supervisor / TodoMaker) do not get the line.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kimix.utils.system_prompt import SystemPromptType, get_system_prompt


def _make_runtime(work_dir: Path) -> SimpleNamespace:
    builtin_args = SimpleNamespace(
        KIMI_WORK_DIR=work_dir,
        KIMI_SKILLS="",
        KIMI_OS="Windows",
    )
    session = SimpleNamespace(dir=work_dir, id="test-session", custom_data={})
    return SimpleNamespace(builtin_args=builtin_args, session=session)


def _prompt(work_dir: Path, role: SystemPromptType) -> str:
    prompt_func = get_system_prompt(
        work_dir=work_dir,
        agent_role=role,
        max_system_prompt_tokens=10_000,
    )
    return prompt_func(_make_runtime(work_dir))


_WORKER_ROLES = (
    SystemPromptType.Worker,
    SystemPromptType.Thinker,
    SystemPromptType.TrivialSubAgent,
)

_NON_WORKER_ROLES = (
    SystemPromptType.Reader,
    SystemPromptType.Supervisor,
    SystemPromptType.TodoMaker,
    SystemPromptType.SwarmLeader,
)


class TestWorkerPidLine:
    @pytest.mark.parametrize("role", _WORKER_ROLES)
    def test_states_current_process_id(self, tmp_path: Path, role: SystemPromptType):
        assert f"PID {os.getpid()}" in _prompt(tmp_path, role)

    @pytest.mark.parametrize("role", _WORKER_ROLES)
    def test_forbids_killing_own_process(self, tmp_path: Path, role: SystemPromptType):
        prompt = _prompt(tmp_path, role)
        # Case-insensitive: the prompt renders the clause with emphasis
        # ("NEVER kill your own process"), the contract is the phrase itself.
        assert "never kill your own process" in prompt.lower()

    def test_line_stays_concise(self, tmp_path: Path):
        line = next(
            item
            for item in _prompt(tmp_path, SystemPromptType.Worker).splitlines()
            if "never kill your own process" in item.lower()
        )
        # One short sentence, no wrapping: "- PID 1234: never kill your own process."
        assert line.lower().endswith("never kill your own process.")
        assert len(line) < 80

    def test_no_unsubstituted_placeholder(self, tmp_path: Path):
        assert "{PID}" not in _prompt(tmp_path, SystemPromptType.Worker)

    @pytest.mark.parametrize("role", _NON_WORKER_ROLES)
    def test_non_worker_roles_have_no_pid_line(
        self, tmp_path: Path, role: SystemPromptType
    ):
        prompt = _prompt(tmp_path, role)
        assert "never kill your own process" not in prompt
        assert f"PID {os.getpid()}" not in prompt
