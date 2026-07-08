"""Benchmark: xxhash should be faster than md5 for non-crypto hashing."""

import time

import xxhash


def test_xxhash_vs_md5_speed() -> None:
    """xxhash should be faster than md5 for SimHash fingerprinting."""
    import hashlib

    data = b"test data" * 1000
    t0 = time.perf_counter()
    for _ in range(100000):
        hashlib.md5(data).digest()[:8]  # noqa: S324
    t_md5 = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(100000):
        xxhash.xxh64(data).intdigest()
    t_xx = time.perf_counter() - t0
    assert t_xx < t_md5 * 0.5  # should be at least 2x faster
