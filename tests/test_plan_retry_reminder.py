"""Regression tests for the plan-session retry reminder contract.

Background: ``prompt_plan_async`` (and both server session managers) verified
the plan file on disk with ``plan_file.exists() and size > 0``. On failure the
loop originally re-prompted the planner with an inline machine-built message
("The plan file was not generated. Please generate the plan and save it using
the write_plan tool.") delivered as a user-role turn. Planners read it as the
user confirming/reviewing their work and answered a question nobody asked
(hallucinated confirmation turns, e.g. "The user is asking me to continue").

An intermediate fix kept the injection but labeled it ``[system check]`` and
stated "it is NOT a message from the user" — the label treated the symptom;
the nudge still arrived as a user-role turn and models kept misattributing it.

The current contract deletes the synthetic nudge entirely: the retry loop
re-sends the ORIGINAL requirement reminder (text derived from real user
input), which cannot be mistaken for a new human turn. These tests cover:

- prompt_plan_async retry: every attempt receives the original reminder,
  byte-identical across attempts (no machine-phrased nudge, no label)
- prompt_plan_async exhausted retries: graceful "Plan generation failed",
  no raise
- drift guard: the old ambiguous literal, the ``[system check]`` label, and
  the deleted ``build_plan_retry_reminder``/``PLAN_RETRY_CHECK_PREFIX``
  symbols appear nowhere in the source tree
"""

import importlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import kimix.base as base

# NB: ``import kimix.utils.prompt as prompt_mod`` would bind the *function*
# ``prompt`` re-exported by kimix.utils.__init__ (it shadows the submodule
# attribute); importlib reliably returns the module object.
prompt_mod = importlib.import_module("kimix.utils.prompt")
from kimix.utils.prompt import prompt_plan_async

REQUIREMENT = "  Add --max-distance and --ao-gamma to the AO baker.  "

# The full old second sentence; must not be re-inlined at any call site.
OLD_NUDGE_SENTENCE = "Please generate the plan and save it using the write_plan tool."

# Symbols deleted from the source tree; must not be referenced anywhere.
DELETED_SYMBOLS = ["build_plan_retry_reminder", "PLAN_RETRY_CHECK_PREFIX"]

# The retired provenance label of the synthetic nudge.
RETIRED_LABEL = "[system check]"


class _FakePlannerSession:
    """Minimal stand-in for the planner Session used by prompt_plan_async."""

    def __init__(self, prompts: list[str], write_on_attempt: int | None, plan_file: Path):
        self.prompts = prompts
        self._write_on_attempt = write_on_attempt
        self._plan_file = plan_file
        self._calls = 0
        self._cli = None  # disables the read-only runtime lock branch
        self._cancel_event = None

    def get_custom_data(self) -> dict:
        return {}

    def cancel(self) -> None:
        pass

    async def prompt(self, text: str, **kwargs):
        self._calls += 1
        self.prompts.append(text)
        if self._write_on_attempt is not None and self._calls == self._write_on_attempt:
            self._plan_file.write_text("# Plan\n\nstep one\n", encoding="utf-8")
        return
        yield  # pragma: no cover — makes this an async generator, yields nothing


@pytest.fixture()
def patched(monkeypatch, tmp_path):
    """Patch every collaborator of prompt_plan_async; return bookkeeping handles."""
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

    close_mock = AsyncMock()
    monkeypatch.setattr(prompt_mod, "close_session_async", close_mock)

    # Execution phase must not create real sessions or prompt real agents.
    create_default_mock = AsyncMock(return_value=MagicMock())
    prompt_async_mock = AsyncMock()
    monkeypatch.setattr(prompt_mod, "_create_default_session_async", create_default_mock)
    monkeypatch.setattr(prompt_mod, "prompt_async", prompt_async_mock)

    return SimpleHandles(
        stream=stream,
        close_mock=close_mock,
        create_default_mock=create_default_mock,
        prompt_async_mock=prompt_async_mock,
    )


class SimpleHandles:
    def __init__(self, stream, close_mock, create_default_mock=None, prompt_async_mock=None):
        self.stream = stream
        self.close_mock = close_mock
        self.create_default_mock = create_default_mock
        self.prompt_async_mock = prompt_async_mock

    def printed_texts(self) -> list[str]:
        texts = []
        for call in self.stream.colorful_print_word.call_args_list:
            if call.args:
                texts.append(str(call.args[0]))
        return texts


async def _drive(monkeypatch, patched, prompts: list[str], write_on_attempt: int | None, plan_file: Path, inputs: list[str]):
    fake = _FakePlannerSession(prompts, write_on_attempt, plan_file)
    monkeypatch.setattr(prompt_mod, "_create_session_async", AsyncMock(return_value=fake))
    it = iter(inputs)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    await prompt_plan_async(REQUIREMENT, plan_file)
    return fake


def _assert_original_reminder(text: str) -> None:
    """The reminder must be the original requirement text, with no machine nudge."""
    assert RETIRED_LABEL not in text
    assert "NOT a message from the user" not in text
    assert OLD_NUDGE_SENTENCE not in text
    assert "plan file was not found" not in text.lower()
    assert "Requirement:" in text
    assert "Add --max-distance and --ao-gamma to the AO baker." in text


class TestPromptPlanAsyncRetry:
    async def test_retry_resends_original_reminder_verbatim(self, monkeypatch, patched, tmp_path):
        """Attempt 1 never writes the file; attempt 2 writes it.

        The second planner prompt must be the ORIGINAL requirement reminder,
        byte-identical to the first — no synthetic nudge, no label.
        """
        prompts: list[str] = []
        plan_file = tmp_path / "plan.md"
        await _drive(
            monkeypatch, patched, prompts,
            write_on_attempt=2, plan_file=plan_file,
            inputs=["y"],  # approve the plan in the review loop
        )

        assert len(prompts) == 2, prompts
        _assert_original_reminder(prompts[0])
        _assert_original_reminder(prompts[1])
        # Retry carries the exact original reminder, not a reworded variant.
        assert prompts[1] == prompts[0]

        # The run must not have swallowed any exception on the way.
        assert not any("prompt_plan failed" in t for t in patched.printed_texts())
        # Approved plan flows to implementation; planner session closed.
        patched.close_mock.assert_awaited_once()

    async def test_all_retries_original_reminder_when_plan_never_written(self, monkeypatch, patched, tmp_path):
        """File never appears: 3 attempts, every prompt is the original reminder."""
        prompts: list[str] = []
        plan_file = tmp_path / "plan.md"
        await _drive(
            monkeypatch, patched, prompts,
            write_on_attempt=None, plan_file=plan_file,
            inputs=[],  # review loop never reached
        )

        assert len(prompts) == 3, prompts
        for p in prompts:
            _assert_original_reminder(p)
        assert prompts[1] == prompts[0]
        assert prompts[2] == prompts[0]
        assert any("Plan generation failed" in t for t in patched.printed_texts())
        assert not any("prompt_plan failed" in t for t in patched.printed_texts())
        patched.close_mock.assert_awaited_once()


class TestReviewLoopRestored:
    """The human review/revision loop must exist; the auto review prompt must not.

    Commit e903488 removed the wrong half of ``prompt_plan_async``: it dropped
    the interactive review/revision loop while keeping the machine-sent
    "review the plan" prompt. These tests pin the correct shape.
    """

    async def test_impl_prompt_sent_once_and_no_review_prompt(
        self, monkeypatch, patched, tmp_path
    ):
        prompts: list[str] = []
        plan_file = tmp_path / "plan.md"
        await _drive(
            monkeypatch, patched, prompts,
            write_on_attempt=1, plan_file=plan_file,
            inputs=["y"],  # approve immediately
        )

        # Planner was prompted exactly once (no revision turn).
        assert len(prompts) == 1, prompts

        # Only the implementation prompt is sent to the worker session.
        assert patched.create_default_mock.await_count == 1
        assert patched.prompt_async_mock.await_count == 1, "review prompt must be gone"
        sent = patched.prompt_async_mock.await_args.args[0]
        assert sent.startswith("Read this plan")
        assert "Review this plan" not in sent
        assert "Review the plan" not in sent
        patched.close_mock.assert_awaited_once()

    async def test_quit_in_review_loop_skips_implementation(
        self, monkeypatch, patched, tmp_path
    ):
        prompts: list[str] = []
        plan_file = tmp_path / "plan.md"
        await _drive(
            monkeypatch, patched, prompts,
            write_on_attempt=1, plan_file=plan_file,
            inputs=["n", "/quit"],  # not yet -> give up
        )

        assert len(prompts) == 1, prompts
        # No Worker session, no implementation prompt, no review prompt.
        patched.create_default_mock.assert_not_awaited()
        patched.prompt_async_mock.assert_not_awaited()
        # Planner session still closed via finally.
        patched.close_mock.assert_awaited_once()

    async def test_revision_feedback_resumes_planner_then_approves(
        self, monkeypatch, patched, tmp_path
    ):
        prompts: list[str] = []
        plan_file = tmp_path / "plan.md"
        await _drive(
            monkeypatch, patched, prompts,
            write_on_attempt=1, plan_file=plan_file,
            inputs=["n", "add a rollback step", "y"],
        )

        # Generation turn + one revision turn.
        assert len(prompts) == 2, prompts
        assert prompts[1].startswith("The user reviewed the plan")
        assert "add a rollback step" in prompts[1]

        # After approval the implementation prompt runs exactly once.
        assert patched.prompt_async_mock.await_count == 1
        sent = patched.prompt_async_mock.await_args.args[0]
        assert "Review this plan" not in sent


class TestCallSiteDriftGuard:
    """The retry contract must hold identically at all three plan call sites."""

    SITES = [
        "src/kimix/utils/prompt.py",
        "src/kimix/server/session_manager.py",
        "src/kimix/server/dummy_session_manager.py",
    ]

    def test_no_site_reinjects_a_synthetic_nudge(self):
        root = Path(__file__).resolve().parents[1]
        for rel in self.SITES:
            src = (root / rel).read_text(encoding="utf-8")
            assert OLD_NUDGE_SENTENCE not in src, f"{rel} re-inlines the ambiguous nudge"
            assert RETIRED_LABEL not in src, f"{rel} re-injects the labeled nudge"
            for symbol in DELETED_SYMBOLS:
                assert symbol not in src, f"{rel} still references deleted {symbol}"

    def test_deleted_symbols_absent_tree_wide(self):
        """build_plan_retry_reminder / PLAN_RETRY_CHECK_PREFIX have no callers left."""
        root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        for py in (root / "src").rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for symbol in DELETED_SYMBOLS:
                if symbol in text:
                    offenders.append(f"{py.relative_to(root)}: {symbol}")
        assert not offenders, "deleted symbols still referenced:\n" + "\n".join(offenders)

    def test_original_reminder_present_at_all_sites(self):
        """All three sites still seed the retry loop with the original reminder,
        so a retry re-sends real user-derived content (drift guard)."""
        root = Path(__file__).resolve().parents[1]
        needle = "read the following requirement carefully and generate a comprehensive plan."
        for rel in self.SITES:
            src = (root / rel).read_text(encoding="utf-8")
            assert needle in src, f"{rel} lost the original requirement reminder"
