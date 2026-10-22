"""Unit tests for the process-lifetime event loop helper."""

from __future__ import annotations

import asyncio

from kimix.utils.event_loop import (
    close_process_event_loop,
    get_process_event_loop,
    run_on_event_loop,
)


def test_get_process_event_loop_is_stable() -> None:
    loop = get_process_event_loop()
    assert loop is get_process_event_loop()
    assert asyncio.get_event_loop() is loop  # legacy call sites see the live loop
    assert not loop.is_closed()


async def _current_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


def test_run_on_event_loop_reuses_one_loop() -> None:
    first = run_on_event_loop(_current_loop())
    second = run_on_event_loop(_current_loop())
    assert first is second
    assert not first.is_closed()


def test_run_on_event_loop_returns_result_and_keeps_loop_bound_state() -> None:
    async def _make_waiter() -> asyncio.Future[str]:
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda _f: None)
        return future

    async def _resolve(future: asyncio.Future[str], value: str) -> str:
        future.set_result(value)
        return await future

    waiter = run_on_event_loop(_make_waiter())
    # The waiter was created on the process loop; resolving it from a *later*
    # conversation on the same loop must not raise "Event loop is closed".
    assert run_on_event_loop(_resolve(waiter, "done")) == "done"


def test_close_process_event_loop_allows_a_fresh_loop() -> None:
    loop = get_process_event_loop()
    close_process_event_loop()
    assert loop.is_closed()
    replacement = get_process_event_loop()
    assert replacement is not loop
    assert not replacement.is_closed()


def test_run_on_event_loop_from_inside_a_loop_falls_back() -> None:
    async def _outer() -> int:
        # Called while a loop is already running: blocking on the process loop
        # would deadlock, so the helper uses its worker-thread fallback.
        return run_on_event_loop(_inner())

    async def _inner() -> int:
        return 42

    assert asyncio.run(_outer()) == 42
