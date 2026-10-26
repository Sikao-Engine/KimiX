from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from typing import Any

import pytest
from kosong.chat_provider import APIStatusError

prompt_mod = importlib.import_module("kimix.utils.prompt")


@dataclass
class FakeStatus:
    context_usage: float = 0.0
    context_tokens: int = 0


class FakeSession:
    def __init__(self, raises: Exception | None = None) -> None:
        self.status = FakeStatus()
        self._cancel_event = None
        self.cancelled = False
        self.raises = raises

    async def prompt(self, prompt_str: str, *, merge_wire_messages: bool = False) -> Any:
        if self.raises is not None:
            raise self.raises
        # Force this into an async generator that yields nothing.
        if False:
            yield None

    def cancel(self) -> None:
        self.cancelled = True


def _suppress_output(monkeypatch: Any) -> list[str]:
    printed: list[str] = []

    def _capture(text: str, *args: Any, **kwargs: Any) -> None:
        printed.append(text)

    monkeypatch.setattr(prompt_mod.base._stream, "colorful_print_word", _capture)
    monkeypatch.setattr(prompt_mod.base._stream, "print_word", lambda *args, **kwargs: None)
    monkeypatch.setattr(prompt_mod, "_print_usage", lambda *args, **kwargs: None)
    return printed


@pytest.mark.asyncio
async def test_run_single_prompt_propagates_api_status_error(monkeypatch: Any) -> None:
    """A 429 from the low-level layer must not trigger the old high-level backoff."""
    printed = _suppress_output(monkeypatch)
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    session = FakeSession(raises=APIStatusError(429, "rate limited"))

    with pytest.raises(APIStatusError, match="rate limited"):
        await prompt_mod._run_single_prompt(
            session, "hi", output_function=None, cancel_callable=None,
            merge_wire_messages=False, info_print=False,
        )

    # No high-level "Rate limited" message or status-code string matching.
    assert not any("Rate limited" in text for text in printed)
    # 429 is handled by the low-level layer; the high-level utility must not sleep.
    assert sleeps == []
    assert session.cancelled


@pytest.mark.asyncio
async def test_run_single_prompt_propagates_non_retryable_400(monkeypatch: Any) -> None:
    """A 400 Bad Request must not be retried at the high level."""
    printed = _suppress_output(monkeypatch)
    sleeps: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    session = FakeSession(raises=APIStatusError(400, "bad request"))

    with pytest.raises(APIStatusError, match="bad request"):
        await prompt_mod._run_single_prompt(
            session, "hi", output_function=None, cancel_callable=None,
            merge_wire_messages=False, info_print=False,
        )

    assert not any("Rate limited" in text for text in printed)
    # 400 is not retried at the high level.
    assert sleeps == []
    assert session.cancelled


# ---------------------------------------------------------------------------
# A closed session is a cancellation, not a "gracefully handled" prompt error
# ---------------------------------------------------------------------------


class _ClosedSession(FakeSession):
    """A session that was torn down under the prompt (interrupt/cascade)."""

    def __init__(self) -> None:
        super().__init__(raises=None)
        self._closed = True

    async def prompt(self, prompt_str: str, *, merge_wire_messages: bool = False) -> Any:
        # The SDK raises this before it even starts streaming.
        from kimi_agent_sdk import SessionStateError

        raise SessionStateError("Session is closed")
        if False:  # pragma: no cover - keeps this an async generator
            yield None


@pytest.mark.asyncio
async def test_prompt_async_does_not_report_a_closed_session_as_an_error(
    monkeypatch: Any,
) -> None:
    """No "Prompt error (gracefully handled)" / "prompt failed." for a close.

    Those two red lines describe a prompt that *failed*, but a session closed
    underneath the prompt (``interrupt_agent``, a parent-close cascade, CLI
    shutdown) is simply a cancelled conversation — printing them made an
    ordinary teardown look like an agent failure.
    """
    printed = _suppress_output(monkeypatch)
    session = _ClosedSession()

    # It must not raise either: ``close_session_after_prompt`` teardown and the
    # caller's own handling still run.
    await prompt_mod.prompt_async(
        prompt_str="hi",
        session=session,
        info_print=False,
    )

    joined = "\n".join(str(text) for text in printed)
    assert "Prompt error" not in joined
    assert "prompt failed." not in joined
    assert "Session is closed" not in joined


@pytest.mark.asyncio
async def test_prompt_async_still_reports_a_live_session_failure(
    monkeypatch: Any,
) -> None:
    """Control probe: a failure on a *live* session is still reported."""
    printed = _suppress_output(monkeypatch)
    session = FakeSession(raises=RuntimeError("provider exploded"))
    session._closed = False

    await prompt_mod.prompt_async(
        prompt_str="hi",
        session=session,
        info_print=False,
    )

    joined = "\n".join(str(text) for text in printed)
    assert "Prompt error (gracefully handled): provider exploded" in joined
    assert "prompt failed." in joined
