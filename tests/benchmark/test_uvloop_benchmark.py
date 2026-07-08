"""Benchmark: uvloop event loop should work correctly."""

import asyncio


async def _run_async_ops() -> list[int]:
    """Run standard asyncio operations."""
    results: list[int] = []

    # Task creation and awaiting
    async def double(x: int) -> int:
        await asyncio.sleep(0.001)
        return x * 2

    tasks = [double(i) for i in range(10)]
    results = await asyncio.gather(*tasks)
    return results


def test_uvloop_event_loop() -> None:
    """Using uvloop should not break any asyncio operations."""
    try:
        import uvloop

        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        # uvloop not available on this platform (e.g. Windows)
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(_run_async_ops())
        assert results == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]
    finally:
        loop.close()
