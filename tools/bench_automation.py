"""Live Windows benchmark for ``computer_use`` and ``browser_use`` hot paths.

The benchmark owns its Notepad process and incognito Chromium browser.  It never
injects foreground input.  Run before and after optimization with the same
settings, for example::

    uv run python tools/bench_automation.py --repeat 7 --output .kimix_cache/automation-before.json
    uv run python tools/bench_automation.py --repeat 7 --output .kimix_cache/automation-after.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import platform
import signal
import statistics
import subprocess
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal, TypeVar
from urllib.parse import quote

import orjson
from kimi_cli.tools.browser_use.backend import BrowserConfig, BrowserManager
from kimi_cli.tools.computer_use.windows import WindowsComputerBackend

T = TypeVar("T")
Suite = Literal["all", "browser", "computer"]


def summarize(samples: list[float]) -> dict[str, float | int]:
    """Return stable wall-clock statistics in milliseconds."""
    if not samples:
        raise ValueError("At least one sample is required.")
    ordered = sorted(samples)
    p95_index = max(0, round(0.95 * (len(ordered) - 1)))
    return {
        "iterations": len(samples),
        "median_ms": round(statistics.median(samples) * 1000, 3),
        "mean_ms": round(statistics.fmean(samples) * 1000, 3),
        "min_ms": round(ordered[0] * 1000, 3),
        "p95_ms": round(ordered[p95_index] * 1000, 3),
        "max_ms": round(ordered[-1] * 1000, 3),
    }


async def measure(
    operation: Callable[[], Awaitable[T]], *, repeat: int, warmup: int = 2
) -> tuple[dict[str, float | int], T]:
    """Measure steady-state sequential calls and return the last real result."""
    result: T | None = None
    for _ in range(warmup):
        result = await operation()
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        result = await operation()
        samples.append(time.perf_counter() - started)
    assert result is not None
    return summarize(samples), result


async def benchmark_browser(repeat: int, element_count: int) -> dict[str, Any]:
    manager = BrowserManager(BrowserConfig(max_elements=element_count))
    try:
        state = await manager.get_session("benchmark")
        controls = "".join(
            f"<button id='button-{index}' name='button-{index}'>Action {index}</button>"
            f"<input aria-label='Field {index}' value='value {index}'>"
            for index in range(element_count)
        )
        html = (
            "<title>Automation benchmark</title><main>"
            + controls
            + "<div class='record'>record text</div>" * element_count
            + "</main>"
        )
        await manager.navigate(
            state, "data:text/html," + quote(html), wait_until="domcontentloaded"
        )

        snapshot_stats, snapshot = await measure(lambda: manager.snapshot(state), repeat=repeat)
        assert snapshot["element_count"] == element_count
        extract_stats, extracted = await measure(
            lambda: manager.extract(state, ".record", None, True, element_count),
            repeat=repeat,
        )
        assert len(extracted["values"]) == element_count
        screenshot_stats, screenshot = await measure(
            lambda: manager.screenshot(state, False), repeat=repeat
        )
        assert screenshot[1]
        return {
            "element_count": element_count,
            "snapshot": snapshot_stats,
            "extract": extract_stats,
            "screenshot": screenshot_stats,
        }
    finally:
        await manager.close_all()


async def benchmark_computer(repeat: int) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("The computer_use live benchmark requires Windows.")
    backend = WindowsComputerBackend(max_elements=200)
    actual_pid: int | None = None
    with tempfile.TemporaryDirectory(prefix="kimi-automation-bench-") as directory:
        fixture = Path(directory) / f"owned-{uuid.uuid4().hex}.txt"
        fixture.write_text("benchmark", encoding="utf-8")
        process = subprocess.Popen(["notepad.exe", str(fixture)])
        try:
            for _ in range(50):
                try:
                    initial = await asyncio.to_thread(
                        backend.capture, "ax", fixture.name, None, None
                    )
                    actual_pid = initial.pid
                    break
                except Exception:
                    await asyncio.sleep(0.2)
            if actual_pid is None:
                raise RuntimeError("Owned Notepad window did not become available.")

            ax_stats, ax = await measure(
                lambda: asyncio.to_thread(backend.capture, "ax", fixture.name, None, None),
                repeat=repeat,
            )
            vision_stats, vision = await measure(
                lambda: asyncio.to_thread(backend.capture, "vision", fixture.name, None, None),
                repeat=repeat,
            )
            som_stats, som = await measure(
                lambda: asyncio.to_thread(backend.capture, "som", fixture.name, None, None),
                repeat=repeat,
            )
            assert ax.elements and vision.png_data and som.png_data
            return {
                "capture_ax": ax_stats,
                "capture_vision": vision_stats,
                "capture_som": som_stats,
            }
        finally:
            if (
                actual_pid is not None
                and backend._process_name(actual_pid).lower() == "notepad.exe"
            ):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(actual_pid, signal.SIGTERM)
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)


async def run(suite: Suite, repeat: int, element_count: int) -> dict[str, Any]:
    results: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "repeat": repeat,
        "suites": {},
    }
    if suite in {"all", "browser"}:
        results["suites"]["browser"] = await benchmark_browser(repeat, element_count)
    if suite in {"all", "computer"}:
        results["suites"]["computer"] = await benchmark_computer(repeat)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=("all", "browser", "computer"), default="all")
    parser.add_argument("--repeat", type=int, default=7)
    parser.add_argument("--elements", type=int, default=80)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeat < 1 or not 10 <= args.elements <= 500:
        parser.error("--repeat must be positive and --elements must be in 10..500")
    results = asyncio.run(run(args.suite, args.repeat, args.elements))
    encoded = orjson.dumps(results, option=orjson.OPT_INDENT_2)
    print(encoded.decode("utf-8"))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(encoded + b"\n")


if __name__ == "__main__":
    main()
