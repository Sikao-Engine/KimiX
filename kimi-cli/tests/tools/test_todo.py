"""Tests for TodoList tool."""

from __future__ import annotations

from pathlib import Path

import pytest
from kosong.tooling import ToolReturnValue

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.todo import Params, Todo, TodoList


@pytest.fixture
def todo_list_tool(runtime: Runtime) -> TodoList:
    """Create a TodoList tool instance with runtime."""
    return TodoList(runtime)


class TestTodoListOutputNotEmpty:
    """Regression test for issue #1710: TodoList storm.

    The root cause is that TodoList returned output="" which meant the model
    only saw '<system>Todo list updated</system>' — no confirmation of what it
    saved. This led to repeated calls (a "storm") especially when Shell was disabled.
    """

    async def test_write_mode_returns_nonempty_output(self, todo_list_tool: TodoList):
        """When todos are provided, the tool must return a non-empty output
        so the model gets meaningful feedback (not just 'Todo list updated')."""
        params = Params(
            todos=[
                Todo(title="Analyze code", status="pending"),
                Todo(title="Write tests", status="in_progress"),
                Todo(title="Read requirements", status="done"),
            ]
        )
        result = await todo_list_tool(params)
        assert not result.is_error
        # The critical assertion: output must NOT be empty
        assert result.output != "", (
            "TodoList output must not be empty — this is the root cause of issue #1710. "
            "The model needs to see confirmation of the todo state it just set."
        )
        assert result.message == "Todo list updated."

    async def test_read_mode_returns_current_todos(self, todo_list_tool: TodoList):
        """When no todos are provided (None), the tool should return the current
        todo list from persistent storage, including status."""
        # First write some todos
        write_params = Params(
            todos=[
                Todo(title="Task A", status="pending"),
                Todo(title="Task B", status="done"),
            ]
        )
        await todo_list_tool(write_params)

        # Then read without providing todos
        read_params = Params(todos=None)
        result = await todo_list_tool(read_params)
        assert not result.is_error
        assert "Task A" in result.output
        assert "Task B" in result.output
        assert "pending" in result.output
        assert "done" in result.output

    async def test_read_mode_empty_list(self, todo_list_tool: TodoList):
        """Reading with no prior todos should return a clear empty message."""
        read_params = Params(todos=None)
        result = await todo_list_tool(read_params)
        assert not result.is_error
        assert result.output  # non-empty even when no todos

    async def test_write_empty_list_clears_todos_when_force_replace(
        self, todo_list_tool: TodoList
    ):
        """Passing an empty list [] with force_replace=True clears all todos."""
        # Write some todos first
        write_params = Params(todos=[Todo(title="Task A", status="pending")])
        await todo_list_tool(write_params)

        # Clear with empty list + force_replace
        clear_params = Params(todos=[], force_replace=True)
        result = await todo_list_tool(clear_params)
        assert not result.is_error
        assert result.output == "Todo list updated"
        assert "force_replace=True bypasses all validation logic" in result.message

        # Verify cleared
        read_params = Params(todos=None)
        result = await todo_list_tool(read_params)
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower() or result.output.strip() == "Todo list is empty."

    async def test_write_empty_list_without_force_replace_errors(
        self, todo_list_tool: TodoList
    ):
        """Passing an empty list [] without force_replace when old todos are
        not all done should return an error."""
        write_params = Params(todos=[Todo(title="Task A", status="pending")])
        await todo_list_tool(write_params)

        clear_params = Params(todos=[])
        result = await todo_list_tool(clear_params)
        assert result.is_error
        assert "Cannot clear todos" in result.output

    async def test_write_empty_list_when_all_done_clears(
        self, todo_list_tool: TodoList
    ):
        """Passing an empty list [] when all old todos are done should clear."""
        write_params = Params(todos=[Todo(title="Task A", status="done")])
        await todo_list_tool(write_params)

        clear_params = Params(todos=[])
        result = await todo_list_tool(clear_params)
        assert not result.is_error
        assert result.output == "Todo list updated"

        read_params = Params(todos=None)
        result = await todo_list_tool(read_params)
        assert "empty" in result.output.lower()

    async def test_root_todos_persisted_to_disk(
        self, todo_list_tool: TodoList, runtime: Runtime
    ):
        """Write mode should persist todos to disk via SessionState."""
        from kimi_cli.session_state import load_session_state

        params = Params(
            todos=[
                Todo(title="Disk task", status="in_progress"),
                Todo(title="Another task", status="done"),
            ]
        )
        await todo_list_tool(params)

        # Verify by loading directly from disk, bypassing in-memory state
        disk_state = load_session_state(runtime.session.dir)
        assert len(disk_state.todos) == 2
        assert disk_state.todos[0].title == "Disk task"
        assert disk_state.todos[0].status == "in_progress"
        assert disk_state.todos[1].title == "Another task"
        assert disk_state.todos[1].status == "done"

    async def test_write_mode_display_block(self, todo_list_tool: TodoList):
        """Write mode should still produce TodoDisplayBlock for UI rendering."""
        from kimi_cli.tools.display import TodoDisplayBlock

        params = Params(todos=[Todo(title="UI task", status="pending")])
        result = await todo_list_tool(params)
        assert len(result.display) == 1
        assert isinstance(result.display[0], TodoDisplayBlock)
        assert result.display[0].items[0].title == "UI task"

    async def test_read_mode_no_display_block(self, todo_list_tool: TodoList):
        """Read mode should not produce display blocks (no UI side-effect)."""
        read_params = Params(todos=None)
        result = await todo_list_tool(read_params)
        assert result.display == []


class TestTodoListActiveSummary:
    """Tests for the active-todo summary appended to successful writes."""

    async def test_write_outputs_pending_and_in_progress_summary(
        self, todo_list_tool: TodoList
    ):
        """Successful writes list pending and in_progress todos in output."""
        params = Params(
            todos=[
                Todo(title="Pending task", status="pending"),
                Todo(title="In progress task", status="in_progress"),
                Todo(title="Done task", status="done"),
            ]
        )
        result = await todo_list_tool(params)
        assert not result.is_error
        assert "- [pending] Pending task" in result.output
        assert "- [in progress] In progress task" in result.output
        assert "Done task" not in result.output

    async def test_write_summary_omits_done_items(self, todo_list_tool: TodoList):
        """When all todos are done, no active summary is emitted."""
        params = Params(todos=[Todo(title="Only done", status="done")])
        result = await todo_list_tool(params)
        assert not result.is_error
        assert result.output == "Todo list updated"

    async def test_write_summary_preserved_when_all_done(self, todo_list_tool: TodoList):
        """Marking all active todos as done removes the active summary."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="Active A", status="pending"),
                    Todo(title="Active B", status="in_progress"),
                ]
            )
        )

        result = await todo_list_tool(
            Params(
                todos=[
                    Todo(title="Active A", status="done"),
                    Todo(title="Active B", status="done"),
                ]
            )
        )
        assert not result.is_error
        assert result.output == "Todo list updated"

    async def test_write_summary_with_force_replace_warning(
        self, todo_list_tool: TodoList
    ):
        """Warning from force_replace is in message; active summary is in output."""
        params = Params(
            todos=[
                Todo(title="Forced pending", status="pending"),
                Todo(title="Forced in progress", status="in_progress"),
            ],
            force_replace=True,
        )
        result = await todo_list_tool(params)
        assert not result.is_error
        assert result.output.startswith("Todo list updated")
        assert "- [pending] Forced pending" in result.output
        assert "- [in progress] Forced in progress" in result.output
        assert "force_replace" not in result.output
        assert "force_replace=True bypasses all validation logic" in result.message

    async def test_write_summary_order_matches_persisted_order(
        self, todo_list_tool: TodoList
    ):
        """Active todos are listed in the exact order they appear after the write."""
        params = Params(
            todos=[
                Todo(title="First", status="pending"),
                Todo(title="Second", status="in_progress"),
                Todo(title="Third", status="pending"),
                Todo(title="Fourth", status="done"),
                Todo(title="Fifth", status="in_progress"),
            ]
        )
        result = await todo_list_tool(params)
        assert not result.is_error

        active_lines = [
            line for line in result.output.splitlines() if line.startswith("- [")
        ]
        assert active_lines == [
            "- [pending] First",
            "- [in progress] Second",
            "- [pending] Third",
            "- [in progress] Fifth",
        ]


class TestTodoListIncrementalUpdate:
    """Test incremental update behavior when new todos are a subset of old."""

    async def test_incremental_update_status(self, todo_list_tool: TodoList):
        """Updating a subset of todos should only change their statuses."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="A", status="pending"),
                    Todo(title="B", status="in_progress"),
                    Todo(title="C", status="pending"),
                ]
            )
        )

        # Update only B and C
        result = await todo_list_tool(
            Params(todos=[Todo(title="B", status="done"), Todo(title="C", status="in_progress")])
        )
        assert not result.is_error

        # Read back and verify
        read_result = await todo_list_tool(Params(todos=None))
        assert "[pending] A" in read_result.output
        assert "[done] B" in read_result.output
        assert "[in_progress] C" in read_result.output

    async def test_incremental_update_preserves_order(self, todo_list_tool: TodoList):
        """Incremental update should preserve the original order of todos."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="First", status="pending"),
                    Todo(title="Second", status="pending"),
                    Todo(title="Third", status="pending"),
                ]
            )
        )

        # Update in reverse order
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="Third", status="done"),
                    Todo(title="First", status="done"),
                ]
            )
        )

        read_result = await todo_list_tool(Params(todos=None))
        lines = read_result.output.splitlines()
        assert lines[1] == "- [done] First"
        assert lines[2] == "- [pending] Second"
        assert lines[3].startswith("- [done] Third")

    async def test_single_todo_update(self, todo_list_tool: TodoList):
        """Passing a single Todo instance should update just that item."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="A", status="pending"),
                    Todo(title="B", status="pending"),
                ]
            )
        )

        # Pass single Todo, not a list
        result = await todo_list_tool(Params(todos=Todo(title="B", status="done")))
        assert not result.is_error

        read_result = await todo_list_tool(Params(todos=None))
        assert "[pending] A" in read_result.output
        assert "[done] B" in read_result.output


class TestTodoListValidation:
    """Test validation rules for new todos."""

    async def test_duplicate_titles_rejected(self, todo_list_tool: TodoList):
        """Duplicate titles in new todos should return an error."""
        params = Params(
            todos=[
                Todo(title="Task A", status="pending"),
                Todo(title="Task B", status="in_progress"),
                Todo(title="Task A", status="done"),
            ]
        )
        result = await todo_list_tool(params)
        assert result.is_error
        assert "Duplicate todo titles found" in result.output

    async def test_title_too_long_rejected(self, todo_list_tool: TodoList):
        """Titles longer than 65536 characters should be rejected at model level."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Todo(title="x" * 65537, status="pending")

    async def test_whitespace_only_title_rejected(self, todo_list_tool: TodoList):
        """Titles that are only whitespace should be rejected at model level."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Todo(title="   ", status="pending")

    async def test_todo_count_limit(self, todo_list_tool: TodoList):
        """More than 4096 todos should return an error."""
        todos = [Todo(title=f"Task {i}", status="pending") for i in range(4097)]
        params = Params(todos=todos)
        result = await todo_list_tool(params)
        assert result.is_error
        assert "exceeds maximum limit of 4096" in result.output

    async def test_force_replace_outputs_warning(self, todo_list_tool: TodoList):
        """force_replace=True should include a warning in the message."""
        params = Params(todos=[Todo(title="Task", status="pending")], force_replace=True)
        result = await todo_list_tool(params)
        assert not result.is_error
        assert "force_replace=True bypasses all validation logic" in result.message

    async def test_status_regression_blocked(self, todo_list_tool: TodoList):
        """Changing a done todo back to pending/in_progress should be blocked."""
        await todo_list_tool(
            Params(todos=[Todo(title="A", status="pending"), Todo(title="B", status="done")])
        )

        result = await todo_list_tool(
            Params(todos=[Todo(title="A", status="done"), Todo(title="B", status="pending")])
        )
        assert result.is_error
        assert "Cannot regress completed todos" in result.output

        # B should remain done
        read_result = await todo_list_tool(Params(todos=None))
        assert "[done] B" in read_result.output
        assert "[done] A" in read_result.output


class TestTodoListNewListValidation:
    """Test error behavior when new todos contain items not in the old list."""

    async def test_new_todo_with_old_incomplete_returns_error(
        self, todo_list_tool: TodoList
    ):
        """If old todos are not all done and new list has new titles, return error."""
        await todo_list_tool(
            Params(todos=[Todo(title="Old task", status="pending")])
        )

        result = await todo_list_tool(
            Params(todos=[Todo(title="New task", status="pending")])
        )
        assert result.is_error
        assert "Cannot replace with new todos" in result.output
        assert "Old task" in result.output

    async def test_new_todo_when_all_old_done_is_allowed(
        self, todo_list_tool: TodoList
    ):
        """If all old todos are done, new list with new titles is allowed."""
        await todo_list_tool(
            Params(todos=[Todo(title="Old task", status="done")])
        )

        result = await todo_list_tool(
            Params(todos=[Todo(title="New task", status="pending")])
        )
        assert not result.is_error

        read_result = await todo_list_tool(Params(todos=None))
        assert "New task" in read_result.output
        assert "Old task" not in read_result.output

    async def test_force_replace_bypasses_validation(
        self, todo_list_tool: TodoList
    ):
        """force_replace=True should bypass the incomplete-todo check."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="Old task", status="pending"),
                    Todo(title="Another old", status="in_progress"),
                ]
            )
        )

        result = await todo_list_tool(
            Params(
                todos=[Todo(title="New task", status="done")],
                force_replace=True,
            )
        )
        assert not result.is_error

        read_result = await todo_list_tool(Params(todos=None))
        assert "New task" in read_result.output
        assert "Old task" not in read_result.output

    async def test_new_todo_mixed_with_old_titles_merges(
        self, todo_list_tool: TodoList
    ):
        """Overlapping titles merge instead of erroring."""
        await todo_list_tool(
            Params(todos=[Todo(title="Keep me", status="pending")])
        )

        result = await todo_list_tool(
            Params(
                todos=[
                    Todo(title="Keep me", status="done"),
                    Todo(title="Brand new", status="pending"),
                ]
            )
        )
        assert not result.is_error
        assert "Keep me" in str(result.display)
        assert "Brand new" in str(result.display)

    async def test_subset_update_does_not_error(
        self, todo_list_tool: TodoList
    ):
        """A strict subset of old titles should always succeed (incremental update)."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="A", status="pending"),
                    Todo(title="B", status="pending"),
                ]
            )
        )

        result = await todo_list_tool(Params(todos=[Todo(title="A", status="done")]))
        assert not result.is_error

    async def test_new_todo_when_old_empty_succeeds(
        self, todo_list_tool: TodoList
    ):
        """Writing new todos when old list is empty should never error."""
        result = await todo_list_tool(
            Params(todos=[Todo(title="New task", status="pending")])
        )
        assert not result.is_error

    async def test_clear_when_old_empty_succeeds(
        self, todo_list_tool: TodoList
    ):
        """Writing an empty list when old list is empty should succeed."""
        result = await todo_list_tool(Params(todos=[]))
        assert not result.is_error

    async def test_single_todo_when_old_empty_succeeds(
        self, todo_list_tool: TodoList
    ):
        """Writing a single Todo when old list is empty should succeed."""
        result = await todo_list_tool(
            Params(todos=Todo(title="Only task", status="in_progress"))
        )
        assert not result.is_error


class TestTodoListSubagent:
    """Test TodoList behavior in subagent context."""

    async def test_subagent_uses_independent_storage(self, runtime: Runtime):
        """Subagent todos should be stored independently from root agent."""
        # Create root tool and set a todo
        root_tool = TodoList(runtime)
        await root_tool(Params(todos=[Todo(title="Root task", status="pending")]))

        # Create a subagent runtime
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-1",
            subagent_type="coder",
        )
        # Initialize the subagent instance directory
        assert subagent_runtime.subagent_store is not None
        subagent_runtime.subagent_store.instance_dir("test-sub-1", create=True)

        sub_tool = TodoList(subagent_runtime)

        # Subagent should start with empty todos
        result = await sub_tool(Params(todos=None))
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower() or "Root task" not in result.output

        # Subagent writes its own todo
        await sub_tool(Params(todos=[Todo(title="Sub task", status="in_progress")]))
        result = await sub_tool(Params(todos=None))
        assert "Sub task" in result.output

        # Root agent should still have its own todo
        result = await root_tool(Params(todos=None))
        assert "Root task" in result.output
        assert "Sub task" not in result.output

    async def test_subagent_no_store_or_id_returns_error(self, runtime: Runtime):
        """When subagent_store or subagent_id is None, save returns an error."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-2",
            subagent_type="coder",
        )
        # Force store/id to None to simulate edge case
        subagent_runtime.subagent_store = None
        subagent_runtime.subagent_id = None

        tool = TodoList(subagent_runtime)

        # Write should return error since state file is unavailable
        result = await tool(Params(todos=[Todo(title="Ghost task", status="pending")]))
        assert result.is_error
        assert "Unable to save subagent todos" in result.output

        # Read should return empty
        result = await tool(Params(todos=None))
        assert not result.is_error
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower()

    async def test_corrupted_subagent_state_file(self, runtime: Runtime):
        """Corrupted subagent state.json should be handled gracefully."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-3",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        instance_dir = subagent_runtime.subagent_store.instance_dir("test-sub-3", create=True)

        # Write corrupted JSON to state.json
        state_file = instance_dir / "state.json"
        state_file.write_text("not valid json {{{", encoding="utf-8")

        tool = TodoList(subagent_runtime)

        # Read should return empty (corrupted file treated as empty)
        result = await tool(Params(todos=None))
        assert not result.is_error
        assert isinstance(result.output, str)
        assert "empty" in result.output.lower()

        # Write should overwrite the corrupted file successfully
        result = await tool(Params(todos=[Todo(title="Recovery task", status="pending")]))
        assert not result.is_error

        # Verify recovery
        result = await tool(Params(todos=None))
        assert "Recovery task" in result.output

    async def test_subagent_malformed_individual_item(self, runtime: Runtime):
        """Malformed individual items in state.json should be skipped, valid ones preserved."""
        import json

        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-4",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        instance_dir = subagent_runtime.subagent_store.instance_dir("test-sub-4", create=True)

        # Write JSON with one valid and one invalid todo item
        state_file = instance_dir / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "todos": [
                        {"title": "Valid task", "status": "pending"},
                        {"bad": "item"},  # missing title and status
                        {"title": "Also valid", "status": "done"},
                    ]
                }
            ),
            encoding="utf-8",
        )

        tool = TodoList(subagent_runtime)
        result = await tool(Params(todos=None))
        assert not result.is_error
        assert "Valid task" in result.output
        assert "Also valid" in result.output
        # The malformed item should be silently skipped
        assert "bad" not in result.output

    async def test_subagent_incremental_update(self, runtime: Runtime):
        """Incremental update should work in subagent context."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-incr",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        subagent_runtime.subagent_store.instance_dir("test-sub-incr", create=True)

        tool = TodoList(subagent_runtime)
        await tool(
            Params(
                todos=[
                    Todo(title="Sub A", status="pending"),
                    Todo(title="Sub B", status="pending"),
                ]
            )
        )

        # Incremental update
        result = await tool(Params(todos=[Todo(title="Sub A", status="done")]))
        assert not result.is_error

        read_result = await tool(Params(todos=None))
        assert "[done] Sub A" in read_result.output
        assert "[pending] Sub B" in read_result.output

    async def test_subagent_new_list_with_incomplete_errors(self, runtime: Runtime):
        """New list validation should work in subagent context."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-err",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        subagent_runtime.subagent_store.instance_dir("test-sub-err", create=True)

        tool = TodoList(subagent_runtime)
        await tool(Params(todos=[Todo(title="Sub task", status="in_progress")]))

        result = await tool(Params(todos=[Todo(title="New sub task", status="pending")]))
        assert result.is_error
        assert "Cannot replace with new todos" in result.output


# --- Additional comprehensive tests ---


class TestTodoListFuzzyMatching:
    """Test fuzzy title matching in error messages."""

    async def test_new_todo_typo_suggests_nearest_title(
        self, todo_list_tool: TodoList
    ):
        """A typo in a new todo title should suggest the nearest existing title."""
        await todo_list_tool(
            Params(todos=[Todo(title="Implement feature", status="pending")])
        )

        result = await todo_list_tool(
            Params(todos=[Todo(title="Implement featuer", status="pending")])
        )
        assert result.is_error
        assert "Cannot replace with new todos" in result.output
        assert "Implement featuer" in result.output
        assert "Implement feature" in result.output
        assert "Did you mean" in result.output

    async def test_new_todo_no_match_does_not_crash(
        self, todo_list_tool: TodoList
    ):
        """A completely unrelated title should still error without crashing."""
        await todo_list_tool(Params(todos=[Todo(title="Old task", status="pending")]))

        result = await todo_list_tool(
            Params(todos=[Todo(title="Completely unrelated", status="pending")])
        )
        assert result.is_error
        assert isinstance(result.output, str)
        assert result.output
        assert isinstance(result.message, str)
        assert result.message

    def test_find_nearest_titles_helper_directly(self):
        """_find_nearest_titles should return BM25 nearest matches."""
        nearest = TodoList._find_nearest_titles(
            ["foo bar"], ["foo baz", "qux"], top_k=1
        )
        assert "foo bar" in nearest
        assert len(nearest["foo bar"]) == 1
        assert nearest["foo bar"][0][0] == "foo baz"

        empty_candidates = TodoList._find_nearest_titles(["foo"], [], top_k=1)
        assert empty_candidates == {"foo": []}

    async def test_fuzzy_suggestions_in_subagent_context(self, runtime: Runtime):
        """Fuzzy suggestions should work when TodoList runs in a subagent."""
        subagent_runtime = runtime.copy_for_subagent(
            agent_id="test-sub-fuzzy",
            subagent_type="coder",
        )
        assert subagent_runtime.subagent_store is not None
        subagent_runtime.subagent_store.instance_dir("test-sub-fuzzy", create=True)

        tool = TodoList(subagent_runtime)
        await tool(Params(todos=[Todo(title="Sub task", status="in_progress")]))

        result = await tool(Params(todos=[Todo(title="Sub taks", status="pending")]))
        assert result.is_error
        assert "Cannot replace with new todos" in result.output
        assert "Sub taks" in result.output
        assert "Sub task" in result.output
        assert "Did you mean" in result.output


class TestTodoModel:
    """Test Todo model validation."""

    def test_title_stripped(self):
        """Title with leading/trailing whitespace should be stripped."""
        todo = Todo(title="  hello  ", status="pending")
        assert todo.title == "hello"

    def test_title_internal_whitespace_preserved(self):
        """Internal whitespace in title should be preserved."""
        todo = Todo(title="hello world", status="pending")
        assert todo.title == "hello world"

    def test_title_min_length(self):
        """Single non-whitespace character should be valid."""
        todo = Todo(title="x", status="pending")
        assert todo.title == "x"

    def test_valid_statuses(self):
        """All valid statuses should be accepted."""
        for status in ("pending", "in_progress", "done"):
            todo = Todo(title="Task", status=status)
            assert todo.status == status


class TestTodoListInternals:
    """Test internal helper methods directly."""

    def test_find_duplicate_titles(self):
        """_find_duplicate_titles returns first duplicate or None."""
        from kimi_cli.tools.todo import TodoList

        assert TodoList._find_duplicate_titles([]) is None
        assert TodoList._find_duplicate_titles([Todo(title="A", status="pending")]) is None
        assert (
            TodoList._find_duplicate_titles(
                [Todo(title="A", status="pending"), Todo(title="B", status="done")]
            )
            is None
        )
        assert (
            TodoList._find_duplicate_titles(
                [
                    Todo(title="A", status="pending"),
                    Todo(title="B", status="done"),
                    Todo(title="A", status="in_progress"),
                ]
            )
            == "A"
        )

    def test_merge_todos_empty_old(self):
        """_merge_todos with empty old returns new."""
        from kimi_cli.tools.todo import TodoList

        tool = object.__new__(TodoList)
        result = tool._merge_todos([], [Todo(title="A", status="pending")])
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].title == "A"

    def test_merge_todos_empty_new_when_all_done(self):
        """_merge_todos with empty new and all old done returns empty."""
        from kimi_cli.tools.todo import TodoList

        tool = object.__new__(TodoList)
        result = tool._merge_todos([Todo(title="A", status="done")], [])
        assert isinstance(result, list)
        assert len(result) == 0

    def test_merge_todos_empty_new_when_not_done_errors(self):
        """_merge_todos with empty new and incomplete old returns error."""
        from kimi_cli.tools.todo import TodoList

        tool = object.__new__(TodoList)
        result = tool._merge_todos([Todo(title="A", status="pending")], [])
        assert isinstance(result, ToolReturnValue)
        assert result.is_error

    def test_merge_todos_superset_when_all_done(self):
        """_merge_todos with superset titles when all old done returns new."""
        from kimi_cli.tools.todo import TodoList

        tool = object.__new__(TodoList)
        result = tool._merge_todos(
            [Todo(title="A", status="done")],
            [Todo(title="A", status="pending"), Todo(title="B", status="pending")],
        )
        assert isinstance(result, list)
        assert len(result) == 2

    def test_read_subagent_state_non_dict(self):
        """_read_subagent_state handles non-JSON and non-dict data."""
        import tempfile

        from kimi_cli.tools.todo import TodoList

        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".json") as f:
            f.write("[1, 2, 3]")  # valid JSON but not a dict
            path = Path(f.name)

        result = TodoList._read_subagent_state(path)
        assert result == {}
        path.unlink()

    def test_read_subagent_state_nonexistent(self):
        """_read_subagent_state returns empty dict for nonexistent file."""
        from kimi_cli.tools.todo import TodoList

        result = TodoList._read_subagent_state(Path("/nonexistent/state.json"))
        assert result == {}


class TestTodoListRegression:
    """Test edge cases around status regression and force_replace."""

    async def test_regression_allowed_with_force_replace(self, todo_list_tool: TodoList):
        """force_replace=True allows regressing done todos."""
        await todo_list_tool(
            Params(todos=[Todo(title="A", status="pending"), Todo(title="B", status="done")])
        )

        result = await todo_list_tool(
            Params(
                todos=[Todo(title="A", status="done"), Todo(title="B", status="pending")],
                force_replace=True,
            )
        )
        assert not result.is_error
        assert "force_replace=True bypasses all validation logic" in result.message

        read = await todo_list_tool(Params(todos=None))
        assert "[pending] B" in read.output

    async def test_multiple_duplicate_titles(self, todo_list_tool: TodoList):
        """Multiple duplicate titles are still rejected."""
        result = await todo_list_tool(
            Params(
                todos=[
                    Todo(title="A", status="pending"),
                    Todo(title="B", status="pending"),
                    Todo(title="C", status="pending"),
                    Todo(title="B", status="done"),
                    Todo(title="D", status="pending"),
                ]
            )
        )
        assert result.is_error
        assert "Duplicate todo titles found" in result.output

    async def test_all_done_replace_with_mixed_old_new(self, todo_list_tool: TodoList):
        """When all old are done, new list with mix of old (still done) and new titles works."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="Old A", status="done"),
                    Todo(title="Old B", status="done"),
                ]
            )
        )

        # Old A stays done, New C is added — no regression
        result = await todo_list_tool(
            Params(
                todos=[
                    Todo(title="Old A", status="done"),
                    Todo(title="New C", status="in_progress"),
                ]
            )
        )
        assert not result.is_error

        read = await todo_list_tool(Params(todos=None))
        assert "Old A" in read.output
        assert "Old B" in read.output
        assert "New C" in read.output

    async def test_display_block_on_regression_error(self, todo_list_tool: TodoList):
        """Regression error response includes TodoDisplayBlock."""
        from kimi_cli.tools.display import TodoDisplayBlock

        await todo_list_tool(
            Params(todos=[Todo(title="A", status="pending"), Todo(title="B", status="done")])
        )

        result = await todo_list_tool(
            Params(todos=[Todo(title="A", status="done"), Todo(title="B", status="pending")])
        )
        assert result.is_error
        assert len(result.display) == 1
        assert isinstance(result.display[0], TodoDisplayBlock)
        items = result.display[0].items
        assert any(i.title == "A" and i.status == "done" for i in items)
        assert any(i.title == "B" and i.status == "done" for i in items)

    async def test_update_all_to_done_then_replace(self, todo_list_tool: TodoList):
        """Mark all as done, then replace with completely new list."""
        await todo_list_tool(
            Params(
                todos=[
                    Todo(title="A", status="pending"),
                    Todo(title="B", status="in_progress"),
                ]
            )
        )

        # Mark all done
        await todo_list_tool(
            Params(todos=[Todo(title="A", status="done"), Todo(title="B", status="done")])
        )

        # Replace with new list
        result = await todo_list_tool(
            Params(todos=[Todo(title="C", status="pending")])
        )
        assert not result.is_error

        read = await todo_list_tool(Params(todos=None))
        assert "C" in read.output
        assert "A" not in read.output
        assert "B" not in read.output
