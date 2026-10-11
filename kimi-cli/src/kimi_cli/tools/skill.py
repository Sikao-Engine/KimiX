"""Skill tool: read a skill's full SKILL.md document by name.

The system prompt lists available skills as name + description only (no file
paths). This tool lets the model fetch the full skill document on demand:
look the requested name up in ``Runtime.skills`` (keyed by normalized name)
and return the raw markdown of the skill's ``SKILL.md`` file.

When the name is empty, the tool returns the list of skills available at the
current work directory (name + description, grouped by scope), rendered the
same way :func:`kimi_cli.skill.format_skills_for_prompt` does for the system
prompt.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from kimi_cli.soul.agent import Runtime
from kimi_cli.skill import Skill, format_skills_for_prompt, normalize_skill_name
from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue


class Params(BaseModel):
    """Parameters for the skill tool."""

    name: str = Field(
        default="",
        description=(
            "Skill name. Leave empty to list all available skills "
            "(name + description) in the current work directory."
        ),
    )


class SkillTool(CallableTool2[Params]):
    name: str = "skill"
    description: str = (
        "Load a skill's SKILL.md document by name. "
        "The system prompt lists the names and descriptions of available skills; "
        "call this tool to read the full skill document before using a skill. "
        "Pass an empty name to list all available skills (name + description) "
        "in the current work directory."
    )
    params: type[Params] = Params

    def __init__(self, runtime: Runtime) -> None:
        super().__init__()
        self._skills: dict[str, Skill] = runtime.skills

    async def __call__(self, params: Params) -> ToolReturnValue:
        if not params.name.strip():
            return ToolOk(
                output=format_skills_for_prompt(self._skills.values()),
                brief="Listed available skills",
            )
        skill = self._skills.get(normalize_skill_name(params.name))
        if skill is None:
            available = ", ".join(sorted(s.name for s in self._skills.values())) or "(none)"
            return ToolError(
                output="",
                message=(
                    f'Skill "{params.name}" does not exist. '
                    f"Available skills: {available}"
                ),
                brief="Skill not found",
            )
        try:
            content = await skill.skill_md_file.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            return ToolError(
                output="",
                message=(
                    f'Failed to read the SKILL.md document of skill "{skill.name}": {exc}'
                ),
                brief="Skill document unreadable",
            )
        return ToolOk(
            output=content,
            brief=f"Loaded skill: {skill.name}",
        )
