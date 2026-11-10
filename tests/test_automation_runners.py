"""Offline tests for the live automation runner scripts."""

from __future__ import annotations

import asyncio
import importlib
import logging
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

RUNNER_NAMES = ("run_computer_use", "run_browser_use")


class FakeSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def export(self, *, output_path: str) -> tuple[Path, int]:
        self.events.append("export")
        path = Path(output_path)
        path.write_text("# Fake session transcript\n", encoding="utf-8")
        return path, 2


def load_runner(name: str) -> ModuleType:
    return importlib.import_module(name)


@pytest.mark.parametrize("runner_name", RUNNER_NAMES)
def test_runner_prompts_exports_logs_and_closes(
    runner_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = load_runner(runner_name)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    transcript_path = tmp_path / f"{runner_name}.markdown"
    events: list[str] = []
    session = FakeSession(events)
    init_kwargs: dict[str, Any] = {}
    create_kwargs: dict[str, Any] = {}
    prompts: list[str] = []

    def fake_init(**kwargs: Any) -> None:
        init_kwargs.update(kwargs)
        events.append("init")

    def create(**kwargs: Any) -> FakeSession:
        create_kwargs.update(kwargs)
        events.append("create")
        return session

    def fake_prompt(
        prompt_text: str,
        *,
        session: FakeSession,
        ensure_todo_finished: bool,
        format_output: bool,
    ) -> None:
        assert session is not None
        assert ensure_todo_finished is False
        assert format_output is True
        prompts.append(prompt_text)
        events.append("prompt")

    def close(current_session: FakeSession) -> None:
        assert current_session is session
        events.append("close")

    monkeypatch.setattr(runner, "CONFIG_PATH", config_path)
    monkeypatch.setattr(runner, "WORK_DIR", tmp_path)
    monkeypatch.setattr(runner, "init", fake_init)
    monkeypatch.setattr(runner, "create_session", create)
    monkeypatch.setattr(runner, "prompt", fake_prompt)
    monkeypatch.setattr(runner, "run_on_event_loop", lambda coroutine: asyncio.run(coroutine))
    monkeypatch.setattr(runner, "close_session", close)
    monkeypatch.setattr(runner, "temporary_export_path", lambda: transcript_path)

    with caplog.at_level(logging.INFO, logger=runner_name):
        result = runner.run()

    assert result == transcript_path
    assert transcript_path.read_text(encoding="utf-8") == "# Fake session transcript\n"
    assert events == ["init", "create", "prompt", "export", "close"]
    assert init_kwargs == {"config_path": config_path, "yolo": True, "think": True}
    assert create_kwargs.keys() == {"work_dir"}
    assert str(create_kwargs["work_dir"]) == str(tmp_path)
    assert str(transcript_path) in caplog.text
    assert len(prompts) == 1
    assert "do not retry" in prompts[0]
    assert "end the\nsession" in prompts[0]


@pytest.mark.parametrize("runner_name", RUNNER_NAMES)
def test_temporary_export_path_is_unique_and_does_not_exist(runner_name: str) -> None:
    runner = load_runner(runner_name)
    path = runner.temporary_export_path()
    assert not path.exists()
    assert path.parent == Path(tempfile.gettempdir())
    expected_prefix = runner_name.removeprefix("run_").replace("_", "-")
    assert path.name.startswith(f"kimix-{expected_prefix}-session-")
    assert path.suffix == ".markdown"


def test_computer_prompt_targets_unique_file_and_only_computer_tool() -> None:
    runner = load_runner("run_computer_use")
    target = Path(tempfile.gettempdir()) / "unique-computer-smoke-test.txt"

    prompt = runner.build_prompt(target)

    assert str(target) in prompt
    assert "Use only\n`computer_use`" in prompt
    assert "Save As dialog" in prompt
    assert "Edited and saved successfully." in prompt
    assert "unavailable, invalid, or broken" in prompt


def test_browser_prompt_verifies_fixture_and_closes_browser() -> None:
    runner = load_runner("run_browser_use")

    assert runner.FIXTURE_URL.startswith("data:text/html,")
    assert runner.FIXTURE_URL in runner.PROMPT
    assert "Use only `browser_use`" in runner.PROMPT
    assert "Hello from browser_use" in runner.PROMPT
    assert "Received: Hello from browser_use" in runner.PROMPT
    assert "Close the `runner-smoke-test` browser session" in runner.PROMPT
    assert "unavailable, invalid, or broken" in runner.PROMPT
