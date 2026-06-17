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
        if name == "SetTodoList" and self.has_set_todo:
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

    assert len(session.prompts) == 2
    assert session.prompts[0] == "hello"
    reminder = session.prompts[1]
    assert "<system-reminder>" in reminder
    assert "You have unfinished todos" in reminder
    assert "- [pending] Analyze requirement" in reminder
    assert "- [in_progress] Implement helper" in reminder
    assert "- [done] Run tests" in reminder
    assert "</system-reminder>" in reminder


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
