"""Tests for BM25 retrieval components."""

import tempfile

import pytest

from kimix.retrieval import (
    NgramTokenizer,
    InvertedIndex,
    BM25Scorer,
    LevenshteinAutomaton,
    Searcher,
)


class TestNgramTokenizer:
    def test_tokenize_basic(self):
        t = NgramTokenizer(n=2)
        tokens = t.tokenize("hello", n=2)
        assert tokens == ["he", "el", "ll", "lo"]

    def test_tokenize_short_text(self):
        t = NgramTokenizer(n=3)
        tokens = t.tokenize("ab")
        assert tokens == ["ab"]

    def test_normalize(self):
        assert NgramTokenizer.normalize("HELLO") == "hello"

    def test_detect_n_cjk(self):
        t = NgramTokenizer(n=3)
        assert t._detect_n("你好世界") == 2

    def test_detect_n_latin(self):
        t = NgramTokenizer(n=2)
        assert t._detect_n("hello world") == 3

    def test_empty_text(self):
        t = NgramTokenizer(n=2)
        assert t.tokenize("") == []
        assert t.tokenize("   ") == []


class TestInvertedIndex:
    def test_add_and_get_postings(self):
        idx = InvertedIndex()
        idx.add_document(0, ["alpha", "beta", "gamma"])
        idx.add_document(1, ["alpha", "beta"])
        idx.finalize(stop_threshold=1.0)
        postings = idx.get_postings("alpha")
        assert postings is not None
        docs, tfs = postings
        assert len(docs) == 2

    def test_doc_freq(self):
        idx = InvertedIndex()
        idx.add_document(0, ["alpha", "beta"])
        idx.add_document(1, ["alpha"])
        idx.finalize(stop_threshold=1.0)
        assert idx.doc_freq("alpha") == 2
        assert idx.doc_freq("beta") == 1
        assert idx.doc_freq("gamma") == 0

    def test_save_and_load(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello", "world"])
        idx.add_document(1, ["hello"])
        idx.finalize(stop_threshold=1.0)
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            idx.save(path)
            idx2 = InvertedIndex()
            idx2.load(path)
            assert idx2.N == idx.N
            assert idx2.doc_freq("hello") == 2
        finally:
            import os
            os.unlink(path)

    def test_cannot_add_after_finalize(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.finalize()
        with pytest.raises(RuntimeError):
            idx.add_document(1, ["b"])


class TestBM25Scorer:
    def test_basic_score(self):
        idx = InvertedIndex()
        idx.add_document(0, ["python", "async", "programming"])
        idx.add_document(1, ["python", "threading"])
        idx.finalize(stop_threshold=0.6)
        scorer = BM25Scorer(idx)
        scores = scorer.score(["async"])
        assert len(scores) == 1
        assert scores[0] > 0

    def test_empty_index(self):
        idx = InvertedIndex()
        scorer = BM25Scorer(idx)
        scores = scorer.score(["test"])
        assert scores == {}

    def test_candidate_docs_filter(self):
        idx = InvertedIndex()
        idx.add_document(0, ["alpha", "beta"])
        idx.add_document(1, ["alpha", "gamma"])
        idx.finalize(stop_threshold=1.0)
        scorer = BM25Scorer(idx)
        scores = scorer.score(["alpha"], candidate_docs={0})
        assert len(scores) == 1
        assert 0 in scores
        assert 1 not in scores


class TestLevenshteinAutomaton:
    def test_exact_match(self):
        auto = LevenshteinAutomaton("hello", max_edits=0)
        assert auto.match(["hello"]) == ["hello"]

    def test_fuzzy_match(self):
        auto = LevenshteinAutomaton("hello", max_edits=1)
        results = auto.match(["hello", "hell", "hallo", "world"])
        assert "hello" in results
        assert "hell" in results
        assert "hallo" in results
        assert "world" not in results

    def test_auto_fuzziness(self):
        assert LevenshteinAutomaton.auto_fuzziness("ab") == 0
        assert LevenshteinAutomaton.auto_fuzziness("hello") == 1
        assert LevenshteinAutomaton.auto_fuzziness("hello world") == 2

    def test_damerau_levenshtein(self):
        auto = LevenshteinAutomaton("", max_edits=0)
        assert auto._damerau_levenshtein("hello", "hello") == 0
        assert auto._damerau_levenshtein("hello", "hell") == 1
        assert auto._damerau_levenshtein("ab", "ba") == 1  # transposition

    def test_max_expansions(self):
        auto = LevenshteinAutomaton("a", max_edits=1)
        dictionary = ["a", "b", "c", "d", "e"]
        results = auto.match(dictionary, max_expansions=2)
        assert len(results) <= 2

    def test_prefix_filter(self):
        auto = LevenshteinAutomaton("hello", max_edits=2, prefix_length=2)
        # 'ha' != 'he', so 'hallo' is filtered by prefix
        results = auto.match(["hallo", "world"])
        assert "hallo" not in results
        assert "world" not in results
        # 'helo' matches prefix 'he' and is within 1 edit
        results = auto.match(["hello", "helo", "world"])
        assert "hello" in results
        assert "helo" in results
        assert "world" not in results


class TestSearcher:
    def test_basic_search(self):
        tokenizer = NgramTokenizer(n=3)
        idx = InvertedIndex()
        idx.add_document(0, tokenizer.tokenize("python async programming"))
        idx.add_document(1, tokenizer.tokenize("python threading model"))
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx)
        results = searcher.search("async programming", top_k=2)
        assert len(results) <= 2
        assert len(results) > 0
        assert results[0][0] in {0, 1}

    def test_empty_index(self):
        idx = InvertedIndex()
        searcher = Searcher(idx)
        results = searcher.search("test")
        assert results == []

    def test_empty_query(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.finalize()
        searcher = Searcher(idx)
        results = searcher.search("")
        assert results == []

    def test_min_should_match(self):
        idx = InvertedIndex()
        idx.add_document(0, ["python", "async"])
        idx.add_document(1, ["python", "threading"])
        idx.finalize()
        searcher = Searcher(idx, min_should_match=1.0)
        results = searcher.search("python nonexistent")
        assert len(results) == 0  # Only one token matches, need 100%

    def test_fuzzy_search(self):
        tokenizer = NgramTokenizer(n=3)
        idx = InvertedIndex()
        idx.add_document(0, tokenizer.tokenize("hello world"))
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx, fuzziness=1)
        results = searcher.search("helo wrld")  # typo
        assert len(results) > 0
