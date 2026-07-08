"""Benchmark: regex should be at least as fast as re for common patterns."""

import time


def test_regex_vs_re_speed() -> None:
    """regex should be at least as fast as re for common patterns."""
    import re
    import regex as re2

    pattern = r"\b\w+@\w+\.\w+\b"
    text = "contact us at support@example.com or sales@example.org" * 1000
    t0 = time.perf_counter()
    for _ in range(1000):
        re.findall(pattern, text)
    t_re = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(1000):
        re2.findall(pattern, text)
    t_regex = time.perf_counter() - t0
    assert t_regex <= t_re * 1.3  # should be comparable or faster
