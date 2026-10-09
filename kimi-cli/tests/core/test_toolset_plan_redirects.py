"""Toolset-level guards for the plan-tool rename (WritePlan -> write_plan etc.).

Every plausible spelling an LLM (or a session recorded before the rename) can
produce must resolve to the snake_case plan tool names, mirroring the
todo-merge redirect guards.
"""

from __future__ import annotations

import pytest

from kimi_cli.soul.toolset import _build_platform_redirects  # noqa: PLC2701
from kosong.tooling import normalize_tool_name, resolve_tool_name

PLAN_TARGETS = {"write_plan", "read_plan", "edit_plan"}

# Spelling variants that must redirect to the snake_case plan tools.
PLAN_HALLUCINATIONS: dict[str, str] = {
    # Old CamelCase class names (pre-rename sessions / manifests).
    "WritePlan": "write_plan",
    "ReadPlan": "read_plan",
    "EditPlan": "edit_plan",
    # Inverted word order.
    "PlanWrite": "write_plan",
    "PlanRead": "read_plan",
    "PlanEdit": "edit_plan",
    # File-suffixed variants.
    "WritePlanFile": "write_plan",
    "ReadPlanFile": "read_plan",
    "EditPlanFile": "edit_plan",
    # Synonyms.
    "SavePlan": "write_plan",
    "CreatePlan": "write_plan",
    "NewPlan": "write_plan",
    "StorePlan": "write_plan",
    "OverwritePlan": "write_plan",
    "ViewPlan": "read_plan",
    "OpenPlan": "read_plan",
    "ShowPlan": "read_plan",
    "PlanView": "read_plan",
    "UpdatePlan": "edit_plan",
    "ModifyPlan": "edit_plan",
    "PatchPlan": "edit_plan",
    "RevisePlan": "edit_plan",
    "PlanUpdate": "edit_plan",
    "PlanPatch": "edit_plan",
    "PlanModify": "edit_plan",
    "PlanEditor": "edit_plan",
}


def _redirects() -> dict[str, str]:
    return _build_platform_redirects()


class TestPlanNameRedirection:
    @pytest.mark.parametrize(("hallucinated", "target"), sorted(PLAN_HALLUCINATIONS.items()))
    def test_hallucinated_names_point_at_the_plan_tools(
        self, hallucinated: str, target: str
    ) -> None:
        assert _redirects()[normalize_tool_name(hallucinated)] == target

    @pytest.mark.parametrize(("hallucinated", "target"), sorted(PLAN_HALLUCINATIONS.items()))
    def test_resolution_against_a_real_toolset_name_set(
        self, hallucinated: str, target: str
    ) -> None:
        valid = PLAN_TARGETS | {"read", "write", "edit", "bash"}
        resolution = resolve_tool_name(hallucinated, valid, redirects=_redirects())
        assert resolution.name == target

    @pytest.mark.parametrize("sep_variant", ["write-plan", "WRITE_PLAN", "Write_Plan"])
    def test_separator_variants_fold_to_write_plan(self, sep_variant: str) -> None:
        valid = PLAN_TARGETS
        resolution = resolve_tool_name(sep_variant, valid, redirects=_redirects())
        assert resolution.name == "write_plan"

    def test_canonical_names_are_exact_matches_not_redirects(self) -> None:
        # The snake_case names themselves must not be redirected anywhere.
        redirects = _redirects()
        for target in PLAN_TARGETS:
            assert normalize_tool_name(target) not in redirects or redirects[
                normalize_tool_name(target)
            ] == target
