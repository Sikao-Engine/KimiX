"""Broad JSONL history benchmarks; cold rebuild and warm query are separate."""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

import orjson
import pytest
from kosong.message import Message

from kimi_cli.soul.history_index import HistoryIndex
from kimi_cli.wire.types import TextPart

pytestmark = pytest.mark.benchmark


def _msg(role: str, text: str) -> Message:
    return Message(role=role, content=[TextPart(text=text)])


def _report(name: str, **metrics: object) -> None:
    print("JSONL_BENCHMARK " + orjson.dumps({"case": name, **metrics}).decode())


def _seed_history(tmp_path: Path, count: int = 5000) -> Path:
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path)
    idx.load()
    batch = [
        _msg("user", f"Message {i} about sqlite and python deployment")
        for i in range(count)
    ]
    started = time.perf_counter()
    idx.index_messages(batch)
    elapsed = time.perf_counter() - started
    idx.close()
    _report(
        "batch_5000",
        turns=count,
        append_seconds=elapsed,
        bytes=path.stat().st_size,
        bytes_per_turn=path.stat().st_size / count,
    )
    assert elapsed < 2.0, f"batched indexing of {count} turns took {elapsed:.2f}s"
    return path


def test_common_keyword_warm_search_and_cold_rebuild(tmp_path: Path) -> None:
    path = _seed_history(tmp_path)
    started = time.perf_counter()
    idx = HistoryIndex(jsonl_path=path)
    idx.load()
    cold_seconds = time.perf_counter() - started
    started = time.perf_counter()
    results = idx.search("sqlite", top_k=5)
    warm_seconds = time.perf_counter() - started
    started = time.perf_counter()
    fallback = idx.search("日", top_k=5)
    fallback_seconds = time.perf_counter() - started
    idx.close()
    _report(
        "warm_and_cold_5000",
        cold_load_seconds=cold_seconds,
        warm_latin_seconds=warm_seconds,
        fallback_seconds=fallback_seconds,
        hits=len(results),
        fallback_hits=len(fallback),
    )
    assert results
    assert warm_seconds < 0.05, f"warm search took {warm_seconds:.3f}s"
    assert cold_seconds < 5.0, f"cold JSONL rebuild took {cold_seconds:.3f}s"


def test_cjk_warm_search(tmp_path: Path) -> None:
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path)
    idx.index_messages([_msg("user", "关于Python安装和配置")] * 200)
    idx.close()
    idx = HistoryIndex(jsonl_path=path)
    idx.load()
    started = time.perf_counter()
    results = idx.search("安装", top_k=3)
    elapsed = time.perf_counter() - started
    idx.close()
    _report("cjk_200", warm_cjk_seconds=elapsed, hits=len(results))
    assert results
    assert elapsed < 0.05


def test_sub_million_token_corpus_load_append_search_and_memory(tmp_path: Path) -> None:
    # 1200 * roughly 750 estimated tokens: near the normal sub-1M-token bound.
    count = 1200
    payload = "module deployment python search " * 100  # 3000 characters
    path = tmp_path / "history.jsonl"
    idx = HistoryIndex(jsonl_path=path)
    tracemalloc.start()
    started = time.perf_counter()
    idx.index_messages([_msg("user", f"turn {i} {payload}") for i in range(count)])
    append_seconds = time.perf_counter() - started
    idx.close()
    started = time.perf_counter()
    warm = HistoryIndex(jsonl_path=path)
    warm.load()
    load_seconds = time.perf_counter() - started
    started = time.perf_counter()
    hits = warm.search("python", top_k=5)
    latin_seconds = time.perf_counter() - started
    started = time.perf_counter()
    fallback = warm.search("turn 1199", top_k=1)
    fallback_seconds = time.perf_counter() - started
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    warm.close()
    size = path.stat().st_size
    _report(
        "sub_million_token_corpus",
        turns=count,
        estimated_tokens=count * 750,
        append_seconds=append_seconds,
        cold_load_seconds=load_seconds,
        warm_latin_seconds=latin_seconds,
        fallback_seconds=fallback_seconds,
        jsonl_bytes=size,
        bytes_per_turn=size / count,
        traced_current_bytes=current,
        traced_peak_bytes=peak,
        hits=len(hits),
        fallback_hits=len(fallback),
    )
    assert hits and fallback
    assert append_seconds < 15.0
    assert load_seconds < 15.0
    assert latin_seconds < 0.25
