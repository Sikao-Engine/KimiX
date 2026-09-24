"""Close hooks: a session's teardown must cascade to what it spawned.

``kimix.utils.session`` closes every session in the application (CLI exit, the
web server, the sub-agent store, the interpreter-shutdown hook), so it is the
place to notify session-scoped resources that the session is going away. The
``subagent`` tool uses it to delete the sub-agent sessions it spawned: an
anonymous sub-agent session (and its ``.kimix_cache/<id>`` directory) must not
outlive the parent conversation.
"""

from __future__ import annotations

from typing import Any

import pytest

import kimix.utils.session as session_mod


@pytest.fixture(autouse=True)
def _isolated_hooks():
    """Keep the global hook registry clean for every test."""
    saved = list(session_mod._session_close_hooks)
    session_mod._session_close_hooks.clear()
    yield
    session_mod._session_close_hooks.clear()
    session_mod._session_close_hooks.extend(saved)


class _FakeSession:
    """Minimal session: records close/clear calls."""

    def __init__(self) -> None:
        self.closed = 0
        self.cleared = 0

    async def close(self) -> None:
        self.closed += 1

    async def clear(self) -> None:
        self.cleared += 1


def test_register_session_close_hook_is_idempotent() -> None:
    def hook(session: Any) -> None:
        pass

    session_mod.register_session_close_hook(hook)
    session_mod.register_session_close_hook(hook)
    assert session_mod._session_close_hooks == [hook]


async def test_close_session_async_runs_hooks_before_closing() -> None:
    seen: list[Any] = []
    session = _FakeSession()
    session_mod.register_session_close_hook(seen.append)

    await session_mod.close_session_async(session)  # type: ignore[arg-type]

    assert seen == [session]
    assert session.closed == 1


async def test_async_close_hook_is_awaited() -> None:
    seen: list[Any] = []

    async def hook(session: Any) -> None:
        seen.append(session)

    session_mod.register_session_close_hook(hook)
    session = _FakeSession()

    await session_mod.close_session_async(session)  # type: ignore[arg-type]

    assert seen == [session]


async def test_failing_close_hook_does_not_break_the_close() -> None:
    def boom(session: Any) -> None:
        raise RuntimeError("hook boom")

    session_mod.register_session_close_hook(boom)
    session = _FakeSession()

    await session_mod.close_session_async(session)  # type: ignore[arg-type]

    assert session.closed == 1


def test_close_session_sync_runs_hooks() -> None:
    seen: list[Any] = []
    session_mod.register_session_close_hook(seen.append)
    session = _FakeSession()

    session_mod.close_session(session)  # type: ignore[arg-type]

    assert seen == [session]
    assert session.closed == 1


async def test_clear_session_async_runs_hooks_before_clearing() -> None:
    seen: list[Any] = []
    session_mod.register_session_close_hook(seen.append)
    session = _FakeSession()

    await session_mod.clear_session_async(session)  # type: ignore[arg-type]

    assert seen == [session]
    assert session.cleared == 1


async def test_clear_context_async_also_runs_hooks() -> None:
    """``clear_context_async`` is the older name of the same operation."""
    seen: list[Any] = []
    session_mod.register_session_close_hook(seen.append)
    session = _FakeSession()

    await session_mod.clear_context_async(session)  # type: ignore[arg-type]

    assert seen == [session]
    assert session.cleared == 1
