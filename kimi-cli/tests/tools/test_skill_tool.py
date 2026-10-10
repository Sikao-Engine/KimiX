"""Tests for the skill tool (kimi_cli.tools.skill:skill).

The tool resolves a skill by (normalized) name from ``Runtime.skills`` and
returns the content of its SKILL.md file. These tests stub the runtime with a
SimpleNamespace exposing only ``.skills``, which is all ``SkillTool.__init__``
reads.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from kaos.path import KaosPath

from kimi_cli.skill import Skill, index_skills, normalize_skill_name
from kimi_cli.tools.skill import Params, SkillTool
from kosong.tooling import ToolError, ToolOk


def _skill(name: str, md_path: Path, description: str = "desc") -> Skill:
    return Skill(
        name=name,
        description=description,
        type="standard",
        dir=KaosPath.unsafe_from_local_path(md_path.parent),
        skill_md_file=KaosPath.unsafe_from_local_path(md_path),
        scope="project",
    )


def _make_skill_md(tmp_path: Path, name: str, body: str) -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True)
    md = skill_dir / "SKILL.md"
    md.write_text(body, encoding="utf-8")
    return md


def _tool(skills: list[Skill]) -> SkillTool:
    runtime = SimpleNamespace(skills=index_skills(skills))
    return SkillTool(runtime)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_existing_skill_returns_file_content(tmp_path: Path):
    body = "---\nname: alpha\ndescription: a\n---\n# Alpha doc\n"
    md = _make_skill_md(tmp_path, "alpha", body)
    tool = _tool([_skill("alpha", md)])

    result = await tool(Params(name="alpha"))

    assert isinstance(result, ToolOk)
    assert result.is_error is False
    assert result.output == body


@pytest.mark.asyncio
async def test_lookup_is_case_insensitive(tmp_path: Path):
    md = _make_skill_md(tmp_path, "Beta", "beta body")
    tool = _tool([_skill("Beta", md)])

    result = await tool(Params(name="bEtA"))

    assert isinstance(result, ToolOk)
    assert result.output == "beta body"


@pytest.mark.asyncio
async def test_missing_skill_lists_available_names(tmp_path: Path):
    md_a = _make_skill_md(tmp_path, "alpha", "a body")
    md_b = _make_skill_md(tmp_path, "bravo", "b body")
    tool = _tool([_skill("bravo", md_b), _skill("alpha", md_a)])

    result = await tool(Params(name="missing"))

    assert isinstance(result, ToolError)
    assert result.is_error is True
    assert "does not exist" in result.message
    # Available names are listed, sorted.
    assert "alpha" in result.message
    assert "bravo" in result.message
    assert result.message.index("alpha") < result.message.index("bravo")


@pytest.mark.asyncio
async def test_missing_skill_with_no_skills_installed(tmp_path: Path):
    tool = _tool([])

    result = await tool(Params(name="anything"))

    assert isinstance(result, ToolError)
    assert "does not exist" in result.message


@pytest.mark.asyncio
async def test_unreadable_skill_file_returns_error(tmp_path: Path):
    md = _make_skill_md(tmp_path, "gamma", "gamma body")
    skill = _skill("gamma", md)
    # Sanity: the normalized index key resolves even before deletion.
    assert normalize_skill_name("gamma") in index_skills([skill])
    # Delete the file after indexing: lookup succeeds but reading fails.
    md.unlink()
    tool = _tool([skill])

    result = await tool(Params(name="gamma"))

    assert isinstance(result, ToolError)
    assert result.is_error is True
    assert "gamma" in result.message
