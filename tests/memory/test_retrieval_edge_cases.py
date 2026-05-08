"""Edge-case tests for retrieval module: CJK, empty queries, fuzzy limits."""

import pytest

from kimix.retrieval import (
    NgramTokenizer,
    InvertedIndex,
    BM25Scorer,
    LevenshteinAutomaton,
    Searcher,
)


class TestNgramTokenizerEdgeCases:
    def test_tokenize_pure_cjk(self):
        t = NgramTokenizer()
        tokens = t.tokenize("中文测试")
        assert len(tokens) > 0
        assert all(len(tok) == 2 for tok in tokens)

    def test_tokenize_mixed_cjk_latin(self):
        t = NgramTokenizer()
        tokens = t.tokenize("Python编程")
        # Should auto-detect bigram for CJK-heavy text
        assert len(tokens) > 0

    def test_tokenize_single_char(self):
        t = NgramTokenizer()
        tokens = t.tokenize("a")
        assert tokens == ["a"]

    def test_tokenize_empty(self):
        t = NgramTokenizer()
        assert t.tokenize("") == []
        assert t.tokenize("   ") == []

    def test_normalize_unicode(self):
        t = NgramTokenizer()
        # NFKC should normalize full-width letters
        result = t.normalize("ＡＢＣ")
        assert result == "abc"


class TestInvertedIndexEdgeCases:
    def test_empty_index_finalize(self):
        idx = InvertedIndex()
        idx.finalize()
        assert idx.N == 0
        assert idx.avgdl == 0.0

    def test_add_document_empty_tokens(self):
        idx = InvertedIndex()
        idx.add_document(0, [])
        idx.finalize()
        assert idx.N == 1
        assert idx.avgdl == 0.0

    def test_get_postings_missing_term(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.finalize()
        assert idx.get_postings("missing") is None

    def test_doc_freq_missing_term(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello"])
        idx.finalize()
        assert idx.doc_freq("missing") == 0


class TestBM25ScorerEdgeCases:
    def test_empty_query(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello", "world"])
        idx.finalize()
        scorer = BM25Scorer(idx)
        assert scorer.score([]) == {}

    def test_single_document(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello", "world", "hello"])
        idx.add_document(1, ["foo", "bar"])
        idx.finalize()
        scorer = BM25Scorer(idx)
        scores = scorer.score(["hello"])
        assert 0 in scores
        assert scores[0] > 0

    def test_candidate_docs_filter_excludes_all(self):
        idx = InvertedIndex()
        idx.add_document(0, ["a"])
        idx.add_document(1, ["b"])
        idx.finalize()
        scorer = BM25Scorer(idx)
        scores = scorer.score(["a"], candidate_docs={1})  # doc 1 has no 'a'
        assert scores == {}


class TestLevenshteinAutomatonEdgeCases:
    def test_zero_max_edits_exact_only(self):
        automaton = LevenshteinAutomaton("test", max_edits=0)
        results = automaton.match(["test", "tent", "best"])
        assert results == ["test"]

    def test_auto_fuzziness_short(self):
        assert LevenshteinAutomaton.auto_fuzziness("ab") == 0

    def test_auto_fuzziness_medium(self):
        assert LevenshteinAutomaton.auto_fuzziness("hello") == 1
        assert LevenshteinAutomaton.auto_fuzziness("abcdef") == 2

    def test_transposition_distance(self):
        dl = LevenshteinAutomaton._damerau_levenshtein
        assert dl("ab", "ba") == 1

    def test_empty_strings(self):
        dl = LevenshteinAutomaton._damerau_levenshtein
        assert dl("", "") == 0
        assert dl("abc", "") == 3


class TestSearcherEdgeCases:
    def test_search_cjk_query(self):
        idx = InvertedIndex()
        tokenizer = NgramTokenizer()
        # Pure CJK text ensures bigram tokenization
        tokens = tokenizer.tokenize("异步编程指南")
        idx.add_document(0, tokens)
        idx.add_document(1, ["other"])
        idx.finalize()
        searcher = Searcher(idx, tokenizer=tokenizer)
        results = searcher.search("异步", top_k=5)
        assert len(results) == 1
        assert results[0][0] == 0

    def test_search_no_match_due_to_min_should_match(self):
        idx = InvertedIndex()
        idx.add_document(0, ["hello", "world"])
        idx.finalize()
        searcher = Searcher(idx, min_should_match=1.0)
        results = searcher.search("foo bar", top_k=5)
        assert results == []

    def test_fuzzy_search_expansion(self):
        tokenizer = NgramTokenizer(n=3)
        idx = InvertedIndex()
        idx.add_document(0, tokenizer.tokenize("hello world"))
        idx.add_document(1, tokenizer.tokenize("foo bar"))
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx, tokenizer=tokenizer, fuzziness=1)
        results = searcher.search("helo", top_k=5)  # typo for hello
        assert len(results) == 1

    def test_search_top_k_larger_than_results(self):
        tokenizer = NgramTokenizer(n=3)
        idx = InvertedIndex()
        idx.add_document(0, tokenizer.tokenize("alpha beta"))
        idx.add_document(1, tokenizer.tokenize("gamma beta"))
        idx.finalize(stop_threshold=1.0)
        searcher = Searcher(idx, tokenizer=tokenizer)
        results = searcher.search("beta", top_k=100)
        assert len(results) == 2
