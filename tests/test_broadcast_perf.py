"""Performance smoke tests for the ``broadcast`` bus hot paths.

The poller runs every three seconds for the whole life of a turn, so its *idle*
cost has to stay in the microsecond range even when the shared table holds tens
of thousands of messages; the tool's ``append`` is on the interactive path.

Thresholds are deliberately loose (>= 10x the measured median on a developer
Windows box) so the probes catch an accidental table scan / re-open per call,
not machine noise.  Deselect with ``-m "not benchmark"``.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from kimi_cli.tools.broadcast.bus import READ_LIMIT, BroadcastBus, BroadcastMessage
from kimi_cli.tools.broadcast.format import format_broadcast_batch
from kimi_cli.utils.tokens import count_tokens

pytestmark = pytest.mark.benchmark

MESSAGE = "peer update: the migration script is mine, do not touch db/schema.py " * 2


def _median_ms(func: Callable[[], Any], *, repeat: int = 9) -> float:
    samples: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        func()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples) * 1000


@pytest.fixture(scope="module")
def populated_bus(tmp_path_factory: pytest.TempPathFactory):
    """A bus with 20k messages (a realistic long-lived work directory)."""
    work_dir = tmp_path_factory.mktemp("broadcast-perf")
    bus = BroadcastBus(work_dir=work_dir)
    for index in range(20_000):
        bus.append(f"{MESSAGE} #{index}")
    try:
        yield bus
    finally:
        bus.close()


def test_append_stays_cheap_on_a_large_table(populated_bus: BroadcastBus) -> None:
    assert _median_ms(lambda: populated_bus.append(MESSAGE)) < 10.0


def test_idle_poll_tick_is_microsecond_scale(populated_bus: BroadcastBus) -> None:
    """The common case: nothing new since the cursor."""
    cursor = (populated_bus.latest_time() or 0) + 1
    assert _median_ms(lambda: populated_bus.read_since(cursor)) < 5.0


def test_bounded_read_of_new_messages_is_cheap(populated_bus: BroadcastBus) -> None:
    cursor = (populated_bus.latest_time() or 0) - 10_000
    rows = populated_bus.read_since(cursor, limit=READ_LIMIT)
    assert len(rows) == READ_LIMIT
    assert _median_ms(lambda: populated_bus.read_since(cursor, limit=READ_LIMIT)) < 20.0


def test_read_cost_does_not_scale_with_table_size(tmp_path: Path) -> None:
    """``time >= ?`` must stay a range search, not a growing scan."""
    small = BroadcastBus(work_dir=tmp_path / "small")
    big = BroadcastBus(work_dir=tmp_path / "big")
    try:
        small.append("only row")
        for index in range(5_000):
            big.append(f"{MESSAGE} #{index}")
        small_cursor = (small.latest_time() or 0) - 5
        big_cursor = (big.latest_time() or 0) - 5
        small_ms = _median_ms(lambda: small.read_since(small_cursor), repeat=15)
        big_ms = _median_ms(lambda: big.read_since(big_cursor), repeat=15)
        # 25x more rows must not mean a proportional read cost.
        assert big_ms < 5.0, (small_ms, big_ms)
        assert big_ms < small_ms * 20 + 1.0, (small_ms, big_ms)
    finally:
        small.close()
        big.close()


def test_prune_without_stale_rows_stays_read_only(populated_bus: BroadcastBus) -> None:
    assert _median_ms(populated_bus.prune) < 5.0


def test_storage_failure_path_is_not_pathological(tmp_path: Path) -> None:
    """A missing directory must fail fast, not retry for seconds."""
    bus = BroadcastBus(work_dir=tmp_path / "work")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / ".kimix_cache").write_text("not a directory")

    def _attempt() -> None:
        with pytest.raises(Exception):
            bus.append("blocked")

    assert _median_ms(_attempt, repeat=5) < 100.0
    bus.close()


def test_render_of_a_full_batch_is_fast() -> None:
    batch = [
        BroadcastMessage(time=1_700_000_000_000 + index * 1000, content=MESSAGE)
        for index in range(READ_LIMIT)
    ]

    def _render() -> None:
        format_broadcast_batch(batch, token_budget=8_000, count_tokens=count_tokens)

    assert _median_ms(_render) < 25.0


def test_render_of_oversized_messages_is_bounded() -> None:
    batch = [
        BroadcastMessage(time=1_700_000_000_000 + index, content="Z" * 32_000)
        for index in range(25)
    ]

    def _render() -> None:
        format_broadcast_batch(batch, token_budget=2_000, count_tokens=count_tokens)

    assert _median_ms(_render) < 50.0
