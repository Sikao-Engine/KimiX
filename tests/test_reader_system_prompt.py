"""Tests for the ``SystemPromptType.Reader`` system prompt.

The Reader prompt drives the research tool's read-only child session, whose
toolset is fixed by ``src/kimix/agent_readonly.json`` (read, read_image, glob,
grep, fetch_url, web_search, skill). These tests pin the prompt contract:

  - it names exactly the read/search tools the manifest provides;
  - it demands an evidence-based, self-contained final result with sources;
  - it directly refuses execution, file changes, and implementation tasks;
  - it stays free of AGENTS.md / CLAUDE.md / skills injection (unlike the
    worker-family prompts) — the manifest, not repo docs, defines its tools.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kimix.utils.system_prompt import SystemPromptType, get_system_prompt


def _make_runtime(work_dir: Path, skills: str = "") -> SimpleNamespace:
    builtin_args = SimpleNamespace(
        KIMI_WORK_DIR=work_dir,
        KIMI_SKILLS=skills,
        KIMI_OS="Windows",
    )
    session = SimpleNamespace(dir=work_dir, id="test-session", custom_data={})
    return SimpleNamespace(builtin_args=builtin_args, session=session)


def _reader_prompt(work_dir: Path, skills: str = "") -> str:
    prompt_func = get_system_prompt(
        work_dir=work_dir,
        agent_role=SystemPromptType.Reader,
        max_system_prompt_tokens=10_000,
    )
    return prompt_func(_make_runtime(work_dir, skills))


class TestReaderRoleDoc:
    def test_role_line_declares_read_only_research_subagent(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        assert "Read-only research subagent:" in prompt

    def test_prompt_does_not_use_worker_tool_conventions(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        assert "rtk <tool>" not in prompt


class TestReaderToolGuidance:
    def test_names_every_manifest_tool(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        for tool in ("read", "read_image", "glob", "grep", "fetch_url", "web_search", "skill"):
            assert tool in prompt, f"Reader prompt does not mention `{tool}`"

    def test_read_only_session_directly_refuses_non_research_tasks(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        assert "This session is read-only" in prompt
        assert "directly refuse command execution, file changes, or implementation tasks" in prompt

    def test_research_should_finish_quickly(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        assert "Research quickly" in prompt

    def test_demands_evidence_with_sources(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        assert "evidence-based findings" in prompt
        assert "file paths" in prompt
        assert "URLs" in prompt

    def test_result_must_be_self_contained(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        assert "self-contained final result" in prompt
        assert "the parent sees only your result" in prompt


class TestReaderNoDocInjection:
    def test_agents_md_content_is_not_injected(self, tmp_path: Path):
        (tmp_path / "AGENTS.md").write_text("AGENTS-RULES", encoding="utf-8")
        (tmp_path / "CLAUDE.md").write_text("CLAUDE-RULES", encoding="utf-8")
        prompt = _reader_prompt(tmp_path)
        assert "AGENTS-RULES" not in prompt
        assert "CLAUDE-RULES" not in prompt
        assert "AGENTS.md" not in prompt
        assert "CLAUDE.md" not in prompt

    def test_skills_doc_is_not_injected(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path, skills="some-skill-doc")
        assert "some-skill-doc" not in prompt
        assert "Skills:" not in prompt

    def test_no_unsubstituted_placeholders(self, tmp_path: Path):
        prompt = _reader_prompt(tmp_path)
        for placeholder in ("{TOOL_CONVENTIONS}", "{AGENT_ROLE}", "{NUMBERED}", "{AGENTS_MD}", "{SKILLS}"):
            assert placeholder not in prompt
