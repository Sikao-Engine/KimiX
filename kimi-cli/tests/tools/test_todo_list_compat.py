"""Compatibility tests: calls written for the two retired planning tools.

The merge is a hard cutover (only ``todo_list`` is registered), but every
*payload shape* the old tools accepted must still land, because session
histories and cached prompts keep producing them:

  old spelling                                    new landing
  ----------------------------------------------  ---------------------------------
  ``{"todos": [...]}`` (whole-plan write)          merge/replace write
  ``{"updates": [...]}`` / other batch keys        ``todos``
  ``{"title": ..., "status": ...}`` (single edit)  one merge item
  ``{"content": ...}`` item key                    ``title``
  ``mode='append'``/``'force_overwrite'``          ``merge`` / ``replace``+force
  retired tool names                               ``todo_list`` (redirected)
"""

from __future__ import annotations

import importlib

import pytest
from pydantic import ValidationError

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools import RETIRED_TODO_TOOL_NAMES, resolve_tool_class
from kimi_cli.tools.todo import Params, Todo, TodoList


def _titles(tool: TodoList) -> list[str]:
    def walk(nodes: list[Todo]) -> list[str]:
        out: list[str] = []
        for node in nodes:
            out.append(node.title)
            out.extend(walk(node.children))
        return out

    return walk(tool._load_todos())


class TestRetiredNamesStillResolve:
    def test_the_module_exports_exactly_the_merged_tool(self) -> None:
        module = importlib.import_module("kimi_cli.tools.todo")
        exported = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, TodoList)
            and getattr(value, "name", None) == "todo_list"
        ]
        assert len({item.__name__ for item in exported}) == 1
        assert TodoList.name == "todo_list"

    def test_retire_names_are_two_distinct_strings(self) -> None:
        assert len(RETIRED_TODO_TOOL_NAMES) == 2
        assert all(name != TodoList.name for name in RETIRED_TODO_TOOL_NAMES)

    @pytest.mark.parametrize("entry", ["TodoList", *RETIRED_TODO_TOOL_NAMES, "todo_list"])
    def test_manifest_entries_resolve_to_the_merged_class(self, entry: str) -> None:
        module = importlib.import_module("kimi_cli.tools.todo")
        resolved = resolve_tool_class(module, entry)
        assert resolved is TodoList


class TestLegacyPayloadShapes:
    @pytest.mark.parametrize(
        "batch_key",
        ["todos", "updates", "edits", "items", "changes", "tasks", "operations", "actions", "list"],
    )
    def test_batch_keys_all_land_on_todos(self, batch_key: str) -> None:
        params = Params.model_validate({batch_key: [{"title": "A", "status": "done"}]})
        assert params.todos is not None
        assert [item.title for item in params.todos] == ["A"]

    def test_single_object_batch(self) -> None:
        params = Params.model_validate({"todos": {"title": "A"}})
        assert params.todos is not None and len(params.todos) == 1

    def test_top_level_single_edit_becomes_one_merge_item(self) -> None:
        params = Params.model_validate(
            {"title": "A", "status": "in_progress", "notes": "n", "parent": "P"}
        )
        assert params.todos is not None
        assert len(params.todos) == 1
        item = params.todos[0]
        assert (item.title, item.status, item.notes, item.parent) == (
            "A",
            "in_progress",
            "n",
            "P",
        )
        assert "title" not in params.model_dump()  # consumed into the item

    @pytest.mark.parametrize(
        "spelling", ["force_overwrite", "force_replace", "force", "forceoverride"]
    )
    def test_force_modes_fold_onto_replace_plus_force(self, spelling: str) -> None:
        params = Params.model_validate({"todos": [{"title": "A"}], "mode": spelling})
        assert params.mode == "replace"
        assert params.force is True

    async def test_numbered_restatement_matches_the_same_words(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="Ship it", status="done")]))
        res = await tool(Params(todos=[Todo(title="1. SHIP IT", status="pending")]))
        # same word set, different spelling -> conflict under the default policy,
        # so an old-style re-declaration cannot silently regress a done item.
        assert res.is_error
        assert "near-duplicate" in res.output
        assert tool._load_todos()[0].status == "done"

    async def test_a_whole_plan_write_still_replaces(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="old", status="done")]))
        res = await tool(
            Params.model_validate(
                {"todos": [{"content": "A"}, {"content": "B", "status": "in_progress"}],
                 "mode": "replace", "force": True}
            )
        )
        assert not res.is_error, res.output
        assert _titles(tool) == ["A", "B"]

    async def test_legacy_append_mode_upserts(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="A")]))
        res = await tool(Params.model_validate(
            {"todos": [{"title": "A", "status": "done"}], "mode": "append"}
        ))
        assert not res.is_error, res.output
        assert _titles(tool) == ["A"]

    async def test_legacy_nested_write_keeps_children(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool(Params.model_validate(
            {
                "todos": [
                    {
                        "content": "Parent",
                        "status": "pending",
                        "children": [{"content": "kid", "status": "done"}],
                    }
                ],
                "mode": "replace",
            }
        ))
        assert not res.is_error, res.output
        parent = tool._load_todos()[0]
        assert parent.title == "Parent"
        assert [c.title for c in parent.children] == ["kid"]

    def test_status_alias_and_case_tolerant_values(self) -> None:
        assert Todo.model_validate({"title": "A", "status": "COMPLETED"}).status == "done"
        assert Todo.model_validate({"title": "A", "status": "IN_PROGRESS"}).status == (
            "in_progress"
        )
        assert Todo.model_validate({"title": "A", "status": "completed"}).status == "done"
        # a spaced spelling is refused with the accepted list in the message
        with pytest.raises(ValidationError) as excinfo:
            Todo.model_validate({"title": "A", "status": "in progress"})
        assert "pending, in_progress, done" in str(excinfo.value)
