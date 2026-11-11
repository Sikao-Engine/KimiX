"""Tests for the skill tool (kimi_cli.tools.skill:skill).

The tool refreshes skills from the runtime's roots, resolves names case-
insensitively, and returns the content of the selected SKILL.md file. Tests use
lightweight namespaces with either a static skill index or a roots loader.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import kimi_cli.skill as skill_module
from kaos.path import KaosPath
from kimi_cli.skill import ScopedSkillsRoot, Skill, index_skills, normalize_skill_name
from kimi_cli.tools.skill import _MAX_SKILL_MD_BYTES, Params, SkillTool
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


def _dynamic_tool(skills_dir: Path) -> SkillTool:
    async def load_roots() -> list[ScopedSkillsRoot]:
        return [
            ScopedSkillsRoot(
                root=KaosPath.unsafe_from_local_path(skills_dir),
                scope="project",
            )
        ]

    runtime = SimpleNamespace(skills={}, skill_roots_loader=load_roots)
    return SkillTool(runtime)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_existing_skill_returns_file_content(tmp_path: Path):
    body = "---\nname: alpha\ndescription: a\n---\n# Alpha doc\n"
    md = _make_skill_md(tmp_path, "alpha", body)
    tool = _tool([_skill("alpha", md)])

    result = await tool(Params(name="alpha"))

    assert isinstance(result, ToolOk)
    assert result.is_error is False
    # The skill file path is always included in the output.
    assert str(md) in result.output
    assert result.output.endswith(body)


@pytest.mark.asyncio
async def test_lookup_is_case_insensitive(tmp_path: Path):
    md = _make_skill_md(tmp_path, "Beta", "beta body")
    tool = _tool([_skill("Beta", md)])

    result = await tool(Params(name="bEtA"))

    assert isinstance(result, ToolOk)
    assert str(md) in result.output
    assert result.output.endswith("beta body")


@pytest.mark.asyncio
async def test_empty_name_lists_skills_with_descriptions(tmp_path: Path):
    md_a = _make_skill_md(tmp_path, "alpha", "a body")
    md_b = _make_skill_md(tmp_path, "bravo", "b body")
    tool = _tool([_skill("alpha", md_a, description="Alpha desc"),
                  _skill("bravo", md_b, description="Bravo desc")])

    result = await tool(Params(name=""))

    assert isinstance(result, ToolOk)
    assert result.is_error is False
    # Same rendering as format_skills_for_prompt: scope heading, name, description.
    assert "### Project" in result.output
    assert "- alpha" in result.output
    assert "  - Description: Alpha desc" in result.output
    assert "- bravo" in result.output
    assert "  - Description: Bravo desc" in result.output
    # Sorted by name.
    assert result.output.index("- alpha") < result.output.index("- bravo")
    # Trailing hint line present.
    assert "Use the `skill` tool" in result.output


@pytest.mark.asyncio
async def test_whitespace_only_name_lists_skills(tmp_path: Path):
    md = _make_skill_md(tmp_path, "alpha", "a body")
    tool = _tool([_skill("alpha", md)])

    result = await tool(Params(name="   "))

    assert isinstance(result, ToolOk)
    assert "- alpha" in result.output


@pytest.mark.asyncio
async def test_empty_name_with_no_skills(tmp_path: Path):
    tool = _tool([])

    result = await tool(Params())

    assert isinstance(result, ToolOk)
    assert result.output == "No skills found."


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
async def test_discovers_skill_added_after_tool_creation(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    tool = _dynamic_tool(root)

    missing = await tool(Params(name="added"))
    assert isinstance(missing, ToolError)

    body = "---\nname: added\ndescription: Added later\n---\nAdded body"
    md = _make_skill_md(root, "added", body)
    result = await tool(Params(name="added"))

    assert isinstance(result, ToolOk)
    assert str(md) in result.output
    assert result.output.endswith(body)


@pytest.mark.asyncio
async def test_discovers_project_root_created_after_tool_creation(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    work_dir = tmp_path / "project"
    home.mkdir()
    work_dir.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path / "share"))

    async def load_roots() -> list[ScopedSkillsRoot]:
        return await skill_module.resolve_skills_roots(
            KaosPath.unsafe_from_local_path(work_dir)
        )

    runtime = SimpleNamespace(skills={}, skill_roots_loader=load_roots)
    tool = SkillTool(runtime)  # type: ignore[arg-type]
    missing = await tool(Params(name="added"))
    assert isinstance(missing, ToolError)

    root = work_dir / ".kimi" / "skills"
    root.mkdir(parents=True)
    _make_skill_md(root, "added", "---\nname: added\ndescription: Added\n---\nBody")
    result = await tool(Params(name="added"))

    assert isinstance(result, ToolOk)
    assert result.output.endswith("Body")


@pytest.mark.asyncio
async def test_discovers_skill_md_added_to_existing_directory(tmp_path: Path):
    root = tmp_path / "skills"
    skill_dir = root / "added"
    skill_dir.mkdir(parents=True)
    tool = _dynamic_tool(root)

    initial = await tool(Params())
    assert isinstance(initial, ToolOk)
    assert initial.output == "No skills found."

    md = skill_dir / "SKILL.md"
    md.write_text("---\nname: added\ndescription: Added later\n---\nBody", encoding="utf-8")
    result = await tool(Params(name="added"))

    assert isinstance(result, ToolOk)
    assert result.output.endswith("Body")


@pytest.mark.asyncio
async def test_skill_file_edit_marks_cached_root_dirty(tmp_path: Path):
    root = tmp_path / "skills"
    root.mkdir()
    md = _make_skill_md(root, "before", "---\nname: before\ndescription: Old\n---\nOld")
    tool = _dynamic_tool(root)

    initial = await tool(Params(name="before"))
    assert isinstance(initial, ToolOk)

    updated_body = (
        "---\nname: after\ndescription: A longer replacement description\n---\nUpdated body"
    )
    md.write_text(updated_body, encoding="utf-8")
    result = await tool(Params(name="after"))

    assert isinstance(result, ToolOk)
    assert result.output.endswith(updated_body)


@pytest.mark.asyncio
async def test_unchanged_skill_root_is_not_reparsed(tmp_path: Path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    _make_skill_md(root, "alpha", "---\nname: alpha\ndescription: Alpha\n---\nBody")
    tool = _dynamic_tool(root)
    discover = AsyncMock(wraps=skill_module.discover_skills)
    monkeypatch.setattr(skill_module, "discover_skills", discover)

    first = await tool(Params(name="alpha"))
    second = await tool(Params(name="alpha"))

    assert isinstance(first, ToolOk)
    assert isinstance(second, ToolOk)
    assert discover.await_count == 1


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


@pytest.mark.asyncio
async def test_oversized_skill_file_returns_path_only(tmp_path: Path):
    # A document larger than 65536 bytes is not inlined into the output;
    # the tool only reports the size and the original file path.
    big_body = "x" * (_MAX_SKILL_MD_BYTES + 1)
    md = _make_skill_md(tmp_path, "huge", big_body)
    tool = _tool([_skill("huge", md)])

    result = await tool(Params(name="huge"))

    assert isinstance(result, ToolOk)
    assert result.is_error is False
    assert "too long" in result.output
    assert str(md) in result.output
    # The document content must not be returned.
    assert "x" * 1024 not in result.output


@pytest.mark.asyncio
async def test_skill_file_at_size_limit_returns_content(tmp_path: Path):
    # Exactly at the limit the document is still returned, path included.
    body = "y" * _MAX_SKILL_MD_BYTES
    md = _make_skill_md(tmp_path, "limit", body)
    tool = _tool([_skill("limit", md)])

    result = await tool(Params(name="limit"))

    assert isinstance(result, ToolOk)
    assert str(md) in result.output
    assert result.output.endswith(body)
