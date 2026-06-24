from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kimi_cli.wire.types import TextPart

prompt_mod = importlib.import_module("kimix.utils.prompt")


@dataclass
class FakeStatus:
    context_usage: float
    context_tokens: int


class FakeToolset:
    def __init__(self, has_set_todo: bool = True) -> None:
        self.has_set_todo = has_set_todo

    def find(self, name: str) -> object | None:
        if name == "TodoList" and self.has_set_todo:
            return object()
        return None


class FakeAgent:
    def __init__(self, has_set_todo: bool = True) -> None:
        self.toolset = FakeToolset(has_set_todo=has_set_todo)


class FakeSoul:
    def __init__(self, has_set_todo: bool = True) -> None:
        self.agent = FakeAgent(has_set_todo=has_set_todo)


@dataclass
class TodoItemState:
    title: str
    status: str


class FakeState:
    def __init__(self, todos: list[TodoItemState] | None = None) -> None:
        self.todos = todos or []


class FakeCLISession:
    def __init__(self, todos: list[TodoItemState] | None = None) -> None:
        self.state = FakeState(todos=todos)


class FakeCLI:
    def __init__(self, has_set_todo: bool = True, todos: list[TodoItemState] | None = None) -> None:
        self.soul = FakeSoul(has_set_todo=has_set_todo)
        self.session = FakeCLISession(todos=todos)


class FakeSessionWithCLI:
    def __init__(
        self,
        has_set_todo: bool = True,
        todos: list[TodoItemState] | None = None,
        context_usage: float = 0.125,
        context_tokens: int = 1024,
    ) -> None:
        self._cli = FakeCLI(has_set_todo=has_set_todo, todos=todos)
        self.status = FakeStatus(context_usage=context_usage, context_tokens=context_tokens)
        self.cancelled = False
        self._cancel_event = None
        self._tmp_data = {}
        self.prompts: list[str] = []

    async def prompt(self, prompt: str, *, merge_wire_messages: bool = False) -> Any:
        self.last_prompt = prompt
        self.prompts.append(prompt)
        yield TextPart(text="prompt output")

    def cancel(self) -> None:
        self.cancelled = True


class FakeSessionWithoutCLI:
    def __init__(self, context_usage: float = 0.125, context_tokens: int = 1024) -> None:
        self.status = FakeStatus(context_usage=context_usage, context_tokens=context_tokens)
        self.cancelled = False
        self._cancel_event = None
        self._tmp_data = {}
        self.prompts: list[str] = []

    async def prompt(self, prompt: str, *, merge_wire_messages: bool = False) -> Any:
        self.last_prompt = prompt
        self.prompts.append(prompt)
        yield TextPart(text="prompt output")

    def cancel(self) -> None:
        self.cancelled = True


def _suppress_stream(monkeypatch: Any) -> None:
    monkeypatch.setattr(prompt_mod.base._stream, "colorful_print_word", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_mod.base._stream, "print_word", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_mod, "_print_usage", lambda *args, **kwargs: None)


def test_reminder_injected_when_todos_unfinished(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    session = FakeSessionWithCLI(
        has_set_todo=True,
        todos=[
            TodoItemState(title="Analyze requirement", status="pending"),
            TodoItemState(title="Implement helper", status="in_progress"),
            TodoItemState(title="Run tests", status="done"),
        ],
    )

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    # The fake session never updates todo statuses, so both the regular and the
    # strong follow-up reminder are injected before cleanup.
    assert len(session.prompts) == 3
    assert session.prompts[0] == "hello"
    reminder = session.prompts[1]
    assert "<system-reminder>" in reminder
    assert "You have unfinished todos" in reminder
    assert "- [pending] Analyze requirement" in reminder
    assert "- [in_progress] Implement helper" in reminder
    assert "- [done] Run tests" in reminder
    assert "</system-reminder>" in reminder

    strong_reminder = session.prompts[2]
    assert "<system-reminder>" in strong_reminder
    assert "CRITICAL" in strong_reminder
    assert "MUST use `TodoList`" in strong_reminder
    assert "- [pending] Analyze requirement" in strong_reminder
    assert "- [in_progress] Implement helper" in strong_reminder
    assert "</system-reminder>" in strong_reminder


def test_no_reminder_when_all_todos_done(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    session = FakeSessionWithCLI(
        has_set_todo=True,
        todos=[
            TodoItemState(title="Analyze requirement", status="done"),
            TodoItemState(title="Implement helper", status="done"),
        ],
    )

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    assert session.prompts == ["hello"]


def test_no_reminder_when_todo_list_empty(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    session = FakeSessionWithCLI(has_set_todo=True, todos=[])

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    assert session.prompts == ["hello"]


def test_no_reminder_when_set_todo_tool_absent(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    session = FakeSessionWithCLI(
        has_set_todo=False,
        todos=[TodoItemState(title="Analyze requirement", status="pending")],
    )

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    assert session.prompts == ["hello"]


def test_prompt_async_works_without_cli_attribute(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    session = FakeSessionWithoutCLI()

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    assert session.prompts == ["hello"]


def test_reminder_stops_when_todos_marked_done(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    todos = [
        TodoItemState(title="Analyze requirement", status="pending"),
    ]
    session = FakeSessionWithCLI(has_set_todo=True, todos=todos)

    async def mark_done_prompt(self: Any, prompt: str, *, merge_wire_messages: bool = False) -> Any:
        if "system-reminder" in prompt:
            session._cli.session.state.todos[0].status = "done"
        self.last_prompt = prompt
        self.prompts.append(prompt)
        yield TextPart(text="prompt output")

    monkeypatch.setattr(FakeSessionWithCLI, "prompt", mark_done_prompt)

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    assert len(session.prompts) == 2
    assert session.prompts[0] == "hello"
    assert "You have unfinished todos" in session.prompts[1]


def test_no_reminder_when_ensure_todo_finished_false(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    session = FakeSessionWithCLI(
        has_set_todo=True,
        todos=[
            TodoItemState(title="Analyze requirement", status="pending"),
            TodoItemState(title="Implement helper", status="in_progress"),
        ],
    )

    asyncio.run(
        prompt_mod.prompt_async(
            "hello", session=session, info_print=False, ensure_todo_finished=False
        )
    )

    assert session.prompts == ["hello"]


def test_todos_are_cleared_after_prompt_async(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    todos = [
        TodoItemState(title="Analyze requirement", status="pending"),
        TodoItemState(title="Implement helper", status="in_progress"),
        TodoItemState(title="Run tests", status="done"),
    ]
    session = FakeSessionWithCLI(has_set_todo=True, todos=todos)

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    assert session._cli.session.state.todos == []


class FakeCLISessionWithSave(FakeCLISession):
    def __init__(self, todos: list[TodoItemState] | None = None) -> None:
        super().__init__(todos=todos)
        self.saved = False

    def save_state(self) -> None:
        self.saved = True


class FakeRuntimeRoot:
    role: str = "root"


class FakeCLIRoot:
    def __init__(self, todos: list[TodoItemState] | None = None) -> None:
        self.session = FakeCLISessionWithSave(todos=todos)
        self._runtime = FakeRuntimeRoot()


class FakeSessionRoot:
    def __init__(self, todos: list[TodoItemState] | None = None) -> None:
        self._cli = FakeCLIRoot(todos=todos)


def test_root_todos_cleared_from_disk(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    todos = [TodoItemState(title="task", status="pending")]
    session = FakeSessionRoot(todos=todos)

    asyncio.run(prompt_mod._clear_session_todos(session))

    assert session._cli.session.state.todos == []
    assert session._cli.session.saved is True


class FakeSubagentStore:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def instance_dir(self, agent_id: str) -> Path:
        path = self._base_dir / agent_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class FakeRuntimeSubagent:
    role: str = "subagent"

    def __init__(self, store: FakeSubagentStore, agent_id: str) -> None:
        self.subagent_store = store
        self.subagent_id = agent_id


class FakeCLISubagent:
    def __init__(self, state: FakeState, runtime: FakeRuntimeSubagent) -> None:
        self.session = FakeCLISession()
        self.session.state = state
        self._runtime = runtime


class FakeSessionSubagent:
    def __init__(self, state: FakeState, runtime: FakeRuntimeSubagent) -> None:
        self._cli = FakeCLISubagent(state, runtime)


def test_subagent_todos_cleared_from_disk(tmp_path: Path) -> None:
    store = FakeSubagentStore(tmp_path / "subagents")
    runtime = FakeRuntimeSubagent(store, "agent1")
    state = FakeState(todos=[TodoItemState(title="task", status="pending")])
    session = FakeSessionSubagent(state, runtime)

    state_file = store.instance_dir("agent1") / "state.json"
    state_file.write_text(json.dumps({"todos": [{"title": "old", "status": "pending"}], "other": "data"}))

    asyncio.run(prompt_mod._clear_session_todos(session))

    assert session._cli.session.state.todos == []
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data.get("todos") == []
    assert data.get("other") == "data"


def test_todos_cleared_even_when_reminder_fails(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)

    async def failing_prompt(self: Any, prompt: str, *, merge_wire_messages: bool = False) -> Any:
        if "system-reminder" in prompt:
            raise RuntimeError("reminder failed")
        self.last_prompt = prompt
        self.prompts.append(prompt)
        yield TextPart(text="prompt output")

    monkeypatch.setattr(FakeSessionWithCLI, "prompt", failing_prompt)

    todos = [
        TodoItemState(title="Analyze requirement", status="pending"),
    ]
    session = FakeSessionWithCLI(has_set_todo=True, todos=todos)

    asyncio.run(prompt_mod.prompt_async("hello", session=session, info_print=False))

    assert session._cli.session.state.todos == []


class FakeCLISubagentWithToolset(FakeCLISubagent):
    def __init__(self, state: FakeState, runtime: FakeRuntimeSubagent) -> None:
        super().__init__(state, runtime)
        self.soul = FakeSoul(has_set_todo=True)


class FakeSessionSubagentWithToolset:
    def __init__(self, state: FakeState, runtime: FakeRuntimeSubagent) -> None:
        self._cli = FakeCLISubagentWithToolset(state, runtime)


def test_subagent_reminder_reads_from_state_file(tmp_path: Path) -> None:
    store = FakeSubagentStore(tmp_path / "subagents")
    runtime = FakeRuntimeSubagent(store, "agent1")
    state = FakeState(todos=[])
    session = FakeSessionSubagentWithToolset(state, runtime)

    state_file = store.instance_dir("agent1") / "state.json"
    state_file.write_text(
        json.dumps({"todos": [{"title": "Subagent task", "status": "pending"}]})
    )

    reminder = asyncio.run(prompt_mod._maybe_build_todo_reminder(session))

    assert reminder is not None
    assert "Subagent task" in reminder
    assert "- [pending] Subagent task" in reminder


def test_export_todo_list_to_json(tmp_path: Path, monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    todos = [
        TodoItemState(title="Analyze requirement", status="pending"),
        TodoItemState(title="Implement helper", status="in_progress"),
        TodoItemState(title="Run tests", status="done"),
    ]
    session = FakeSessionWithCLI(has_set_todo=True, todos=todos)
    export_path = tmp_path / "todos.json"

    asyncio.run(
        prompt_mod.prompt_async(
            "hello",
            session=session,
            info_print=False,
            export_todo_list_path=export_path,
        )
    )

    assert export_path.exists()
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported == [
        {"title": "Analyze requirement", "status": "pending"},
        {"title": "Implement helper", "status": "in_progress"},
        {"title": "Run tests", "status": "done"},
    ]
    # Todos must not be cleared when exporting.
    assert len(session._cli.session.state.todos) == 3


def test_invalid_export_path_prints_error_and_clears(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    errors: list[str] = []
    monkeypatch.setattr(prompt_mod.base, "print_error", errors.append)

    todos = [TodoItemState(title="task", status="pending")]
    session = FakeSessionWithCLI(has_set_todo=True, todos=todos)

    asyncio.run(
        prompt_mod.prompt_async(
            "hello",
            session=session,
            info_print=False,
            export_todo_list_path=Path("todos.txt"),
        )
    )

    assert any("Invalid todo list export path" in e for e in errors)
    # Falls back to default clear behavior.
    assert session._cli.session.state.todos == []


def test_export_session_todos_for_subagent(tmp_path: Path) -> None:
    store = FakeSubagentStore(tmp_path / "subagents")
    runtime = FakeRuntimeSubagent(store, "agent1")
    state = FakeState(todos=[])
    session = FakeSessionSubagentWithToolset(state, runtime)

    state_file = store.instance_dir("agent1") / "state.json"
    state_file.write_text(
        json.dumps({"todos": [{"title": "Subagent task", "status": "pending"}]})
    )

    export_path = tmp_path / "exported.json"
    asyncio.run(prompt_mod._export_session_todos(session, export_path))

    assert export_path.exists()
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported == [{"title": "Subagent task", "status": "pending"}]
    # Source todos must remain untouched.
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["todos"] == [{"title": "Subagent task", "status": "pending"}]


class FakeSetTodoTool:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, params: Any) -> dict[str, Any]:
        self.calls.append(params)
        return {}


class FakeToolsetWithImporter:
    def __init__(self, tool: Any) -> None:
        self.tool = tool

    def find(self, name: str) -> object | None:
        if name == "TodoList":
            return self.tool
        return None


class FakeAgentWithImporter:
    def __init__(self, tool: Any) -> None:
        self.toolset = FakeToolsetWithImporter(tool)


class FakeSoulWithImporter:
    def __init__(self, tool: Any) -> None:
        self.agent = FakeAgentWithImporter(tool)


class FakeCLIWithImporter:
    def __init__(self, tool: Any, todos: list[TodoItemState] | None = None) -> None:
        self.soul = FakeSoulWithImporter(tool)
        self.session = FakeCLISession(todos=todos)


class FakeSessionWithImporter:
    def __init__(self, tool: Any, todos: list[TodoItemState] | None = None) -> None:
        self._cli = FakeCLIWithImporter(tool, todos=todos)


def test_import_session_todos(tmp_path: Path) -> None:
    tool = FakeSetTodoTool()
    session = FakeSessionWithImporter(tool)
    todo_path = tmp_path / "todos.json"
    todo_path.write_text(
        json.dumps([
            {"title": "Step 1", "status": "pending"},
            {"title": "Step 2", "status": "in_progress"},
        ])
    )

    asyncio.run(prompt_mod._import_session_todos(session, todo_path))

    assert len(tool.calls) == 1
    params = tool.calls[0]
    assert params.force_replace is True
    assert len(params.todos) == 2
    assert params.todos[0].title == "Step 1"
    assert params.todos[0].status == "pending"
    assert params.todos[1].title == "Step 2"
    assert params.todos[1].status == "in_progress"


def test_import_session_todos_skips_missing_file(tmp_path: Path) -> None:
    tool = FakeSetTodoTool()
    session = FakeSessionWithImporter(tool)
    missing_path = tmp_path / "missing.json"

    asyncio.run(prompt_mod._import_session_todos(session, missing_path))

    assert tool.calls == []


def test_import_session_todos_skips_invalid_json(tmp_path: Path, monkeypatch: Any) -> None:
    tool = FakeSetTodoTool()
    session = FakeSessionWithImporter(tool)
    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("not json")

    errors: list[str] = []
    monkeypatch.setattr(prompt_mod.base, "print_error", errors.append)

    asyncio.run(prompt_mod._import_session_todos(session, invalid_path))

    assert tool.calls == []
    assert any("Failed to read todo list" in e for e in errors)


class FakePlannerSessionForPlan:
    def __init__(self, plan_file: Path) -> None:
        self._cancel_event = None
        self.prompts: list[str] = []
        self._plan_file = plan_file
        self._custom_data: dict[str, Any] = {}

    def get_custom_data(self) -> dict[str, Any]:
        return self._custom_data

    async def prompt(self, prompt_str: str, *, merge_wire_messages: bool = False) -> Any:
        self.prompts.append(prompt_str)
        if not self._plan_file.exists():
            self._plan_file.write_text("# Plan\n\n1. Do thing\n", encoding="utf-8")
        yield TextPart(text="plan output")

    def cancel(self) -> None:
        pass


class FakeExecutionSessionForPlan:
    def __init__(self) -> None:
        self.prompts: list[str] = []


def test_prompt_plan_async_exports_and_imports_todos(tmp_path: Path, monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    plan_file = tmp_path / "plan.md"
    planner_session = FakePlannerSessionForPlan(plan_file)
    execution_session = FakeExecutionSessionForPlan()

    imported: list[tuple[Any, Path]] = []
    prompt_async_calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def fake_import(session: Any, path: Path) -> None:
        imported.append((session, path))

    async def fake_create_session_async(*args: Any, **kwargs: Any) -> Any:
        return planner_session

    def fake_create_default_session() -> Any:
        return execution_session

    async def fake_close_session_async(session: Any) -> None:
        pass

    async def fake_prompt_async(prompt_str: str, session: Any, **kwargs: Any) -> None:
        prompt_async_calls.append((prompt_str, session, kwargs))
        session.prompts.append(prompt_str)
        export_path = kwargs.get("export_todo_list_path")
        if export_path is not None:
            export_path.parent.mkdir(parents=True, exist_ok=True)
            export_path.write_text("[]", encoding="utf-8")

    monkeypatch.setattr(prompt_mod, "_create_session_async", fake_create_session_async)
    monkeypatch.setattr(prompt_mod, "_create_default_session", fake_create_default_session)
    monkeypatch.setattr(prompt_mod, "close_session_async", fake_close_session_async)
    monkeypatch.setattr(prompt_mod, "_import_session_todos", fake_import)
    monkeypatch.setattr(prompt_mod, "prompt_async", fake_prompt_async)
    monkeypatch.setattr(prompt_mod.os, "startfile", lambda _path: None)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    asyncio.run(prompt_mod.prompt_plan_async("test requirement", plan_file))

    # Planner direct reminder should mention TodoList
    assert any("call `TodoList`" in p for p in planner_session.prompts)

    # prompt_async should have been called for planner export and for the two execution prompts
    planner_export_calls = [
        (p, s, k) for p, s, k in prompt_async_calls
        if s is planner_session and k.get("export_todo_list_path") is not None
    ]
    assert len(planner_export_calls) == 1
    export_path = planner_export_calls[0][2]["export_todo_list_path"]
    assert export_path.parent.name == ".kimix_cache"
    assert export_path.suffix == ".json"

    # Import should be called once for the execution session with the same path
    assert len(imported) == 1
    assert imported[0][0] is execution_session
    assert imported[0][1] == export_path

    # Execution prompts should include the plan content
    assert any("Implement this plan:" in p for p in execution_session.prompts)
    assert any("Review this plan" in p for p in execution_session.prompts)
