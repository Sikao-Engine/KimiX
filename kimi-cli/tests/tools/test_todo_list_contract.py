"""Contract tests for the merged single todo tool (``todo_list``).

One tool, one item shape, dispatch by ``mode``/keys. These tests pin the
contract that the two retired planning tools used to split between them:

1. dispatch matrix (read / merge / replace / clear, plus retired mode spellings);
2. the item shape: canonical ``title`` with its accepted synonyms, ``extra="forbid"``
   diagnostics, and the edit keys (``parent``/``rename_to``/``complete``);
3. the batch-key and payload-shape repairs that keep old calls landing;
4. precedence and budgets (``parent`` over ``scope``, ``_MAX_TODOS``, one
   ``in_progress`` at a time, schema+description token budget).
"""

from __future__ import annotations

import orjson
import pytest
from pydantic import ValidationError

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.todo import (
    _MAX_TODOS,  # noqa: PLC2701
    _TODOLIST_DESCRIPTION,  # noqa: PLC2701
    Params,
    Todo,
    TodoList,
)
from kosong.tooling import _parameters_schema  # noqa: PLC2701  (schema budget)


def _titles(tool: TodoList) -> list[str]:
    def walk(nodes: list[Todo]) -> list[str]:
        out: list[str] = []
        for node in nodes:
            out.append(node.title)
            out.extend(walk(node.children))
        return out

    return walk(tool._load_todos())


def _statuses(tool: TodoList) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(nodes: list[Todo]) -> None:
        for node in nodes:
            out[node.title] = node.status
            walk(node.children)

    walk(tool._load_todos())
    return out


class TestDispatchMatrix:
    async def test_no_todos_reads_the_tree(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        empty = await tool(Params())
        assert not empty.is_error
        assert "Todo list is empty." in empty.output
        await tool(Params(todos=[Todo(title="A")], mode="replace"))
        res = await tool(Params())
        assert "Current todo list" in res.output or "- [pending] A" in res.output
        assert res.message == "Current todo list displayed."

    async def test_merge_patches_an_existing_title_in_place(
        self, runtime: Runtime
    ) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="A", status="pending"), Todo(title="B")]))
        res = await tool(Params(todos=[Todo(title="A", status="done")]))
        assert not res.is_error
        assert _statuses(tool) == {"A": "done", "B": "pending"}
        assert 'Updated "A" (status=done)' in res.output

    async def test_merge_creates_an_unknown_title(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="A")]))
        res = await tool(Params(todos=[Todo(title="B", status="in_progress")]))
        assert not res.is_error
        assert _titles(tool) == ["A", "B"]
        assert _statuses(tool)["B"] == "in_progress"
        # A plain root create stays terse; the per-item detail lines are for
        # scoped/patched calls (see TestEditKeys).
        assert 'Created "B"' not in res.output

    async def test_replace_writes_the_whole_tree(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="old", status="done")]))
        res = await tool(Params(todos=[Todo(title="new")], mode="replace"))
        assert not res.is_error
        assert _titles(tool) == ["new"]
        assert "replaced" in res.output.splitlines()[0]

    async def test_clear_empties_the_list(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="A", status="done")]))
        res = await tool(Params(mode="clear"))
        assert not res.is_error
        assert _titles(tool) == []

    @pytest.mark.parametrize(
        "spelling", ["merge", "append", "add", "patch", "update", "upsert", "edit", "edits"]
    )
    async def test_merge_mode_synonyms_upsert(self, runtime: Runtime, spelling: str) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="A")], mode="merge"))  # type: ignore[arg-type]
        res = await tool(Params(todos=[Todo(title="A", status="done")], mode=spelling))  # type: ignore[arg-type]
        assert not res.is_error, res.output
        assert _statuses(tool)["A"] == "done"

    @pytest.mark.parametrize("spelling", ["replace", "overwrite", "override", "set", "write"])
    async def test_replace_mode_synonyms(
        self, runtime: Runtime, spelling: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="keep", status="done")]))
        res = await tool(Params(todos=[Todo(title="fresh")], mode=spelling))  # type: ignore[arg-type]
        assert not res.is_error, res.output
        assert _titles(tool) == ["fresh"]

    @pytest.mark.parametrize("spelling", ["clear", "delete", "reset", "empty", "remove"])
    async def test_clear_mode_synonyms(self, runtime: Runtime, spelling: str) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="A", status="done")]))
        res = await tool(Params(mode=spelling))  # type: ignore[arg-type]
        assert not res.is_error, res.output
        assert _titles(tool) == []

    async def test_clear_with_todos_is_refused(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool(Params(todos=[Todo(title="A")], mode="clear"))
        assert res.is_error
        assert "mode='clear' cannot be combined with todos" in res.output

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Params.model_validate({"todos": [], "mode": "sideways"})
        assert "sideways" in str(excinfo.value)


class TestItemShape:
    @pytest.mark.parametrize("key", ["title", "content", "task", "todo", "item", "name"])
    async def test_title_synonyms_all_validate(self, key: str) -> None:
        item = Todo.model_validate({key: "Only a synonym", "status": "pending"})
        assert item.title == "Only a synonym"

    async def test_title_is_the_canonical_required_key(self) -> None:
        schema = _parameters_schema(Params)
        item = schema["properties"]["todos"]["items"]
        assert "title" in item["properties"]
        assert "title" in item["required"]
        assert "status" not in item["required"]
        assert "notes" not in item["required"]

    async def test_missing_title_names_the_field_and_lists_spellings(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Params.model_validate({"todos": [{"status": "done"}]})
        text = str(excinfo.value)
        assert "title" in text
        assert "content" in text and "task" in text

    async def test_unknown_item_key_is_named(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Params.model_validate({"todos": [{"title": "A", "priority": 1}]})
        assert "unexpected field 'priority'" in str(excinfo.value)

    async def test_bad_index_is_reported_with_a_count(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            Params.model_validate(
                {"todos": [{"title": "ok"}, {"status": "done"}, {"priority": 2}]}
            )
        text = str(excinfo.value)
        assert "2 of 3" in text
        assert "todos[1]" in text and "todos[2]" in text
        assert "unexpected field 'priority'" in text

    async def test_bare_string_item_becomes_a_title(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool(Params(todos=["Just a title"]))  # type: ignore[list-item]
        assert not res.is_error
        assert _titles(tool) == ["Just a title"]
        assert _statuses(tool)["Just a title"] == "pending"

    async def test_todos_accepts_a_json_string_batch(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        payload = orjson.dumps([{"title": "A", "status": "done"}]).decode("utf-8")
        res = await tool(Params(todos=payload))  # type: ignore[arg-type]
        assert not res.is_error
        assert _titles(tool) == ["A"]

    async def test_description_is_carried_as_notes(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params.model_validate({"todos": [{"title": "A", "description": "detail"}]}))
        assert tool._load_todos()[0].notes == "detail"

    async def test_status_alias_completed_normalizes_to_done(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params.model_validate({"todos": [{"title": "A", "status": "completed"}]}))
        assert _statuses(tool)["A"] == "done"

    async def test_blank_notes_keeps_stored_notes(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="A", notes="kept")]))
        res = await tool(Params(todos=[Todo(title="A", status="done", notes="")]))
        assert not res.is_error
        assert tool._load_todos()[0].notes == "kept"


class TestEditKeys:
    async def test_item_parent_creates_a_child(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="Parent")]))
        res = await tool(Params(todos=[Todo(title="kid", parent="Parent")]))
        assert not res.is_error
        assert 'Created "kid" under "Parent"' in res.output
        assert [c.title for c in tool._load_todos()[0].children] == ["kid"]

    async def test_top_level_scope_is_the_default_parent(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="Parent")]))
        res = await tool(Params.model_validate(
            {"todos": [{"title": "kid"}], "scope": "Parent"}
        ))
        assert not res.is_error, res.output
        assert [c.title for c in tool._load_todos()[0].children] == ["kid"]

    async def test_item_parent_wins_over_scope(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="P1"), Todo(title="P2")]))
        await tool(Params.model_validate(
            {"todos": [{"title": "kid", "parent": "P2"}], "scope": "P1"}
        ))
        assert [c.title for c in tool._load_todos()[0].children] == []
        assert [c.title for c in tool._load_todos()[1].children] == ["kid"]

    async def test_unknown_parent_is_named(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool(Params(todos=[Todo(title="kid", parent="Nope")]))
        assert res.is_error
        assert 'No parent todo matching "Nope" found.' in res.output

    async def test_rename_to_moves_the_title_and_keeps_notes(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="old", notes="n", status="in_progress")]))
        res = await tool(Params(todos=[Todo(title="old", rename_to="new")]))
        assert not res.is_error
        assert _titles(tool) == ["new"]
        assert tool._load_todos()[0].notes == "n"
        assert _statuses(tool)["new"] == "in_progress"

    async def test_complete_finishes_a_subtree(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="P"), Todo(title="c1", parent="P")]))
        res = await tool(Params(todos=[Todo(title="P", complete=True)]))
        assert not res.is_error
        assert _statuses(tool) == {"P": "done", "c1": "done"}

    @pytest.mark.parametrize("edit_key", ["rename_to", "complete"])
    async def test_edit_keys_reject_children(
        self, runtime: Runtime, edit_key: str
    ) -> None:
        tool = TodoList(runtime)
        await tool(Params(todos=[Todo(title="P")]))
        payload = {"title": "P", edit_key: "x" if edit_key == "rename_to" else True,
                   "children": [{"title": "c"}]}
        res = await tool(Params.model_validate({"todos": [payload]}))
        assert res.is_error
        assert "combines rename_to/complete with children" in res.output


class TestPrecedenceAndBudgets:
    async def test_max_todos_guard(self, runtime: Runtime, monkeypatch: pytest.MonkeyPatch) -> None:
        import kimi_cli.tools.todo as todo_module

        monkeypatch.setattr(todo_module, "_MAX_TODOS", 3)
        tool = TodoList(runtime)
        res = await tool(
            Params(todos=[Todo(title=f"T{i}") for i in range(4)]),
        )
        assert res.is_error
        assert "exceeds maximum limit of 3 items" in res.output
        assert _MAX_TODOS > 3  # the module default is far larger

    async def test_duplicate_titles_in_one_batch_are_refused(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool(Params(todos=[Todo(title="Same"), Todo(title="Same")]))
        assert res.is_error
        assert "Duplicate todo titles" in res.output

    async def test_two_in_progress_items_are_reconciled_by_auto_fix(
        self, runtime: Runtime
    ) -> None:
        tool = TodoList(runtime)
        res = await tool(
            Params(todos=[Todo(title="A", status="in_progress"),
                          Todo(title="B", status="in_progress")])
        )
        assert not res.is_error
        assert _statuses(tool) in ({"A": "done", "B": "in_progress"},
                                   {"A": "in_progress", "B": "pending"}) or (
            list(_statuses(tool).values()).count("in_progress") == 1
        )

    async def test_in_progress_conflict_without_auto_fix(self, runtime: Runtime) -> None:
        tool = TodoList(runtime)
        res = await tool(
            Params(
                todos=[Todo(title="A", status="in_progress"),
                       Todo(title="B", status="in_progress")],
                auto_fix=False,
            )
        )
        assert res.is_error
        assert "Multiple items are in_progress" in res.output

    def test_single_tool_fits_the_token_budget(self) -> None:
        schema = orjson.dumps(_parameters_schema(Params)).decode("utf-8")
        total = len(schema) + len(_TODOLIST_DESCRIPTION)
        assert total < int(9_563 * 0.75), total

    def test_description_documents_the_contract(self) -> None:
        for cue in ("title", "mode='merge'", "on_conflict", "parent", "todos"):
            assert cue in _TODOLIST_DESCRIPTION, cue
