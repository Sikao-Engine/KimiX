"""Conflict-policy tests for ``todo_list`` (the re-declared-plan incident).

Second session of the reported incident: the plan was written with long titles,
then re-declared with terser wording. ``mode='append'`` created near-duplicate
items because near-match detection was warning-only. The merged tool treats the
same *word set* as the same task and refuses it by default, printing the exact
payload that patches the existing item instead.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.todo import Params, Todo, TodoList

# (existing long spelling, re-declared short spelling) — same words, different
# case / punctuation / numbering / order.
INCIDENT_PAIRS: list[tuple[str, str]] = [
    ("1. Read README lines 55-63", "Read README: lines 55, 63"),
    ("2. Confirm the CLI accepts --wire flag", "Confirm CLI accepts the --wire flag."),
    ("3. Add regression test for wire tools", "Add wire regression test for tools"),
    ("4. Run pytest and record failures", "Record failures and run pytest"),
    ("5. Update docs with the new flags", "Update the docs with new flags"),
    ("6. Fix the flaky approval timeout", "Fix the approval timeout, flaky"),
    ("7. Verify README instructions work", "Verify work README instructions"),
    ("8. Commit the changes to main", "Commit the changes to MAIN"),
]


def _titles(tool: TodoList) -> list[str]:
    def walk(nodes: list[Todo]) -> list[str]:
        out: list[str] = []
        for node in nodes:
            out.append(node.title)
            out.extend(walk(node.children))
        return out

    return walk(tool._load_todos())


async def _seed(tool: TodoList) -> None:
    await tool(
        Params(todos=[Todo(title=title, status="pending") for title, _ in INCIDENT_PAIRS])
    )


class TestIncidentReplay:
    async def test_default_policy_refuses_and_keeps_the_tree_clean(
        self, runtime: Runtime
    ) -> None:
        tool = TodoList(runtime)
        await _seed(tool)
        before = _titles(tool)
        res = await tool(
            Params(
                todos=[
                    Todo(title=short, status="done") for _, short in INCIDENT_PAIRS
                ]
            )
        )
        assert res.is_error
        assert "is a near-duplicate of the existing" in res.output
        assert "on_conflict=" in res.output
        # nothing was written: the whole batch is refused, no duplicates appear
        assert _titles(tool) == before
        assert len(before) == len(INCIDENT_PAIRS)
        assert all(status == "pending" for status in _statuses(tool).values())

    async def test_error_names_the_replacement_payload(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="1. Read README lines 55-63")]))
        res = await tool(Params(todos=[Todo(title="Read README: lines 55, 63", status="done")]))
        assert res.is_error
        assert (
            'send: {"title":"1. Read README lines 55-63","status":"done"}' in res.output
        ), res.output
        assert res.message == (
            'Near-duplicate title "Read README: lines 55, 63" '
            '(existing: "1. Read README lines 55-63").'
        )

    async def test_reuse_patches_the_existing_items(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await _seed(tool)
        res = await tool(
            Params(
                todos=[Todo(title=short, status="done") for _, short in INCIDENT_PAIRS],
                on_conflict="reuse",
            )
        )
        assert not res.is_error
        # the tree is unchanged in size and identity, but every item is now done
        assert len(_titles(tool)) == len(INCIDENT_PAIRS)
        assert _titles(tool) == [long for long, _ in INCIDENT_PAIRS]
        assert all(status == "done" for status in _statuses(tool).values())
        warnings = [
            line for line in res.output.splitlines() if "near-duplicate title" in line
        ]
        assert len(warnings) == len(INCIDENT_PAIRS)

    async def test_append_really_adds_a_second_item(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await _seed(tool)
        res = await tool(
            Params(
                todos=[Todo(title=short) for _, short in INCIDENT_PAIRS],
                on_conflict="append",
            )
        )
        assert not res.is_error, res.output
        assert len(_titles(tool)) == 2 * len(INCIDENT_PAIRS)
        for _, short in INCIDENT_PAIRS:
            assert short in _titles(tool)
        assert "on_conflict='append'" in res.output

    async def test_reuse_synonyms_map_onto_the_policy(self) -> None:
        for spelling in ("reuse", "match", "merge", "patch", "update"):
            assert Params(todos=[], on_conflict=spelling).on_conflict == "reuse"  # type: ignore[arg-type]
        with pytest.raises(ValidationError):
            Params.model_validate({"todos": [], "on_conflict": "sideways"})


class TestConflictBoundary:
    @pytest.mark.parametrize(
        ("first", "second"),
        [
            ("Task 10", "Task 0"),  # digits are words: "10" != "0"
            ("Step 1", "Step 2"),
            ("Fix login bug", "Fix login bugs"),  # typo tier: advisory only
            ("Read the README", "Read the README twice"),  # subset is not equality
        ],
    )
    async def test_different_word_sets_are_not_conflicts(
        self, runtime: Runtime, first: str, second: str
    ) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title=first)]))
        res = await tool(Params(todos=[Todo(title=second)]))
        assert not res.is_error, res.output
        assert second in _titles(tool)

    async def test_near_matches_carry_an_advisory_warning(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="Fix the login bug")]))
        res = await tool(Params(todos=[Todo(title="Fix the login bugs")]))
        assert not res.is_error
        assert 'looks like existing "Fix the login bug"' in res.output

    async def test_reordering_words_is_a_conflict(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="Run pytest and record failures")]))
        res = await tool(Params(todos=[Todo(title="Record failures and run pytest")]))
        assert res.is_error
        assert "near-duplicate" in res.output

    async def test_conflict_is_scoped_to_siblings(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="Parent"), Todo(title="child", parent="Parent")]))
        # the same words at root level are a sibling of "Parent", not of "child"
        res = await tool(Params(todos=[Todo(title="Child")]))
        assert not res.is_error, res.output
        assert [node.title for node in tool._load_todos()] == ["Parent", "Child"]

    async def test_fuzzy_off_skips_the_conflict_scan(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="1. Read README lines 55-63")]))
        res = await tool(Params.model_validate(
            {"todos": [{"title": "Read README: lines 55, 63"}], "fuzzy": False}
        ))
        assert not res.is_error, res.output
        assert "Read README: lines 55, 63" in _titles(tool)

    async def test_exact_title_is_a_patch_not_a_conflict(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="Same wording")]))
        res = await tool(Params(todos=[Todo(title="Same wording", status="done")]))
        assert not res.is_error
        assert _statuses(tool) == {"Same wording": "done"}
        assert len(_titles(tool)) == 1


def _statuses(tool: TodoList) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(nodes: list[Todo]) -> None:
        for node in nodes:
            out[node.title] = node.status
            walk(node.children)

    walk(tool._load_todos())
    return out
