"""prompt_async must wait for a session's background subagents before returning.

When the session's prompt is finished but subagents spawned through the
``subagent`` tool are still running detached, ``prompt_async`` (finally block
in ``kimix.utils.prompt``) waits for all of them to settle — closing the
parent session would otherwise cascade-close their scratch sessions mid-run.
"""
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

from kimi_cli.wire.types import TextPart

prompt_mod = importlib.import_module("kimix.utils.prompt")


class _FakeCLI:
    def __init__(self) -> None:
        # ``custom_data`` is where the Agent tool tracks detached background
        # subagent tasks for the owning session.
        self.session = SimpleNamespace(custom_data={}, state=SimpleNamespace(todos=[]))
        self._runtime = None


class _FakeSession:
    def __init__(self) -> None:
        self._cli = _FakeCLI()
        self._cancel_event = None
        self._tmp_data = {}
        self.prompts: list[str] = []

    async def prompt(self, prompt: str, *, merge_wire_messages: bool = False) -> Any:
        self.prompts.append(prompt)
        yield TextPart(text="prompt output")

    def cancel(self) -> None:
        pass


def _suppress_stream(monkeypatch: Any) -> None:
    monkeypatch.setattr(prompt_mod.base._stream, "colorful_print_word", lambda *a, **k: None)
    monkeypatch.setattr(prompt_mod.base._stream, "print_word", lambda *a, **k: None)
    monkeypatch.setattr(prompt_mod, "_print_usage", lambda *a, **k: None)


def test_prompt_async_waits_for_background_subagents(monkeypatch: Any) -> None:
    _suppress_stream(monkeypatch)
    session = _FakeSession()
    finished: list[str] = []

    async def _scenario() -> None:
        from kimix.tools.agent import _background_tasks_for

        async def _slow_subagent() -> None:
            await asyncio.sleep(0.05)
            finished.append("subagent")

        tasks = _background_tasks_for(session)
        task = asyncio.create_task(_slow_subagent())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        # The fake prompt finishes instantly; the slow subagent must still be
        # awaited before prompt_async returns.
        await prompt_mod.prompt_async("hello", session=session, info_print=False)

    asyncio.run(_scenario())

    assert finished == ["subagent"]
    assert session.prompts == ["hello"]


def test_prompt_async_survives_failing_background_subagent(monkeypatch: Any) -> None:
    """A crashed background subagent must not break the session teardown wait."""
    _suppress_stream(monkeypatch)
    session = _FakeSession()

    async def _scenario() -> None:
        from kimix.tools.agent import _background_tasks_for

        async def _boom() -> None:
            raise RuntimeError("subagent exploded")

        tasks = _background_tasks_for(session)
        task = asyncio.create_task(_boom())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

        await prompt_mod.prompt_async("hello", session=session, info_print=False)

    asyncio.run(_scenario())
    assert session.prompts == ["hello"]
