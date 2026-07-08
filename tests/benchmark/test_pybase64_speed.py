"""Benchmark: pybase64 should be faster than stdlib base64."""

import time


def test_pybase64_speed() -> None:
    """pybase64 should be faster than stdlib base64."""
    import base64
    import pybase64

    data = b"x" * 1000000
    t0 = time.perf_counter()
    for _ in range(100):
        base64.b64encode(data)
    t_base64 = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(100):
        pybase64.b64encode(data)
    t_pybase64 = time.perf_counter() - t0
    assert t_pybase64 < t_base64 * 0.6  # should be ~2-4x faster
