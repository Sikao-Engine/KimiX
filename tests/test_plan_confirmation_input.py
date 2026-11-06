"""Regression tests: the ``/plan`` confirmation must use the CLI input channel.

Background: ``prompt_plan_async`` asked its review questions with a bare
``asyncio.to_thread(input, "...")``.  The interactive CLI *always* has its own
``kimix-local-input`` daemon thread blocked in ``input()``
(``CLIIPCServer.read_input()`` starts it and it never ends), and that thread
owns the terminal: it consumes every typed line and queues it for the CLI loop.
A second, independent ``input()`` therefore

* wrote its question through the raw stdio prompt path instead of the CLI's
  stream (so the question was not part of the CLI output), and
* never received an answer — the line the user typed had already been handed
  to the CLI's reader thread.  The review loop waited forever right after
  ``Plan generated: ...`` and the CLI looked frozen.

The review loop now asks through ``_ask_user`` → ``kimix.cli_impl.utils._input``
(one reader, terminal *and* ``kimix connect`` input) and prints the question
through the same flushed stream as every other CLI prompt.  These tests pin
that contract:

* the question is printed through the CLI stream, flushed before blocking;
* the answer comes from the CLI input channel, never from ``builtins.input``;
* the revision round uses the same channel and feeds the planner;
* EOF / Ctrl+C abort the flow instead of looping on a dead channel;
* a busy window is lifted (and restored) around the read so the answer can
  arrive at all;
* without a CLI server the historical ``input()`` fallback is preserved;
* drift guard: no bare ``input()`` confirmation survives in the source.
"""

import importlib
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import kimix.base as base
from kimix.cli_impl import utils as cli_utils

# NB: ``import kimix.utils.prompt as prompt_mod`` binds the *function*
# ``prompt`` re-exported by kimix.utils.__init__; importlib returns the module.
prompt_mod = importlib.import_module("kimix.utils.prompt")

REQUIREMENT = "Add --max-distance and --ao-gamma to the AO baker."

QUESTION = "Do you want to implement the plan? (y/n): "
FEEDBACK_QUESTION = "Please describe the changes you want (/quit to give up): "


class _FakePlannerSession:
    """Minimal stand-in for the planner Session used by prompt_plan_async."""

    def __init__(self, plan_file: Path) -> None:
        self.prompts: list[str] = []
        self.plan_file = plan_file
        self._cli = None  # disables the read-only runtime lock branch
        self._cancel_event = None

    def get_custom_data(self) -> dict:
        return {}

    def cancel(self) -> None:
        pass

    async def prompt(self, text: str, **kwargs):
        self.prompts.append(text)
        self.plan_file.write_text("# Plan\n\nstep one\n", encoding="utf-8")
        return
        yield  # pragma: no cover — makes this an async generator, yields nothing


class _FakeServer:
    """Stand-in for ``CLIIPCServer`` fed with scripted answers."""

    def __init__(self, answers: list[str | BaseException]) -> None:
        self.answers = list(answers)
        self.reads = 0
        self.busy = False
        self.busy_calls: list[bool] = []

    def read_input(self) -> str:
        self.reads += 1
        if not self.answers:
            raise EOFError("no scripted answer")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.busy_calls.append(busy)


class _Handles:
    def __init__(self, stream, planner, prompt_async_mock) -> None:
        self.stream = stream
        self.planner = planner
        self.prompt_async = prompt_async_mock

    def printed_texts(self) -> list[str]:
        return [
            str(call.args[0]) for call in self.stream.colorful_print_word.call_args_list if call.args
        ]

    def question_calls(self) -> list:
        return [
            call
            for call in self.stream.colorful_print_word.call_args_list
            if call.args and "Do you want to implement" in str(call.args[0])
        ]

    def impl_prompts(self) -> list[str]:
        return [str(call.args[0]) for call in self.prompt_async.call_args_list]


@pytest.fixture()
def patched(monkeypatch, tmp_path):
    """Patch every non-input collaborator of prompt_plan_async."""
    stream = MagicMock()
    monkeypatch.setattr(base, "_stream", stream)
    monkeypatch.setattr(base, "get_default_sub_provider", lambda role: None)
    monkeypatch.setattr(base, "_default_provider", {"type": "test"})

    from kimix.utils import _globals as session_globals

    monkeypatch.setattr(session_globals, "_default_session", None, raising=False)

    # _open_plan_file side effects must never touch the real system.
    import os as _os

    monkeypatch.setattr(_os, "startfile", lambda *a: None, raising=False)
    monkeypatch.setattr(prompt_mod.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(prompt_mod, "close_session_async", AsyncMock())

    planner = _FakePlannerSession(tmp_path / "plan.md")
    monkeypatch.setattr(
        prompt_mod, "_create_session_async", AsyncMock(return_value=planner)
    )
    prompt_async_mock = AsyncMock()
    monkeypatch.setattr(prompt_mod, "_create_default_session_async", AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(prompt_mod, "prompt_async", prompt_async_mock)
    # No CLI server unless a test installs one: the read must fall back to input().
    monkeypatch.setattr(cli_utils, "_active_ipc_server", None)

    return _Handles(stream, planner, prompt_async_mock)


def _install_server(monkeypatch, answers: list[str | BaseException], *, busy: bool = False) -> _FakeServer:
    server = _FakeServer(answers)
    server.busy = busy
    monkeypatch.setattr(cli_utils, "_active_ipc_server", server)
    return server


async def _run(plan_file: Path) -> None:
    await prompt_mod.prompt_plan_async(REQUIREMENT, plan_file)


class TestConfirmationUsesCliInput:
    async def test_question_is_printed_and_answer_read_from_cli_channel(
        self, monkeypatch, patched, tmp_path
    ):
        server = _install_server(monkeypatch, ["y"])
        monkeypatch.setattr(
            "builtins.input", lambda *a, **k: pytest.fail("bare input() used by the plan flow")
        )

        await _run(tmp_path / "plan.md")

        assert server.reads == 1  # "y" came from the CLI channel
        calls = patched.question_calls()
        assert calls, patched.printed_texts()
        # Flushed before the flow blocks on the answer, and a fresh line.
        assert calls[0].kwargs.get("flush") is True
        assert calls[0].kwargs.get("require_new_line") is True
        assert patched.impl_prompts(), "approved plan must reach the implementation phase"

    async def test_revision_round_uses_the_same_channel(self, monkeypatch, patched, tmp_path):
        server = _install_server(monkeypatch, ["n", "add a rollback step", "y"])
        monkeypatch.setattr(
            "builtins.input", lambda *a, **k: pytest.fail("bare input() used by the plan flow")
        )

        await _run(tmp_path / "plan.md")

        assert server.reads == 3
        # Generation + one revision turn.
        assert len(patched.planner.prompts) == 2
        assert "add a rollback step" in patched.planner.prompts[1]
        assert patched.impl_prompts(), "the revision was approved and must be implemented"
        assert any("Revising plan" in text for text in patched.printed_texts())

    async def test_eof_aborts_without_implementing(self, monkeypatch, patched, tmp_path):
        """A dead input channel must end the flow, not spin on empty answers."""
        server = _install_server(monkeypatch, [EOFError("closed")])

        await _run(tmp_path / "plan.md")

        assert server.reads == 1
        assert patched.impl_prompts() == []
        assert any("No answer received" in text for text in patched.printed_texts())

    async def test_keyboard_interrupt_aborts_without_implementing(
        self, monkeypatch, patched, tmp_path
    ):
        """Ctrl+C at the question ends plan mode instead of crashing the CLI."""
        server = _install_server(monkeypatch, [KeyboardInterrupt()])

        await _run(tmp_path / "plan.md")

        assert server.reads == 1
        assert patched.impl_prompts() == []
        assert any("No answer received" in text for text in patched.printed_texts())

    async def test_busy_window_is_released_around_the_read(
        self, monkeypatch, patched, tmp_path
    ):
        """A busy window pauses its reader; the confirmation must un-pause it."""
        server = _install_server(monkeypatch, ["y"], busy=True)

        await _run(tmp_path / "plan.md")

        assert server.busy_calls == [False, True]
        assert server.busy is True  # restored for the caller
        assert patched.impl_prompts()

    async def test_without_cli_server_the_input_fallback_is_preserved(
        self, monkeypatch, patched, tmp_path
    ):
        """Embedded use (no CLI server) keeps the historical ``input()`` path."""
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        await _run(tmp_path / "plan.md")

        assert patched.impl_prompts()


class TestNoBareInputConfirmation:
    """Drift guard: the review loop must not go back to a bare ``input()``."""

    def test_prompt_module_has_no_raw_input_confirmation(self) -> None:
        source = Path(prompt_mod.__file__).read_text(encoding="utf-8")
        assert not re.search(r"to_thread\(\s*input\b", source)
        assert "Do you want to implement the plan?" in source  # asked through _ask_user
        # The answer comes from the CLI's single keyboard-input funnel.
        assert "from kimix.cli_impl.utils import _input" in source
