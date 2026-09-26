"""Tests for TodoList lightweight single-todo edits."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.display import TodoDisplayBlock
from kimi_cli.tools.todo import (
    Params,
    Todo,
    TodoList,
)


def _find_todo(tool: TodoList, title: str) -> Todo:
    """Return the first todo with ``title`` from persisted state."""
    for t in tool._load_todos():
        if t.title == title:
            return t
    raise AssertionError(f"todo {title!r} not found")


class TestTodoUpdateFuzzyArgumentRepair:
    """LLM-style argument shapes are repaired by per-tool field aliases."""

    async def test_call_task_alias_maps_to_title(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="in_progress")]))

        res = await update.call({"task": "Task A", "status": "done"})
        assert not res.is_error
        assert 'Updated "Task A" (status=done)' in res.output

    async def test_call_todo_alias_maps_to_title(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="pending")]))

        res = await update.call({"todo": "Task A", "status": "done"})
        assert not res.is_error
        assert 'Updated "Task A" (status=done)' in res.output

    async def test_call_edits_alias_maps_to_updates(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="pending")]))

        res = await update.call(
            {"edits": [{"title": "Task A", "status": "done"}]}
        )
        assert not res.is_error
        assert 'Updated "Task A" (status=done)' in res.output

    async def test_call_operations_alias_maps_to_updates(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="pending")]))

        res = await update.call(
            {"operations": [{"title": "Task A", "status": "done"}]}
        )
        assert not res.is_error
        assert 'Updated "Task A" (status=done)' in res.output

    async def test_call_nested_task_alias(self, runtime: Runtime) -> None:
        """Items accept `task` as an alias of `title`."""
        lst = TodoList(runtime)

        res = await lst.call({"todos": [{"task": "New", "status": "done"}]})
        assert not res.is_error
        assert _find_todo(lst, "New") is not None


class TestTodoUpdateBasics:
    async def test_update_status_to_done(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="in_progress")]))

        res = await update(Params(title="Task A", status="done"))
        assert not res.is_error
        assert 'Updated "Task A" (status=done)' in res.output
        assert res.message.startswith('Updated "Task A".')

        todo = _find_todo(update, "Task A")
        assert todo.status == "done"

    async def test_update_status_to_in_progress(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="Task A", status="pending"),
                    Todo(content="Task B", status="done"),
                ]
            )
        )

        res = await update(Params(title="Task A", status="in_progress"))
        assert not res.is_error
        todo = _find_todo(update, "Task A")
        assert todo.status == "in_progress"

    async def test_omitted_status_preserves_existing(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="in_progress")]))

        res = await update(Params(title="Task A", notes="new note"))
        assert not res.is_error
        todo = _find_todo(update, "Task A")
        assert todo.status == "in_progress"
        assert todo.notes == "new note"

    async def test_update_notes_blank_keeps_existing(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="pending", notes="old")]))

        res = await update(Params(title="Task A", notes=""))
        assert not res.is_error
        todo = _find_todo(update, "Task A")
        # Non-destructive default: an omitted or blank `notes` keeps what is
        # stored, because models send "" to mean "nothing to add".
        assert todo.notes == "old"


class TestTodoUpdateRename:
    async def test_rename_root_todo(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Old", status="pending")]))

        res = await update(Params(title="Old", rename_to="New"))
        assert not res.is_error
        assert 'Updated "Old" (renamed to "New")' in res.output
        assert _find_todo(update, "New").status == "pending"

    async def test_rename_collision_errors(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update(Params(title="A", rename_to="B"))
        assert res.is_error
        assert 'Cannot rename "A" to "B"' in res.output

        # Nothing changed.
        assert _find_todo(update, "A").title == "A"


class TestTodoListNearDuplicate:
    async def test_same_words_different_case_is_a_conflict(self, runtime: Runtime) -> None:
        """A re-declared title (same words, new casing) is refused, not duplicated."""
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Implement feature", status="pending")]))

        res = await update(Params(title="implement feature", status="done"))
        assert res.is_error
        assert 'near-duplicate of the existing "Implement feature"' in res.output
        # nothing was created or changed
        todo = _find_todo(update, "Implement feature")
        assert todo.status == "pending"

    async def test_on_conflict_reuse_patches_the_existing_item(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Implement feature", status="pending")]))

        res = await update(Params(title="implement feature", status="done", on_conflict="reuse"))
        assert not res.is_error
        assert _find_todo(update, "Implement feature").status == "done"
        assert len(update._load_todos()) == 1

    async def test_typo_is_warned_and_created(self, runtime: Runtime) -> None:
        """A different *word* is a different task: advisory warning, no refusal."""
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Implement feature", status="pending")]))

        res = await update(Params(title="Implement featuer", status="done"))
        assert not res.is_error
        assert "looks like existing" in res.output or "looks like existing" in res.message
        assert _find_todo(update, "Implement featuer") is not None

    async def test_fuzzy_disabled_skips_the_conflict_check(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="pending")]))

        res = await update(Params(title="task a", status="done", fuzzy=False))
        assert not res.is_error
        assert _find_todo(update, "task a") is not None
        assert _find_todo(update, "Task A").status == "pending"


class TestTodoUpdateRegression:
    async def test_regression_blocked_without_force(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="done")]))

        res = await update(Params(title="Task A", status="in_progress"))
        assert res.is_error
        assert "Cannot regress completed todo" in res.output
        assert _find_todo(update, "Task A").status == "done"

    async def test_regression_allowed_with_force(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="done")]))

        res = await update(
            Params(title="Task A", status="in_progress", force=True)
        )
        assert not res.is_error
        assert _find_todo(update, "Task A").status == "in_progress"


class TestTodoUpdateInProgressConstraint:
    async def test_auto_fixes_multiple_in_progress(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="Task A", status="in_progress"),
                    Todo(content="Task B", status="pending"),
                ]
            )
        )

        res = await update(Params(title="Task B", status="in_progress"))
        assert not res.is_error
        a = _find_todo(update, "Task A")
        b = _find_todo(update, "Task B")
        assert a.status == "done"
        assert b.status == "in_progress"
        assert "Auto-fixed" in res.output


class TestTodoUpdateTreeSearch:
    async def test_updates_nested_todo(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(
                        content="Parent",
                        status="pending",
                        children=[Todo(content="Child", status="pending")],
                    )
                ]
            )
        )

        res = await update(Params(title="Child", status="done"))
        assert not res.is_error
        parent = _find_todo(update, "Parent")
        assert parent.children[0].status == "done"

    async def test_empty_tree_upserts_the_first_item(self, runtime: Runtime) -> None:
        """An empty tree is the normal starting point: merge creates, it does not error."""
        update = TodoList(runtime)
        res = await update(Params(title="Task A", status="done"))
        assert not res.is_error
        assert _find_todo(update, "Task A").status == "done"

    async def test_empty_tree_edit_still_errors(self, runtime: Runtime) -> None:
        """An edit (rename) cannot create, so the empty tree is an error there."""
        update = TodoList(runtime)
        res = await update(Params(title="Task A", rename_to="B"))
        assert res.is_error
        assert "No todos exist" in res.output


class TestTodoUpdateComplete:
    """complete=True marks a todo and all its sub-todos done in one call."""

    async def test_complete_marks_subtree_done(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(
                        content="Parent",
                        status="pending",
                        children=[
                            Todo(content="c1", status="pending"),
                            Todo(
                                content="c2",
                                status="in_progress",
                                children=[Todo(content="g", status="pending")],
                            ),
                        ],
                    )
                ]
            )
        )

        res = await update(Params(title="Parent", complete=True))
        assert not res.is_error
        assert "completed with 4 sub-todos marked done" in res.output
        assert res.message.startswith('Updated "Parent".')

        parent = _find_todo(update, "Parent")
        assert parent.status == "done"
        assert parent.children[0].status == "done"
        assert parent.children[1].status == "done"
        assert parent.children[1].children[0].status == "done"

    async def test_complete_single_item(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="A", status="in_progress")]))

        res = await update(Params(title="A", complete=True))
        assert not res.is_error
        assert "completed with 1 sub-todo marked done" in res.output
        assert _find_todo(update, "A").status == "done"

    async def test_complete_with_status_done_ok(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(
                        content="P",
                        status="pending",
                        children=[Todo(content="c", status="pending")],
                    )
                ]
            )
        )

        res = await update(Params(title="P", status="done", complete=True))
        assert not res.is_error
        parent = _find_todo(update, "P")
        assert parent.status == "done"
        assert parent.children[0].status == "done"

    async def test_complete_with_pending_status_errors(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="A", status="pending")]))

        res = await update(Params(title="A", status="pending", complete=True))
        assert res.is_error
        assert "complete=True cannot be combined with status=\"pending\"" in res.output
        assert _find_todo(update, "A").status == "pending"

    async def test_complete_missing_title_errors(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="P", status="pending")]))

        res = await update(
            Params(parent="P", title="ghost", complete=True)
        )
        assert res.is_error
        assert "complete=True requires an existing todo" in res.output
        assert _find_todo(update, "P").children == []

    async def test_complete_in_batch(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(
                        content="P",
                        status="pending",
                        children=[Todo(content="c", status="in_progress")],
                    ),
                    Todo(content="Q", status="pending"),
                ]
            )
        )

        res = await update(
            Params(
                updates=[
                    {"title": "P", "complete": True},
                    {"title": "Q", "status": "done"},
                ]
            )
        )
        assert not res.is_error
        p = _find_todo(update, "P")
        q = _find_todo(update, "Q")
        assert p.status == "done"
        assert p.children[0].status == "done"
        assert q.status == "done"
        assert "completed with 2 sub-todos marked done" in res.output
        assert 'Updated "Q" (status=done)' in res.output


class TestTodoUpdateDisplay:
    async def test_returns_display_block(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="pending")]))

        res = await update(Params(title="Task A", status="done"))
        assert not res.is_error
        assert len(res.display) == 1
        assert isinstance(res.display[0], TodoDisplayBlock)


class TestTodoUpdateMultiple:
    async def test_update_multiple_statuses(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update(
            Params(
                updates=[
                    {"title": "A", "status": "done"},
                    {"title": "B", "status": "in_progress"},
                ]
            )
        )
        assert not res.is_error
        a = _find_todo(update, "A")
        b = _find_todo(update, "B")
        assert a.status == "done"
        assert b.status == "in_progress"
        assert 'Updated "A" (status=done)' in res.output
        assert 'Updated "B" (status=in_progress)' in res.output
        assert res.message.startswith('Updated "A".; Updated "B".')

    async def test_create_multiple_children_under_common_parent(
        self, runtime: Runtime
    ) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Parent", status="pending")]))

        res = await update(
            Params(
                parent="Parent",
                updates=[
                    {"title": "Child1"},
                    {"title": "Child2", "status": "in_progress"},
                ],
            )
        )
        assert not res.is_error
        parent = _find_todo(update, "Parent")
        assert [c.title for c in parent.children] == ["Child1", "Child2"]
        assert parent.children[0].status == "pending"
        assert parent.children[1].status == "in_progress"
        assert 'Created "Child1" under "Parent".' in res.output
        assert 'Created "Child2" under "Parent".' in res.output

    async def test_updates_alias_todos(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update(
            Params(
                todos=[{"title": "A", "status": "done"}, {"title": "B", "status": "done"}]
            )
        )
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"
        assert _find_todo(update, "B").status == "done"

    async def test_batch_error_leaves_state_unchanged(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="done"),
                ]
            )
        )

        res = await update(
            Params(
                updates=[
                    {"title": "A", "status": "done"},
                    {"title": "B", "status": "in_progress"},
                ]
            )
        )
        assert res.is_error
        assert "Cannot regress completed todo" in res.output
        assert _find_todo(update, "A").status == "pending"
        assert _find_todo(update, "B").status == "done"

    async def test_batch_auto_fixes_multiple_in_progress(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update(
            Params(
                updates=[
                    {"title": "A", "status": "in_progress"},
                    {"title": "B", "status": "in_progress"},
                ]
            )
        )
        assert not res.is_error
        a = _find_todo(update, "A")
        b = _find_todo(update, "B")
        assert a.status == "done"
        assert b.status == "in_progress"
        assert "Auto-fixed" in res.output

    async def test_batch_rename_then_update_child(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(
                        content="Parent",
                        status="pending",
                        children=[Todo(content="Child", status="pending")],
                    )
                ]
            )
        )

        res = await update(
            Params(
                updates=[
                    {"title": "Parent", "rename_to": "NewParent"},
                    {"parent": "NewParent", "title": "Child", "status": "done"},
                ]
            )
        )
        assert not res.is_error
        new_parent = _find_todo(update, "NewParent")
        assert new_parent.children[0].status == "done"

    async def test_batch_creates_root_children_when_empty(self, runtime: Runtime) -> None:
        update = TodoList(runtime)
        res = await update(
            Params(parent="", updates=[{"title": "A"}, {"title": "B"}])
        )
        assert not res.is_error
        todos = update._load_todos()
        assert [t.title for t in todos] == ["A", "B"]

    def test_cannot_mix_top_level_title_with_updates(self) -> None:
        with pytest.raises(ValidationError):
            Params(title="A", updates=[{"title": "B"}])


class TestTodoUpdateBatchStringForms:
    """`updates` accepts JSON-string and bare-title shorthand forms so a whole
    batch of edits still lands in one call when the model mis-serializes it
    (mirroring the `todos` string repair)."""

    async def test_updates_as_json_string_batch(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update.call(
            {
                "updates": '[{"title": "A", "status": "done"}, '
                '{"title": "B", "status": "in_progress"}]'
            }
        )
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"
        assert _find_todo(update, "B").status == "in_progress"
        assert 'Updated "A" (status=done)' in res.output
        assert 'Updated "B" (status=in_progress)' in res.output

    async def test_updates_as_broken_json_string(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update.call(
            {"updates": '[{"title": "A", "status": "done",}, {"title": "B",},]'}
        )
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"
        assert _find_todo(update, "B").status == "pending"

    async def test_updates_as_single_json_dict_string(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="A", status="pending")]))

        res = await update.call({"updates": '{"title": "A", "status": "done"}'})
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"

    async def test_updates_as_bare_title_string(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Fix the bug", status="pending")]))

        res = await update.call({"updates": "Fix the bug"})
        assert not res.is_error
        assert _find_todo(update, "Fix the bug") is not None

    async def test_updates_list_with_bare_string_titles(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update(
            Params(
                updates=[{"title": "A", "status": "done"}, "B"],
            )
        )
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"
        assert _find_todo(update, "B").status == "pending"

    def test_updates_whitespace_string_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Params(updates="   ")

    def test_updates_unsupported_type_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Params(updates=123)


class TestTodoUpdateContentAlias:
    """`content` is accepted as an alias for `title` so items written with
    the retired {content, status, notes} shape can still be reused — single and batch."""

    async def test_single_top_level_content_alias(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="in_progress")]))

        res = await update(
            Params.model_validate({"content": "Task A", "status": "done"})
        )
        assert not res.is_error
        assert 'Updated "Task A" (status=done)' in res.output
        assert _find_todo(update, "Task A").status == "done"

    async def test_single_top_level_content_alias_with_notes(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Task A", status="pending")]))

        res = await update(
            Params.model_validate(
                {"content": "Task A", "notes": "from content-shape"}
            )
        )
        assert not res.is_error
        todo = _find_todo(update, "Task A")
        assert todo.notes == "from content-shape"
        assert todo.status == "pending"

    async def test_updates_accept_content_shape_items(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update(
            Params(
                updates=[
                    {"content": "A", "status": "done"},
                    {"content": "B", "status": "in_progress"},
                ]
            )
        )
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"
        assert _find_todo(update, "B").status == "in_progress"
        assert 'Updated "A" (status=done)' in res.output
        assert 'Updated "B" (status=in_progress)' in res.output

    async def test_mixed_title_and_content_items_in_one_batch(
        self, runtime: Runtime
    ) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(content="A", status="pending"),
                    Todo(content="B", status="pending"),
                ]
            )
        )

        res = await update(
            Params(
                updates=[
                    {"title": "A", "status": "done"},
                    {"content": "B", "status": "done"},
                ]
            )
        )
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"
        assert _find_todo(update, "B").status == "done"
        assert res.message.startswith('Updated "A".; Updated "B".')

    async def test_todos_alias_accepts_content_shape(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="A", status="pending")]))

        res = await update(
            Params.model_validate(
                {"todos": [{"content": "A", "status": "done"}]}
            )
        )
        assert not res.is_error
        assert _find_todo(update, "A").status == "done"

    async def test_content_cannot_mix_with_updates(self, runtime: Runtime) -> None:
        with pytest.raises(ValidationError):
            Params.model_validate(
                {"content": "A", "updates": [{"title": "B"}]}
            )


class TestTodoUpdateParent:
    async def test_creates_child_under_parent(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(Params(todos=[Todo(content="Parent", status="pending")]))

        res = await update(Params(parent="Parent", title="Child"))
        assert not res.is_error
        assert 'Created "Child" under "Parent".' in res.output
        parent = _find_todo(update, "Parent")
        assert [c.title for c in parent.children] == ["Child"]
        assert parent.children[0].status == "pending"

    async def test_updates_existing_child_under_parent(self, runtime: Runtime) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(
                        content="Parent",
                        status="pending",
                        children=[Todo(content="Child", status="pending")],
                    )
                ]
            )
        )

        res = await update(
            Params(parent="Parent", title="Child", status="done")
        )
        assert not res.is_error
        assert 'Updated "Child" (status=done)' in res.output
        parent = _find_todo(update, "Parent")
        assert parent.children[0].status == "done"

    async def test_creates_root_child_with_empty_parent(self, runtime: Runtime) -> None:
        update = TodoList(runtime)
        await TodoList(runtime)(Params(todos=[Todo(content="Existing", status="pending")]))

        res = await update(Params(parent="", title="New Root"))
        assert not res.is_error
        assert 'Created "New Root" under "root".' in res.output
        assert [t.title for t in update._load_todos()] == ["Existing", "New Root"]

    async def test_missing_parent_errors(self, runtime: Runtime) -> None:
        update = TodoList(runtime)
        await TodoList(runtime)(Params(todos=[Todo(content="A", status="pending")]))

        res = await update(Params(parent="Missing", title="Child"))
        assert res.is_error
        assert 'No parent todo matching "Missing" found' in res.output

    async def test_parent_scoped_lookup_does_not_match_outside_parent(
        self, runtime: Runtime
    ) -> None:
        lst = TodoList(runtime)
        update = TodoList(runtime)
        await lst(
            Params(
                todos=[
                    Todo(
                        content="P1",
                        status="pending",
                        children=[Todo(content="Child", status="pending")],
                    ),
                    Todo(
                        content="P2",
                        status="pending",
                        children=[Todo(content="Child", status="pending")],
                    ),
                ]
            )
        )

        res = await update(
            Params(parent="P2", title="Child", status="done")
        )
        assert not res.is_error
        p1 = _find_todo(update, "P1")
        p2 = _find_todo(update, "P2")
        assert p1.children[0].status == "pending"
        assert p2.children[0].status == "done"
