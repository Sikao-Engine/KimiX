"""Benchmark the ``broadcast`` bus hot paths (storage + render).

The bus is on two hot paths of a live session:

* the tool's ``append`` (once per ``broadcast`` tool call), and
* the soul poller's tick -- ``read_since`` + ``format_broadcast_batch`` -- which
  runs every three seconds for the whole life of every turn, so its *idle* cost
  matters more than its throughput.

Run before/after an optimization with the same settings::

    uv run python tools/bench_broadcast.py --rows 20000 --repeat 5
    uv run python tools/bench_broadcast.py --rows 20000 --repeat 5 --json .kimix_cache/broadcast-after.json
"""

from __future__ import annotations

import argparse
import statistics
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson
from kimi_cli.tools.broadcast.bus import (
    BroadcastBus,
    BroadcastMessage,
    now_ms,
)
from kimi_cli.tools.broadcast.format import format_broadcast_batch
from kimi_cli.utils.tokens import count_tokens

DEFAULT_MESSAGE = "peer update: the migration script is mine, do not touch db/schema.py " * 2


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


def measure(func: Callable[[], Any], *, repeat: int) -> dict[str, float | int]:
    """Run *func* *repeat* times and summarize its wall-clock duration."""
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        func()
        samples.append(time.perf_counter() - started)
    return summarize(samples)


def bench_storage(work_dir: Path, *, rows: int, repeat: int) -> dict[str, Any]:
    """Benchmark append / read / prune against a table of *rows* messages."""
    bus = BroadcastBus(work_dir=work_dir)
    results: dict[str, Any] = {}
    try:
        # --- fill (also the append benchmark: fresh keys, hot WAL) ---
        fill_started = time.perf_counter()
        for index in range(rows):
            bus.append(f"{DEFAULT_MESSAGE} #{index}")
        fill_elapsed = time.perf_counter() - fill_started
        results["append_fill"] = {
            "messages": rows,
            "total_s": round(fill_elapsed, 3),
            "per_message_ms": round(fill_elapsed / rows * 1000, 4),
        }

        # --- steady-state append ---
        results["append"] = measure(lambda: bus.append(DEFAULT_MESSAGE), repeat=repeat)

        latest = bus.latest_time() or now_ms()
        # Idle tick: the poller's common case (nothing new since the cursor).
        results["read_idle"] = measure(lambda: bus.read_since(latest + 1), repeat=repeat)
        # A single new message (the usual "a peer just published" case).
        results["read_one"] = measure(lambda: bus.read_since(latest), repeat=repeat)
        # A full batch (hit READ_LIMIT rows).
        results["read_batch"] = measure(lambda: bus.read_since(latest - 10_000, limit=256), repeat=repeat)
        # Pathological: every row matches, LIMIT keeps the scan bounded.
        results["read_all_limited"] = measure(
            lambda: bus.read_since(0, limit=256), repeat=repeat
        )
        results["prune_noop"] = measure(lambda: bus.prune(), repeat=repeat)
        results["count"] = measure(bus.count, repeat=repeat)
        results["rows"] = bus.count()
        results["db_bytes"] = bus.path.stat().st_size if bus.path.exists() else 0
        wal = bus.path.with_name(bus.path.name + "-wal")
        results["wal_bytes"] = wal.stat().st_size if wal.exists() else 0
    finally:
        bus.close()
    return results


def bench_render(*, repeat: int) -> dict[str, Any]:
    """Benchmark the steer-block renderer (soul side, per poll with new rows)."""
    results: dict[str, Any] = {}
    for count in (1, 5, 25, 256):
        batch = [
            BroadcastMessage(time=1_700_000_000_000 + index * 1000, content=DEFAULT_MESSAGE)
            for index in range(count)
        ]
        results[f"render_{count}"] = measure(
            lambda batch=batch: format_broadcast_batch(
                batch, token_budget=8000, count_tokens=count_tokens
            ),
            repeat=repeat,
        )

    huge = [BroadcastMessage(time=1_700_000_000_000, content="Z" * 32_000)]
    results["render_truncate_32k_chars"] = measure(
        lambda: format_broadcast_batch(huge, token_budget=2_000, count_tokens=count_tokens),
        repeat=repeat,
    )
    many_huge = [
        BroadcastMessage(time=1_700_000_000_000 + index, content="Z" * 32_000)
        for index in range(25)
    ]
    results["render_truncate_batch_25x32k"] = measure(
        lambda: format_broadcast_batch(
            many_huge, token_budget=2_000, count_tokens=count_tokens
        ),
        repeat=repeat,
    )
    return results


def bench_concurrent_appends(work_dir: Path, *, workers: int, per_worker: int) -> dict[str, Any]:
    """Benchmark several *connections* appending at once (LOCK contention)."""
    buses = [BroadcastBus(work_dir=work_dir) for _ in range(workers)]
    started = time.perf_counter()
    try:
        for index in range(per_worker):
            for bus in buses:
                bus.append(f"{DEFAULT_MESSAGE} c{index}")
    finally:
        elapsed = time.perf_counter() - started
        for bus in buses:
            bus.close()
    total = workers * per_worker
    return {
        "connections": workers,
        "messages": total,
        "total_s": round(elapsed, 3),
        "per_message_ms": round(elapsed / total * 1000, 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20_000, help="rows pre-filled in the bus")
    parser.add_argument("--repeat", type=int, default=7, help="samples per measurement")
    parser.add_argument(
        "--workers", type=int, default=4, help="connections used by the contention benchmark"
    )
    parser.add_argument("--per-worker", type=int, default=250, help="appends per connection")
    parser.add_argument("--json", type=Path, default=None, help="write raw results as JSON")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work_dir = Path(tmp) / "work"
        results: dict[str, Any] = {
            "generated_at_ms": now_ms(),
            "storage": bench_storage(work_dir, rows=args.rows, repeat=args.repeat),
            "render": bench_render(repeat=args.repeat),
            "contention": bench_concurrent_appends(
                Path(tmp) / "work-contention",
                workers=args.workers,
                per_worker=args.per_worker,
            ),
        }

    payload = orjson.dumps(results, option=orjson.OPT_INDENT_2).decode()
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_bytes(orjson.dumps(results, option=orjson.OPT_INDENT_2))
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
