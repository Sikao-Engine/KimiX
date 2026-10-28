"""Tests for AGENTS.md / CLAUDE.md discovery and injection into the prompt.

The agent loop appends project instruction files found in the work dir to the
system prompt. Historically only ``AGENTS.md`` was picked up; ``CLAUDE.md``
(the Claude Code convention) must be honoured as well, with both files appended
when a repo carries one under each name.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from kimix.utils.system_prompt import (
    SystemPromptType,
    get_system_prompt,
)


def _make_runtime(work_dir: Path) -> SimpleNamespace:
    builtin_args = SimpleNamespace(
        KIMI_WORK_DIR=work_dir,
        KIMI_SKILLS="",
        KIMI_OS="Windows",
    )
    session = SimpleNamespace(dir=work_dir, id="test-session", custom_data={})
    return SimpleNamespace(builtin_args=builtin_args, session=session)


def _prompt(work_dir: Path, role: SystemPromptType = SystemPromptType.Worker) -> str:
    prompt_func = get_system_prompt(
        work_dir=work_dir,
        agent_role=role,
        max_system_prompt_tokens=10_000,
    )
    return prompt_func(_make_runtime(work_dir))


class TestInstructionFileDiscovery:
    def test_agents_md_only(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("AGENTS-RULES", encoding="utf-8")
        prompt = _prompt(tmp_path)
        assert "AGENTS-RULES" in prompt
        assert "AGENTS.md:" in prompt
        assert "CLAUDE.md" not in prompt

    def test_claude_md_only(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text("CLAUDE-RULES", encoding="utf-8")
        prompt = _prompt(tmp_path)
        assert "CLAUDE-RULES" in prompt
        assert "CLAUDE.md:" in prompt
        assert "AGENTS.md" not in prompt

    def test_both_files_appended_in_order(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("AGENTS-RULES", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("CLAUDE-RULES", encoding="utf-8")
        prompt = _prompt(tmp_path)
        assert "AGENTS-RULES" in prompt
        assert "CLAUDE-RULES" in prompt
        # AGENTS.md stays first: existing habits/caches keyed on it.
        assert prompt.index("AGENTS-RULES") < prompt.index("CLAUDE-RULES")

    def test_no_instruction_files(self, tmp_path: Path):
        prompt = _prompt(tmp_path)
        assert "AGENTS.md" not in prompt
        assert "CLAUDE.md" not in prompt

    def test_placeholders_are_fully_substituted(self, tmp_path: Path):
        # Braces inside the file content must not be re-substituted away.
        (tmp_path / "CLAUDE.md").write_text("use {content} and {name}", encoding="utf-8")
        prompt = _prompt(tmp_path)
        assert "use {content} and {name}" in prompt
        assert "{AGENTS_MD}" not in prompt

    def test_content_is_deduplicated_for_symlinked_file(self, tmp_path: Path):
        target = tmp_path / "AGENTS.md"
        target.write_text("SHARED-RULES", encoding="utf-8")
        link = tmp_path / "CLAUDE.md"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available on this platform")
        prompt = _prompt(tmp_path)
        # The same file reached under two names is appended exactly once.
        assert prompt.count("SHARED-RULES") == 1

    def test_oversized_file_is_replaced_by_read_hint(self, tmp_path: Path):
        (tmp_path / "CLAUDE.md").write_text("y" * 5_000, encoding="utf-8")
        prompt = _prompt(tmp_path)
        assert "y" * 5_000 not in prompt
        assert "Read CLAUDE.md before work." in prompt

    def test_oversized_and_small_files_coexist(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("SMALL-RULES", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("y" * 5_000, encoding="utf-8")
        prompt = _prompt(tmp_path)
        assert "SMALL-RULES" in prompt
        assert "y" * 5_000 not in prompt
        assert "Read CLAUDE.md before work." in prompt


class TestInstructionFilesPerRole:
    @pytest.mark.parametrize(
        "role",
        [
            SystemPromptType.Worker,
            SystemPromptType.Thinker,
            SystemPromptType.TodoMaker,
            SystemPromptType.Supervisor,
            SystemPromptType.SwarmLeader,
            SystemPromptType.TrivialSubAgent,
        ],
    )
    def test_roles_that_use_agent_md_also_get_claude_md(self, tmp_path: Path, role: SystemPromptType):
        (tmp_path / "CLAUDE.md").write_text("CLAUDE-RULES", encoding="utf-8")
        prompt = _prompt(tmp_path, role)
        assert "CLAUDE-RULES" in prompt

    def test_reader_role_gets_no_instruction_files(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("AGENTS-RULES", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("CLAUDE-RULES", encoding="utf-8")
        prompt = _prompt(tmp_path, SystemPromptType.Reader)
        assert "AGENTS-RULES" not in prompt
        assert "CLAUDE-RULES" not in prompt


class TestInstructionFileBudget:
    def test_both_files_fold_to_read_hint_when_over_budget(self, tmp_path: Path):
        # Each file is under the per-file cap, so the combined doc is inlined and
        # the overall token budget is what forces the short pointer form.
        (tmp_path / "AGENTS.md").write_text("a" * 3_000, encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("c" * 3_000, encoding="utf-8")
        prompt_func = get_system_prompt(
            work_dir=tmp_path,
            agent_role=SystemPromptType.Worker,
            max_system_prompt_tokens=400,
        )
        prompt = prompt_func(_make_runtime(tmp_path))
        assert "Read AGENTS.md, CLAUDE.md before work." in prompt
