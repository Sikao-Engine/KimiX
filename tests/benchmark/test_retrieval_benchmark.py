"""Performance benchmarks for kimix.retrieval.

All timings are assert-based so the file doubles as a regression test.
"""

from __future__ import annotations

import random
import string
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from kimix.retrieval import (
    BM25Scorer,
    InvertedIndex,
    LevenshteinAutomaton,
    NgramTokenizer,
    Searcher,
)

pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_words(n: int, length: int = 5) -> list[str]:
    """Return *n* random lower-case words of *length* chars."""
    rng = random.Random(42)
    return [
        "".join(rng.choices(string.ascii_lowercase, k=length))
        for _ in range(n)
    ]


def _lorem(words: int) -> str:
    """Return deterministic pseudo-text."""
    pool = (
        "lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
        "eiusmod tempor incididunt ut labore et dolore magna aliqua ut enim "
        "ad minim veniam quis nostrud exercitation ullamco laboris nisi ut "
        "aliquip ex ea commodo consequat duis aute irure dolor in reprehenderit "
        "in voluptate velit esse cillum dolore eu fugiat nulla pariatur "
        "excepteur sint occaecat cupidatat non proident sunt in culpa qui "
        "officia deserunt mollit anim id est laborum"
    ).split()
    return " ".join(pool[i % len(pool)] for i in range(words))


def _build_index(num_docs: int, tokens_per_doc: int) -> InvertedIndex:
    """Build an inverted index with random tokens."""
    idx = InvertedIndex()
    words = _random_words(10_000)
    rng = random.Random(42)
    for doc_id in range(num_docs):
        tokens = [rng.choice(words) for _ in range(tokens_per_doc)]
        idx.add_document(doc_id, tokens)
    idx.finalize(stop_threshold=1.0)
    return idx


# ---------------------------------------------------------------------------
# NgramTokenizer
# ---------------------------------------------------------------------------


class TestNgramTokenizerBenchmark:
    def test_normalize_short(self) -> None:
        t = NgramTokenizer()
        start = time.perf_counter()
        for _ in range(100_000):
            t.normalize("Hello World")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_normalize_long(self) -> None:
        t = NgramTokenizer()
        text = _lorem(1_000)
        start = time.perf_counter()
        for _ in range(20_000):
            t.normalize(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0

    def test_is_cjk(self) -> None:
        t = NgramTokenizer()
        chars = ["a", "\u4e00", "1", "\u3042", "\uac00", "z"]
        start = time.perf_counter()
        for _ in range(200_000):
            for c in chars:
                t._is_cjk(c)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_detect_n_latin(self) -> None:
        t = NgramTokenizer()
        text = "abcdef" * 100
        start = time.perf_counter()
        for _ in range(50_000):
            t._detect_n(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 8.0

    def test_detect_n_cjk(self) -> None:
        t = NgramTokenizer()
        text = "\u4e00\u4e01\u4e02" * 100
        start = time.perf_counter()
        for _ in range(50_000):
            t._detect_n(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_tokenize_short(self) -> None:
        t = NgramTokenizer(n=3)
        start = time.perf_counter()
        for _ in range(100_000):
            t.tokenize("hello world")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_tokenize_long(self) -> None:
        t = NgramTokenizer(n=3)
        text = _lorem(500)
        start = time.perf_counter()
        for _ in range(20_000):
            t.tokenize(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 20.0

    def test_tokenize_cjk(self) -> None:
        t = NgramTokenizer()
        text = "中文测试文本" * 50
        start = time.perf_counter()
        for _ in range(20_000):
            t.tokenize(text)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0


# ---------------------------------------------------------------------------
# InvertedIndex
# ---------------------------------------------------------------------------


class TestInvertedIndexBenchmark:
    def test_add_document_small(self) -> None:
        idx = InvertedIndex()
        tokens = ["tok"] * 100
        start = time.perf_counter()
        for i in range(10_000):
            idx.add_document(i, tokens)
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0

    def test_add_document_large(self) -> None:
        idx = InvertedIndex()
        words = _random_words(5_000)
        rng = random.Random(42)
        start = time.perf_counter()
        for i in range(1_000):
            tokens = [rng.choice(words) for _ in range(1_000)]
            idx.add_document(i, tokens)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_finalize_small(self) -> None:
        idx = InvertedIndex()
        for i in range(1_000):
            idx.add_document(i, ["aa", "bb", "cc", "aa"])
        start = time.perf_counter()
        idx.finalize(stop_threshold=1.0)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0

    def test_finalize_medium(self) -> None:
        idx = _build_index(5_000, 50)
        # _build_index already calls finalize; benchmark a fresh one
        idx2 = InvertedIndex()
        rng = random.Random(42)
        words = _random_words(5_000)
        for i in range(5_000):
            tokens = [rng.choice(words) for _ in range(50)]
            idx2.add_document(i, tokens)
        start = time.perf_counter()
        idx2.finalize(stop_threshold=1.0)
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0

    def test_get_postings(self) -> None:
        idx = _build_index(1_000, 100)
        term = list(idx.terms())[0]
        start = time.perf_counter()
        for _ in range(100_000):
            idx.get_postings(term)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_doc_freq(self) -> None:
        idx = _build_index(1_000, 100)
        term = list(idx.terms())[0]
        start = time.perf_counter()
        for _ in range(100_000):
            idx.doc_freq(term)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_has_term(self) -> None:
        idx = _build_index(1_000, 100)
        term = list(idx.terms())[0]
        start = time.perf_counter()
        for _ in range(200_000):
            idx.has_term(term)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0

    def test_terms_iteration(self) -> None:
        idx = _build_index(1_000, 100)
        start = time.perf_counter()
        for _ in range(10_000):
            list(idx.terms())
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_save_load_small(self) -> None:
        idx = _build_index(1_000, 50)
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            path = f.name
        try:
            start = time.perf_counter()
            idx.save(path)
            idx2 = InvertedIndex()
            idx2.load(path)
            elapsed = time.perf_counter() - start
            assert elapsed < 2.0
        finally:
            Path(path).unlink(missing_ok=True)

    def test_save_load_large(self) -> None:
        idx = _build_index(10_000, 100)
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            path = f.name
        try:
            start = time.perf_counter()
            idx.save(path)
            idx2 = InvertedIndex()
            idx2.load(path)
            elapsed = time.perf_counter() - start
            assert elapsed < 10.0
        finally:
            Path(path).unlink(missing_ok=True)

    def test_generate_deletes(self) -> None:
        start = time.perf_counter()
        for _ in range(10_000):
            InvertedIndex._generate_deletes("abcdefgh", 2)
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0

    def test_build_symmetric_delete_index(self) -> None:
        idx = _build_index(5_000, 50)
        start = time.perf_counter()
        idx._build_symmetric_delete_index()
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0

    def test_is_stop_ngram(self) -> None:
        idx = InvertedIndex()
        idx.add_document(0, ["aa"])
        idx.add_document(1, ["bb"])
        start = time.perf_counter()
        for _ in range(200_000):
            idx._is_stop_ngram("aa", 1, threshold=0.5)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0


# ---------------------------------------------------------------------------
# BM25Scorer
# ---------------------------------------------------------------------------


class TestBM25ScorerBenchmark:
    def test_idf(self) -> None:
        scorer = BM25Scorer(InvertedIndex())
        start = time.perf_counter()
        for _ in range(500_000):
            scorer._idf(2, 10_000)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_build_denom_base(self) -> None:
        idx = _build_index(10_000, 50)
        start = time.perf_counter()
        for _ in range(1_000):
            BM25Scorer(idx)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_ensure_buffers(self) -> None:
        idx = _build_index(100, 10)
        scorer = BM25Scorer(idx)
        start = time.perf_counter()
        for size in range(1, 5_000):
            scorer._ensure_buffers(size)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_accumulate_small(self) -> None:
        idx = _build_index(1_000, 50)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        start = time.perf_counter()
        for _ in range(10_000):
            scorer._accumulate(query, None)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_accumulate_sparse_small(self) -> None:
        idx = _build_index(1_000, 50)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        start = time.perf_counter()
        for _ in range(10_000):
            scorer._accumulate_sparse(query, None)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_accumulate_with_candidates(self) -> None:
        idx = _build_index(1_000, 50)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        candidates = set(range(100))
        start = time.perf_counter()
        for _ in range(10_000):
            scorer._accumulate(query, candidates)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_score_small(self) -> None:
        idx = _build_index(1_000, 50)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        start = time.perf_counter()
        for _ in range(10_000):
            scorer.score(query)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_score_large_sparse(self) -> None:
        idx = _build_index(60_000, 30)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        start = time.perf_counter()
        for _ in range(100):
            scorer.score(query)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_score_topk_small(self) -> None:
        idx = _build_index(1_000, 50)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        start = time.perf_counter()
        for _ in range(10_000):
            scorer.score_topk(query, top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_score_topk_large(self) -> None:
        idx = _build_index(60_000, 30)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        start = time.perf_counter()
        for _ in range(1_000):
            scorer.score_topk(query, top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_score_topk_all_docs(self) -> None:
        idx = _build_index(10_000, 30)
        scorer = BM25Scorer(idx)
        query = ["tok"] * 5
        start = time.perf_counter()
        for _ in range(1_000):
            scorer.score_topk(query, top_k=10_000)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# LevenshteinAutomaton
# ---------------------------------------------------------------------------


class TestLevenshteinAutomatonBenchmark:
    def test_auto_fuzziness(self) -> None:
        terms = ["a", "ab", "abc", "abcd", "abcde", "abcdef"]
        start = time.perf_counter()
        for _ in range(200_000):
            for t in terms:
                LevenshteinAutomaton.auto_fuzziness(t)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_damerau_levenshtein_exact(self) -> None:
        start = time.perf_counter()
        for _ in range(200_000):
            LevenshteinAutomaton._damerau_levenshtein("hello", "hello")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_damerau_levenshtein_short(self) -> None:
        start = time.perf_counter()
        for _ in range(200_000):
            LevenshteinAutomaton._damerau_levenshtein("ab", "ba")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_damerau_levenshtein_long(self) -> None:
        start = time.perf_counter()
        for _ in range(50_000):
            LevenshteinAutomaton._damerau_levenshtein(
                "abcdefghijklmnopqrstuvwxyz",
                "abcdefghijklmnopqrstuvwxzy",
            )
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0

    def test_freq_lower_bound(self) -> None:
        auto = LevenshteinAutomaton("hello", max_edits=1)
        start = time.perf_counter()
        for _ in range(200_000):
            auto._freq_lower_bound("hello")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_match_list_small(self) -> None:
        auto = LevenshteinAutomaton("hello", max_edits=1)
        dictionary = ["hello", "hallo", "hillo", "world", "helio"] * 100
        start = time.perf_counter()
        for _ in range(10_000):
            auto.match(dictionary, max_expansions=50)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_match_inverted_index(self) -> None:
        idx = _build_index(1_000, 50)
        auto = LevenshteinAutomaton("tok", max_edits=1, prefix_length=1)
        start = time.perf_counter()
        for _ in range(5_000):
            auto.match(idx, max_expansions=50)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_match_symmetric_delete_path(self) -> None:
        idx = _build_index(5_000, 50)
        auto = LevenshteinAutomaton("to", max_edits=2, prefix_length=1)
        start = time.perf_counter()
        for _ in range(5_000):
            auto.match(idx, max_expansions=50)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# Searcher
# ---------------------------------------------------------------------------


class TestSearcherBenchmark:
    def test_is_latin_token(self) -> None:
        start = time.perf_counter()
        for _ in range(500_000):
            Searcher._is_latin_token("hello")
            Searcher._is_latin_token("\u4e00")
            Searcher._is_latin_token("")
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0

    def test_expand_token_exact(self) -> None:
        idx = _build_index(1_000, 50)
        searcher = Searcher(idx, fuzziness=0)
        term = list(idx.terms())[0]
        start = time.perf_counter()
        for _ in range(50_000):
            searcher._expand_token(term)
        elapsed = time.perf_counter() - start
        assert elapsed < 3.0

    def test_expand_token_fuzzy(self) -> None:
        idx = _build_index(1_000, 50)
        searcher = Searcher(idx, fuzziness="AUTO")
        term = "to"  # likely to fuzzy-match many terms
        start = time.perf_counter()
        for _ in range(5_000):
            searcher._expand_token(term)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0

    def test_search_small(self) -> None:
        idx = _build_index(1_000, 50)
        searcher = Searcher(idx)
        start = time.perf_counter()
        for _ in range(5_000):
            searcher.search("tok", top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_search_medium(self) -> None:
        idx = _build_index(10_000, 50)
        searcher = Searcher(idx)
        start = time.perf_counter()
        for _ in range(1_000):
            searcher.search("tok", top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_search_large_sparse(self) -> None:
        idx = _build_index(60_000, 30)
        searcher = Searcher(idx)
        start = time.perf_counter()
        for _ in range(500):
            searcher.search("tok", top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_search_multi_token(self) -> None:
        idx = _build_index(10_000, 50)
        searcher = Searcher(idx)
        start = time.perf_counter()
        for _ in range(1_000):
            searcher.search("tok1 tok2 tok3", top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

    def test_search_fuzzy(self) -> None:
        idx = _build_index(5_000, 50)
        searcher = Searcher(idx, fuzziness="AUTO")
        start = time.perf_counter()
        for _ in range(500):
            searcher.search("helo", top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0

    def test_search_cjk(self) -> None:
        idx = InvertedIndex()
        tokenizer = NgramTokenizer()
        for i in range(1_000):
            text = "中文测试文档" + str(i)
            idx.add_document(i, tokenizer.tokenize(text))
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx, tokenizer=tokenizer)
        start = time.perf_counter()
        for _ in range(5_000):
            searcher.search("中文", top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 10.0


# ---------------------------------------------------------------------------
# End-to-end workload
# ---------------------------------------------------------------------------


class TestEndToEndBenchmark:
    def test_index_search_save_load_cycle(self) -> None:
        idx = InvertedIndex()
        tokenizer = NgramTokenizer(n=3)
        docs = [_lorem(50) + f" doc {i}" for i in range(5_000)]
        for i, text in enumerate(docs):
            idx.add_document(i, tokenizer.tokenize(text))
        start = time.perf_counter()
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx, tokenizer=tokenizer)
        results = searcher.search("lorem ipsum", top_k=10)
        elapsed = time.perf_counter() - start
        assert len(results) == 10
        assert elapsed < 10.0

    def test_high_throughput_queries(self) -> None:
        idx = _build_index(10_000, 50)
        searcher = Searcher(idx)
        queries = [f"query{i}" for i in range(1_000)]
        start = time.perf_counter()
        for q in queries:
            searcher.search(q, top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 30.0

    @pytest.mark.slow
    def test_very_large_index_search(self) -> None:
        idx = _build_index(100_000, 20)
        searcher = Searcher(idx)
        start = time.perf_counter()
        for _ in range(100):
            searcher.search("tok", top_k=10)
        elapsed = time.perf_counter() - start
        assert elapsed < 30.0
