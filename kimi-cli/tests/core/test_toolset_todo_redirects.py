"""Toolset-level guards for the todo-tool merge.

Nothing may reach the retired tool names any more: every spelling an LLM (or a
session recorded before the merge) can produce must redirect to ``todo_list``,
and the argument repair layer must keep fixing the old payload shapes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from kimi_cli.soul.toolset import (  # noqa: PLC2701
    _build_platform_redirects,
    _repair_todo_arguments,
)
from kimi_cli.tools import RETIRED_TODO_TOOL_NAMES
from kimi_cli.tools.todo import Params, TodoList
from kosong.tooling import TOOL_NAME_REDIRECTS, normalize_tool_name, resolve_tool_name

TODO_TARGET = "todo_list"

# Names a model hallucinates when it wants a planning tool.
HALLUCINATED_TODO_NAMES = [
    "TodoList", "TaskList", "Todo", "Todos", "TaskManager", "TaskPlan", "Plan",
    "Checklist", "TaskTracker", "Progress",
]

# Parent/child flavoured inventions plus the retired names: all route to the one tool.
SUBTREE_TODO_NAMES = [
    "SubTodo", "TodoChild", "TodoAdd", "AddSubTodo", "TodoDetail", "TodoEdit",
    "SubTask", "AddTask", "TaskDetail", "TaskSub", "TodoTree", "TodoStack",
    "TodoHierarchy", "TodoPlan", "TaskList", "UpdateTodo", "SetTodo", "TodoListSub",
]


def _redirects() -> dict[str, str]:
    return _build_platform_redirects()


class TestNameRedirection:
    @pytest.mark.parametrize("name", HALLUCINATED_TODO_NAMES)
    def test_hallucinated_names_point_at_the_single_tool(self, name: str) -> None:
        assert TOOL_NAME_REDIRECTS[name] == TODO_TARGET

    @pytest.mark.parametrize("name", SUBTREE_TODO_NAMES)
    def test_subtree_names_point_at_the_single_tool(self, name: str) -> None:
        assert _redirects()[normalize_tool_name(name)] == TODO_TARGET

    @pytest.mark.parametrize("name", RETIRED_TODO_TOOL_NAMES)
    def test_retired_names_are_redirected(self, name: str) -> None:
        assert _redirects()[normalize_tool_name(name)] == TODO_TARGET

    @pytest.mark.parametrize("name", [*RETIRED_TODO_TOOL_NAMES, *HALLUCINATED_TODO_NAMES])
    def test_resolution_against_a_real_toolset_name_set(self, name: str) -> None:
        valid = {TODO_TARGET, "read", "bash"}
        resolution = resolve_tool_name(name, valid, redirects=_redirects())
        assert resolution.name == TODO_TARGET

    def test_the_surviving_tool_is_named_by_the_module(self) -> None:
        assert TodoList.name == TODO_TARGET


class TestArgumentRepair:
    def test_bare_string_batch_becomes_title_items(self) -> None:
        repaired = _repair_todo_arguments(TODO_TARGET, {"todos": ["Buy milk", "Walk dog"]})
        assert repaired == {"todos": [{"title": "Buy milk"}, {"title": "Walk dog"}]}

    def test_no_status_is_injected(self) -> None:
        repaired = _repair_todo_arguments(TODO_TARGET, {"todos": ["Buy milk"]})
        assert "status" not in repaired["todos"][0]

    @pytest.mark.parametrize("batch_key", ["updates", "items", "tasks", "operations", "changes"])
    def test_batch_synonyms_land_on_todos(self, batch_key: str) -> None:
        payload: dict[str, Any] = {batch_key: [{"title": "A"}]}
        assert _repair_todo_arguments(TODO_TARGET, payload) == {"todos": [{"title": "A"}]}

    def test_singular_item_key_is_folded(self) -> None:
        payload = {"todo": {"title": "A", "status": "done"}}
        assert _repair_todo_arguments(TODO_TARGET, payload) == {
            "todos": [{"title": "A", "status": "done"}]
        }

    def test_singular_top_level_edit_is_left_for_the_params_layer(self) -> None:
        """The repair layer only normalizes lists; the single-edit fold lives in
        ``Params``, so the payload must survive untouched here."""
        payload = {"title": "A", "status": "in_progress", "rename_to": "B"}
        assert _repair_todo_arguments(TODO_TARGET, dict(payload)) == payload
        params = Params.model_validate(payload)
        assert params.todos is not None
        assert params.todos[0].rename_to == "B"

    @pytest.mark.parametrize("name", RETIRED_TODO_TOOL_NAMES)
    def test_retired_names_get_the_same_repair(self, name: str) -> None:
        assert _repair_todo_arguments(name, {"todos": ["X"]}) == {"todos": [{"title": "X"}]}

    def test_other_tools_are_untouched(self) -> None:
        args = {"command": "ls", "task": "ignored"}
        assert _repair_todo_arguments("bash", dict(args)) == args

    def test_json_string_batches_survive_the_repair(self) -> None:
        """A stringified batch is parsed by ``Params``, not by the repair layer."""
        payload = {"todos": '[{"title": "A", "status": "done"}]'}
        assert _repair_todo_arguments(TODO_TARGET, dict(payload)) == payload
        params = Params.model_validate(payload)
        assert params.todos is not None
        assert params.todos[0].title == "A"

    def test_bare_string_inside_a_batch_is_wrapped(self) -> None:
        repaired = _repair_todo_arguments(TODO_TARGET, {"updates": ["Buy milk"]})
        assert repaired == {"todos": [{"title": "Buy milk"}]}

    def test_batch_key_synonyms_reach_todos_even_as_json_text(self) -> None:
        payload = {"updates": '[{"title": "A"}]'}
        assert _repair_todo_arguments(TODO_TARGET, dict(payload)) == {"todos": payload["updates"]}
        params = Params.model_validate(_repair_todo_arguments(TODO_TARGET, dict(payload)))
        assert params.todos is not None and params.todos[0].title == "A"

    def test_repaired_arguments_satisfy_the_item_schema(self) -> None:
        from kimi_cli.tools.todo import Params

        repaired = _repair_todo_arguments(TODO_TARGET, {"todos": ["A", "B"]})
        params = Params.model_validate(repaired)
        assert params.todos is not None
        assert [item.title for item in params.todos] == ["A", "B"]


class TestFieldAliases:
    def test_the_todo_tool_uses_its_own_alias_table(self) -> None:
        from kosong.tooling import FIELD_ALIASES_TODO_LIST  # noqa: PLC2701

        assert FIELD_ALIASES_TODO_LIST["task"] == "title"
        assert FIELD_ALIASES_TODO_LIST["updates"] == "todos"
        assert FIELD_ALIASES_TODO_LIST["parent"] == "scope"
        assert FIELD_ALIASES_TODO_LIST["conflict"] == "on_conflict"
        assert set(FIELD_ALIASES_TODO_LIST.items()) <= set(TodoList.field_aliases.items())

    def test_the_title_alias_is_not_leaked_globally(self) -> None:
        from kosong.tooling import _COMMON_FIELD_ALIASES  # noqa: PLC2701

        assert _COMMON_FIELD_ALIASES.get("task") != "title"
        assert _COMMON_FIELD_ALIASES["task"] == "prompt"


class TestRegistration:
    @pytest.mark.parametrize(
        "manifest",
        [
            Path("src/kimi_cli/agents/default/agent.yaml"),
            Path("src/kimi_cli/agents/okabe/agent.yaml"),
        ],
    )
    def test_base_agents_list_the_single_todo_tool(self, manifest: Path) -> None:
        assert manifest.exists(), manifest
        spec = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        tools = [entry for entry in spec["agent"].get("tools", []) if ".todo:" in str(entry)]
        assert tools == [f"kimi_cli.tools.todo:{TODO_TARGET}"], tools

    def test_no_retired_entry_in_any_manifest(self) -> None:
        root = Path("src/kimi_cli/agents")
        offenders: list[str] = []
        for path in root.rglob("*.yaml"):
            text = path.read_text(encoding="utf-8")
            for retired in RETIRED_TODO_TOOL_NAMES:
                if f"todo:{retired}" in text:
                    offenders.append(f"{path}:{retired}")
        assert offenders == []
