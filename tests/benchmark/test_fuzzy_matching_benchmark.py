"""Benchmark comparing the old and new fuzzy-matching implementations.

This file measures the performance improvements from:

1. Replacing the BM25 + inverted-index title matcher in
   ``kimi_cli.tools.todo`` with a lightweight ``rapidfuzz`` matcher.
2. Replacing the inline ``difflib`` logic in
   ``kosong.tooling._repair_dict_for_model`` with the reusable
   ``_fuzzy_match_keys`` helper.

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import difflib
import random
import string
import time
from typing import Any

import pytest
import rapidfuzz
from pydantic import BaseModel

from kimi_cli.tools.todo import TodoList
from kosong.tooling import (
    _fuzzy_match_keys,
    _repair_dict_for_model,
    _cached_model_field_info,
    _coerce_value,
    _format_pydantic_validation_error,
    _clean_error_loc,
)

pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# Helpers that reproduce the OLD implementations for comparison.
# ---------------------------------------------------------------------------


def _old_find_nearest_titles(
    query_titles: list[str],
    candidate_titles: list[str],
    top_k: int = 1,
) -> dict[str, list[tuple[str, float]]]:
    """Original BM25-based implementation from TodoList._find_nearest_titles."""
    from kimix.retrieval import InvertedIndex, NgramTokenizer, Searcher

    if not candidate_titles or not query_titles:
        return {q: [] for q in query_titles}

    tokenizer = NgramTokenizer(n=2)
    index = InvertedIndex()
    for doc_id, title in enumerate(candidate_titles):
        index.add_document(doc_id, tokenizer.tokenize(title))
    index.finalize(stop_threshold=1.0)
    searcher = Searcher(index, tokenizer=tokenizer)

    results: dict[str, list[tuple[str, float]]] = {}
    for query in query_titles:
        hits = searcher.search(query, top_k=top_k)
        results[query] = [
            (candidate_titles[doc_id], float(score)) for doc_id, score in hits
        ]
    return results


def _old_repair_fuzzy_pass(
    missing: set[str], unmapped_keys: set[str]
) -> dict[str, str]:
    """Original inline difflib fuzzy pass from _repair_dict_for_model."""
    available = list(unmapped_keys)
    result: dict[str, str] = {}
    if not available or not missing:
        return result

    candidates: list[tuple[float, str, str]] = []
    for missing_field in missing:
        if len(missing_field) < 4:
            continue
        cutoff = 0.75 if len(missing_field) >= 8 else 0.80
        close = difflib.get_close_matches(
            missing_field, available, n=1, cutoff=cutoff
        )
        if close:
            matched_key = close[0]
            if len(matched_key) < 4:
                continue
            ratio = difflib.SequenceMatcher(None, missing_field, matched_key).ratio()
            candidates.append((ratio, missing_field, matched_key))

    candidates.sort(key=lambda x: x[0], reverse=True)
    used: set[str] = set()
    for _ratio, missing_field, matched_key in candidates:
        if matched_key in used:
            continue
        used.add(matched_key)
        result[missing_field] = matched_key
    return result


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _random_titles(n: int, rng: random.Random) -> list[str]:
    """Return *n* short todo-like titles."""
    verbs = [
        "Implement", "Fix", "Write", "Review", "Test", "Refactor",
        "Deploy", "Update", "Document", "Investigate",
    ]
    nouns = [
        "feature", "bug", "tests", "code", "docs", "pipeline",
        "api", "ui", "config", "module", "service", "endpoint",
    ]
    modifiers = ["", "user", "auth", "search", "billing", "core", "admin"]
    titles: list[str] = []
    for i in range(n):
        verb = verbs[i % len(verbs)]
        noun = nouns[(i * 3) % len(nouns)]
        mod = modifiers[(i * 7) % len(modifiers)]
        titles.append(f"{verb} {mod} {noun}".strip().replace("  ", " "))
    rng.shuffle(titles)
    return titles


# ---------------------------------------------------------------------------
# Todo title matcher benchmark
# ---------------------------------------------------------------------------


class TestTodoTitleMatcherBenchmark:
    """Benchmarks for TodoList._find_nearest_titles."""

    def test_new_impl_faster_than_old_bm25(self) -> None:
        """The rapidfuzz implementation should be much faster than BM25 rebuild."""
        rng = random.Random(42)
        candidate_titles = _random_titles(500, rng)
        query_titles = candidate_titles[:25]

        # Warm-up imports.
        _old_find_nearest_titles(query_titles[:1], candidate_titles[:1])
        TodoList._find_nearest_titles(query_titles[:1], candidate_titles[:1])

        start = time.perf_counter()
        for _ in range(20):
            _old_find_nearest_titles(query_titles, candidate_titles, top_k=1)
        old_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(20):
            TodoList._find_nearest_titles(query_titles, candidate_titles, top_k=1)
        new_elapsed = time.perf_counter() - start

        # The new implementation is expected to be at least 5x faster because
        # it avoids rebuilding a numpy-backed inverted index on every call.
        assert new_elapsed < old_elapsed / 5, (
            f"rapidfuzz ({new_elapsed:.4f}s) should be >5x faster than "
            f"BM25 rebuild ({old_elapsed:.4f}s)"
        )

    def test_new_impl_matches_old_impl_on_common_cases(self) -> None:
        """Functional sanity check: both find the obvious nearest title."""
        queries = ["Implement featuer", "fix buq", "Writ docs"]
        candidates = ["Implement feature", "fix bug", "Write docs", "Ship release"]

        old = _old_find_nearest_titles(queries, candidates, top_k=1)
        new = TodoList._find_nearest_titles(queries, candidates, top_k=1)

        for q in queries:
            assert old[q], f"old impl returned no match for {q!r}"
            assert new[q], f"new impl returned no match for {q!r}"
            assert old[q][0][0] == new[q][0].choice, (
                f"mismatch for {q!r}: old={old[q][0][0]!r}, new={new[q][0].choice!r}"
            )

    def test_new_impl_rejects_weak_matches(self) -> None:
        """A completely unrelated query must not produce a suggestion."""
        queries = ["Completely unrelated"]
        candidates = ["Implement feature", "fix bug"]
        result = TodoList._find_nearest_titles(queries, candidates, top_k=1)
        assert result["Completely unrelated"] == []

    def test_new_impl_handles_word_reorder(self) -> None:
        """Reordered words still match with the token-sort scorer."""
        result = TodoList._find_nearest_titles(
            ["bug fix"], ["fix bug", "write tests"], top_k=1
        )
        assert result["bug fix"][0].choice == "fix bug"


# ---------------------------------------------------------------------------
# Kosong repair fuzzy-pass benchmark
# ---------------------------------------------------------------------------


class TestKosongFuzzyMatchBenchmark:
    """Benchmarks for kosong.tooling._fuzzy_match_keys."""

    def test_new_helper_is_superset_of_old_inline_logic(self) -> None:
        """The helper preserves all old matches and adds case-insensitive ones."""
        missing = {
            "base_url",
            "output_path",
            "case_insensitive",
            "max_char",
        }
        available = {
            "base_URL",
            "out_path",
            "ignore_case",
            "chars",
            "unrelated_key",
        }

        old = _old_repair_fuzzy_pass(missing, available)
        new = _fuzzy_match_keys(missing, available)

        # Every mapping produced by the old case-sensitive code must still be
        # produced by the new code.
        for key, value in old.items():
            assert new.get(key) == value, (
                f"new helper lost old mapping {key!r} -> {value!r}"
            )

        # The new code additionally fixes the ``base_url`` -> ``base_URL``
        # case-difference match.
        assert new.get("base_url") == "base_URL"

    def test_case_insensitive_match(self) -> None:
        """Casing differences no longer suppress valid fuzzy matches."""
        class Model(BaseModel):
            base_url: str = ""

        repaired = _repair_dict_for_model({"base_URL": "x"}, Model)
        assert repaired == {"base_url": "x"}

    def test_helper_faster_or_on_par(self) -> None:
        """The helper is at least as fast as the old inline loop."""
        rng = random.Random(42)
        missing = {f"field_{i:03d}_name" for i in range(50)}
        available = {f"field_{i:03d}_Name" for i in range(200)}

        # Warm-up.
        _old_repair_fuzzy_pass(missing, available)
        _fuzzy_match_keys(missing, available)

        start = time.perf_counter()
        for _ in range(200):
            _old_repair_fuzzy_pass(missing, available)
        old_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(200):
            _fuzzy_match_keys(missing, available)
        new_elapsed = time.perf_counter() - start

        # The helper should not be slower; in practice it is faster because it
        # avoids one redundant ratio computation and short-circuits early.
        assert new_elapsed <= old_elapsed * 1.5, (
            f"new helper ({new_elapsed:.4f}s) should not be much slower than "
            f"old inline ({old_elapsed:.4f}s)"
        )


# ---------------------------------------------------------------------------
# Micro-benchmarks for rapidfuzz vs difflib title matching
# ---------------------------------------------------------------------------


class TestRapidfuzzVsDifflibBenchmark:
    """Direct comparison of rapidfuzz and difflib for short title strings."""

    def test_rapidfuzz_faster_than_difflib_on_titles(self) -> None:
        """rapidfuzz should outperform difflib for the todo title workload."""
        rng = random.Random(42)
        candidates = _random_titles(500, rng)
        queries = _random_titles(50, rng)

        start = time.perf_counter()
        for _ in range(20):
            for q in queries:
                difflib.get_close_matches(q, candidates, n=1, cutoff=0.6)
        difflib_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(20):
            for q in queries:
                rapidfuzz.process.extract(
                    q,
                    candidates,
                    scorer=rapidfuzz.fuzz.token_sort_ratio,
                    limit=1,
                    score_cutoff=60.0,
                )
        rapidfuzz_elapsed = time.perf_counter() - start

        assert rapidfuzz_elapsed < difflib_elapsed / 2, (
            f"rapidfuzz ({rapidfuzz_elapsed:.4f}s) should be >2x faster than "
            f"difflib ({difflib_elapsed:.4f}s)"
        )


# ---------------------------------------------------------------------------
# Extended benchmarks: repair_dict_for_model, cached_model_field_info, coerce_value
# ---------------------------------------------------------------------------


class TestRepairDictForModelBenchmark:
    """Benchmarks for _repair_dict_for_model at various scales."""

    def test_large_nested_model_50_fields(self) -> None:
        """_repair_dict_for_model() with large nested model with 50+ fields."""
        from pydantic import BaseModel

        class Inner(BaseModel):
            field_01: str = ""
            field_02: int = 0
            field_03: float = 0.0
            field_04: bool = False
            field_05: list[str] = []

        class Middle(BaseModel):
            inner_01: Inner = Inner()
            inner_02: Inner = Inner()
            name: str = ""
            count: int = 0
            active: bool = True

        class Outer(BaseModel):
            middle_01: Middle = Middle()
            middle_02: Middle = Middle()
            middle_03: Middle = Middle()
            title: str = "test"
            version: int = 1

        rng = random.Random(42)
        raw_data: dict[str, object] = {
            "title": "test",
            "version": "1",  # wrong type
            "unknown_key_01": "value1",
            "unknown_key_02": "value2",
            "MIDDLE_01": {"name": "test", "count": "5"},  # case mismatch
        }

        start = time.perf_counter()
        for _ in range(2000):
            _repair_dict_for_model(raw_data, Outer)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_list_of_models_10x20(self) -> None:
        """_repair_dict_for_model() with list-of-models (10 items x 20 fields)."""
        from pydantic import BaseModel

        class Item(BaseModel):
            id: int = 0
            name: str = ""
            description: str = ""
            price: float = 0.0
            quantity: int = 0
            tags: list[str] = []
            active: bool = True
            category: str = ""
            sku: str = ""
            rating: float = 0.0

        items = [
            {
                "id": str(i),
                "name": f"item_{i}",
                "price": str(i * 1.5),
                "quantity": str(i * 10),
                "tags": ["tag1", "tag2"],
                "active": "true",
                "extra_field": "extra",
            }
            for i in range(10)
        ]

        start = time.perf_counter()
        for _ in range(1000):
            _repair_dict_for_model({"items": items, "unknown": "x"}, type("Wrapper", (BaseModel,), {"items": list[Item], "__annotations__": {"items": list[Item]}}))
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0


class TestCachedModelFieldInfoBenchmark:
    """Benchmarks for _cached_model_field_info."""

    def test_cache_hit_miss_ratio(self) -> None:
        """_cached_model_field_info() cache hit/miss ratio benchmark."""
        from pydantic import BaseModel

        class ModelA(BaseModel):
            field_01: str = ""
            field_02: int = 0

        class ModelB(BaseModel):
            field_01: str = ""
            field_02: int = 0
            field_03: float = 0.0

        class ModelC(BaseModel):
            field_01: str = ""

        models = [ModelA, ModelB, ModelC, ModelA, ModelB, ModelC]

        # Warm-up to populate cache
        for m in models:
            _cached_model_field_info(m)

        start = time.perf_counter()
        for _ in range(10_000):
            for m in models:
                _cached_model_field_info(m)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


class TestCoerceValueBenchmark:
    """Benchmarks for _coerce_value."""

    def test_all_type_combinations(self) -> None:
        """_coerce_value() — all type combinations throughput."""
        from pydantic import BaseModel
        from typing import Optional

        class SampleModel(BaseModel):
            str_field: str = ""
            int_field: int = 0
            float_field: float = 0.0
            bool_field: bool = False
            list_field: list[str] = []
            opt_str: Optional[str] = None
            opt_int: Optional[int] = None

        values = [
            ("hello", str),
            (42, int),
            ("42", int),
            (3.14, float),
            ("3.14", float),
            (True, bool),
            ("true", bool),
            ([1, 2, 3], list),
            (123, str),  # wrong type
            ("not_a_number", int),  # invalid
        ] * 5_000

        start = time.perf_counter()
        for value, target_type in values:
            _coerce_value(value, target_type)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0
