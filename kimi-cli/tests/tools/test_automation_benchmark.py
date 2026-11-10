from __future__ import annotations

import pytest
from tools.bench_automation import measure, summarize


def test_automation_benchmark_statistics() -> None:
    stats = summarize([0.004, 0.001, 0.003, 0.002])

    assert stats["iterations"] == 4
    assert stats["median_ms"] == 2.5
    assert stats["min_ms"] == 1.0
    assert stats["max_ms"] == 4.0
    with pytest.raises(ValueError, match="At least one sample"):
        summarize([])


async def test_automation_benchmark_excludes_warmup_and_returns_result() -> None:
    calls = 0

    async def operation() -> int:
        nonlocal calls
        calls += 1
        return calls

    stats, result = await measure(operation, repeat=3, warmup=2)

    assert calls == 5
    assert result == 5
    assert stats["iterations"] == 3
