"""BM25-based retrieve algorithm (refactored from bm25.py)."""

from __future__ import annotations

import functools
import heapq
import math
import pickle
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
from numpy.typing import NDArray


class NgramTokenizer:
    """Overlapping n-gram generator with text normalization."""

    __slots__ = ("n",)

    def __init__(self, n: int = 2) -> None:
        self.n = n

    @staticmethod
    def normalize(text: str) -> str:
        """Lower-case and apply Unicode NFKC normalization."""
        return unicodedata.normalize("NFKC", text.lower())

    @staticmethod
    def _is_cjk(char: str) -> bool:
        cp = ord(char)
        return (
            (0x4E00 <= cp <= 0x9FFF)          # CJK Unified Ideographs
            or (0xAC00 <= cp <= 0xD7AF)       # Hangul Syllables
            or (0x3040 <= cp <= 0x309F)       # Hiragana
            or (0x30A0 <= cp <= 0x30FF)       # Katakana
            or (0x3400 <= cp <= 0x4DBF)       # Extension A
            or (0x20000 <= cp <= 0x2EBEF)     # Extensions B-F
        )

    def _detect_n(self, text: str) -> int:
        """Auto-detect n-gram size: bigram for CJK, trigram for mixed/code."""
        if not text:
            return self.n
        cjk_count = 0
        threshold = len(text) * 0.3
        is_cjk = self._is_cjk
        for c in text:
            if is_cjk(c):
                cjk_count += 1
                if cjk_count > threshold:
                    return 2
        return 3 if self.n < 3 else self.n

    def tokenize(self, text: str, n: int | None = None) -> list[str]:
        """Generate overlapping character n-grams from *text*."""
        text = self.normalize(text).strip()
        if not text:
            return []
        use_n = n if n is not None else self._detect_n(text)
        if len(text) < use_n:
            return [text]
        return [text[i : i + use_n] for i in range(len(text) - use_n + 1)]


class InvertedIndex:
    """Inverted index: build, persist, and load."""

    __slots__ = (
        "_term_to_id",
        "_temp_postings",
        "_doc_lengths",
        "_doc_lengths_arr",
        "_N",
        "_avgdl",
        "_posting_docs",
        "_posting_tfs",
        "_finalized",
        "_terms_by_length",
        "_terms_by_length_prefix",
    )

    def __init__(self) -> None:
        self._term_to_id: dict[str, int] = {}
        self._temp_postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._doc_lengths: list[int] = []
        self._doc_lengths_arr: NDArray[np.int32] = np.array([], dtype=np.int32)
        self._N: int = 0
        self._avgdl: float = 0.0
        # Finalized compact arrays
        self._posting_docs: list[NDArray[np.int32]] = []
        self._posting_tfs: list[NDArray[np.uint16]] = []
        self._finalized: bool = False
        self._terms_by_length: dict[int, tuple[str, ...]] = {}
        self._terms_by_length_prefix: dict[tuple[int, str], tuple[str, ...]] = {}

    @property
    def N(self) -> int:
        return self._N

    @property
    def avgdl(self) -> float:
        return self._avgdl

    @property
    def doc_lengths(self) -> list[int]:
        return self._doc_lengths

    @property
    def doc_lengths_arr(self) -> NDArray[np.int32]:
        return self._doc_lengths_arr

    def add_document(self, doc_id: int, tokens: list[str]) -> None:
        """Add a document's tokens to the index."""
        if self._finalized:
            raise RuntimeError("Cannot add documents after finalize().")
        counter = Counter(tokens)
        self._doc_lengths.append(len(tokens))
        for token, freq in counter.items():
            if token not in self._term_to_id:
                self._term_to_id[token] = len(self._term_to_id)
            self._temp_postings[token].append((doc_id, freq))
        self._N = max(self._N, doc_id + 1)

    def _is_stop_ngram(self, token: str, df: int, threshold: float = 0.5) -> bool:
        """Drop n-grams appearing in >*threshold* fraction of docs or pure punctuation."""
        if not token:
            return True
        if df > self._N * threshold:
            return True
        if all(unicodedata.category(c).startswith("P") for c in token):
            return True
        return False

    def finalize(self, stop_threshold: float = 0.5, prune_df: int | None = None) -> None:
        """Convert temporary postings to compact numpy arrays."""
        if self._finalized:
            return

        self._posting_docs = []
        self._posting_tfs = []
        kept_terms: dict[str, int] = {}

        for token, postings in self._temp_postings.items():
            df = len(postings)
            if self._is_stop_ngram(token, df, stop_threshold):
                continue
            if prune_df is not None and df > prune_df:
                continue
            tid = len(kept_terms)
            kept_terms[token] = tid
            if len(postings) == 1:
                doc_id, freq = postings[0]
                self._posting_docs.append(np.array([doc_id], dtype=np.int32))
                self._posting_tfs.append(np.array([freq], dtype=np.uint16))
            else:
                postings.sort(key=lambda p: p[0])
                self._posting_docs.append(
                    np.fromiter((p[0] for p in postings), dtype=np.int32, count=len(postings))
                )
                self._posting_tfs.append(
                    np.fromiter((p[1] for p in postings), dtype=np.uint16, count=len(postings))
                )

        self._term_to_id = kept_terms
        # Build length + prefix buckets for fast fuzzy expansion
        by_len: dict[int, list[str]] = defaultdict(list)
        by_len_prefix: dict[tuple[int, str], list[str]] = defaultdict(list)
        for term in kept_terms:
            length = len(term)
            by_len[length].append(term)
            by_len_prefix[(length, term[:1])].append(term)
        self._terms_by_length = {length: tuple(terms) for length, terms in by_len.items()}
        self._terms_by_length_prefix = {key: tuple(terms) for key, terms in by_len_prefix.items()}
        if self._doc_lengths:
            self._avgdl = sum(self._doc_lengths) / len(self._doc_lengths)
            self._doc_lengths_arr = np.array(self._doc_lengths, dtype=np.int32)
        self._temp_postings.clear()
        self._finalized = True

    def get_postings(
        self, term: str
    ) -> tuple[NDArray[np.int32], NDArray[np.uint16]] | None:
        """Return (doc_ids, term_frequencies) for *term*, or ``None``."""
        if not self._finalized:
            self.finalize()
        tid = self._term_to_id.get(term)
        if tid is None:
            return None
        return self._posting_docs[tid], self._posting_tfs[tid]

    def doc_freq(self, term: str) -> int:
        """Document frequency of *term*."""
        postings = self.get_postings(term)
        if postings is None:
            return 0
        return len(postings[0])

    def has_term(self, term: str) -> bool:
        return term in self._term_to_id

    def terms(self) -> Iterable[str]:
        return self._term_to_id.keys()

    def save(self, path: str | Path) -> None:
        """Persist the index to disk."""
        if not self._finalized:
            self.finalize()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(
                {
                    "term_to_id": self._term_to_id,
                    "posting_docs": self._posting_docs,
                    "posting_tfs": self._posting_tfs,
                    "doc_lengths": self._doc_lengths,
                    "N": self._N,
                    "avgdl": self._avgdl,
                },
                f,
            )

    def load(self, path: str | Path) -> None:
        """Load a persisted index from disk."""
        with open(path, "rb") as f:
            data = pickle.load(f)
        self._term_to_id = data["term_to_id"]
        self._posting_docs = data["posting_docs"]
        self._posting_tfs = data["posting_tfs"]
        self._doc_lengths = data["doc_lengths"]
        self._N = data["N"]
        self._avgdl = data["avgdl"]
        self._doc_lengths_arr = np.array(self._doc_lengths, dtype=np.int32)
        by_len: dict[int, list[str]] = defaultdict(list)
        by_len_prefix: dict[tuple[int, str], list[str]] = defaultdict(list)
        for term in self._term_to_id:
            length = len(term)
            by_len[length].append(term)
            by_len_prefix[(length, term[:1])].append(term)
        self._terms_by_length = {length: tuple(terms) for length, terms in by_len.items()}
        self._terms_by_length_prefix = {key: tuple(terms) for key, terms in by_len_prefix.items()}
        self._finalized = True


class BM25Scorer:
    """BM25 relevance scorer over an :class:`InvertedIndex`."""

    __slots__ = ("index", "k1", "b")

    def __init__(
        self,
        index: InvertedIndex,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        self.index = index
        self.k1 = k1
        self.b = b

    @staticmethod
    def _idf(df: int, N: int) -> float:
        """IDF = ln(1 + (N - df + 0.5) / (df + 0.5)) - Lucene BM25 variant."""
        return math.log(1 + (N - df + 0.5) / (df + 0.5))

    def score(
        self,
        query_tokens: list[str],
        candidate_docs: set[int] | None = None,
    ) -> dict[int, float]:
        """Accumulate BM25 score per candidate document.

        ``candidate_docs`` restricts scoring to a subset of docs; ``None``
        scores every document that has at least one query token.
        """
        N = self.index.N
        avgdl = self.index.avgdl
        if N == 0 or avgdl == 0:
            return {}

        doc_lengths = self.index.doc_lengths_arr
        k1 = self.k1
        b = self.b
        k1_plus_1 = k1 + 1.0
        one_minus_b = 1.0 - b
        b_over_avgdl = b / avgdl

        scores_arr = np.zeros(N, dtype=np.float64)

        if candidate_docs is not None:
            cand_sorted = np.array(sorted(candidate_docs), dtype=np.int32)
            for token in query_tokens:
                postings = self.index.get_postings(token)
                if postings is None:
                    continue
                docs, tfs = postings
                # Fast intersection for small candidate sets via searchsorted
                if len(cand_sorted) <= 256:
                    idx = np.searchsorted(cand_sorted, docs)
                    idx = np.clip(idx, 0, len(cand_sorted) - 1)
                    valid = cand_sorted[idx] == docs
                else:
                    valid = np.isin(docs, cand_sorted)
                if not np.any(valid):
                    continue
                docs = docs[valid]
                tfs = tfs[valid]
                df = len(docs)
                if df == 0:
                    continue
                idf = self._idf(df, N)
                dls = doc_lengths[docs]
                denom = tfs.astype(np.float64) + k1 * (one_minus_b + b_over_avgdl * dls)
                token_scores = idf * tfs.astype(np.float64) * k1_plus_1 / denom
                scores_arr[docs] += token_scores
        else:
            seen_tokens: set[str] = set()
            for token in query_tokens:
                if token in seen_tokens:
                    continue
                seen_tokens.add(token)
                postings = self.index.get_postings(token)
                if postings is None:
                    continue
                docs, tfs = postings
                df = len(docs)
                idf = self._idf(df, N)
                dls = doc_lengths[docs]
                denom = tfs.astype(np.float64) + k1 * (one_minus_b + b_over_avgdl * dls)
                token_scores = idf * tfs.astype(np.float64) * k1_plus_1 / denom
                scores_arr[docs] += token_scores

        nonzero = np.flatnonzero(scores_arr)
        return {int(i): float(scores_arr[i]) for i in nonzero}


class LevenshteinAutomaton:
    """Damerau-Levenshtein automaton for fuzzy term expansion."""

    __slots__ = ("pattern", "max_edits", "prefix_length")

    def __init__(
        self,
        pattern: str,
        max_edits: int,
        prefix_length: int = 1,
    ) -> None:
        self.pattern = pattern
        self.max_edits = max_edits
        self.prefix_length = prefix_length

    @staticmethod
    def auto_fuzziness(term: str) -> int:
        """AUTO mode: 0-2 chars -> 0, 3-5 -> 1, >5 -> 2."""
        length = len(term)
        if length <= 2:
            return 0
        if length <= 5:
            return 1
        return 2

    @staticmethod
    @functools.lru_cache(maxsize=8192)
    def _damerau_levenshtein(s: str, t: str) -> int:
        """Compute Damerau-Levenshtein distance between *s* and *t*."""
        m, n = len(s), len(t)
        if m < n:
            return LevenshteinAutomaton._damerau_levenshtein(t, s)
        if n == 0:
            return m

        # Fast paths for very short strings (common for n-grams)
        if n == 1:
            return 0 if s[0] == t[0] else 1
        if m == 2 and n == 2:
            if s == t:
                return 0
            if s[0] == t[0] or s[1] == t[1]:
                return 1
            if s[0] == t[1] and s[1] == t[0]:
                return 1
            return 2

        prev_prev = list(range(n + 1))
        prev = list(range(n + 1))
        curr = [0] * (n + 1)
        for i in range(1, m + 1):
            curr[0] = i
            for j in range(1, n + 1):
                cost = 0 if s[i - 1] == t[j - 1] else 1
                curr[j] = min(
                    curr[j - 1] + 1,      # insertion
                    prev[j] + 1,          # deletion
                    prev[j - 1] + cost,   # substitution
                )
                if (
                    i > 1
                    and j > 1
                    and s[i - 1] == t[j - 2]
                    and s[i - 2] == t[j - 1]
                ):
                    curr[j] = min(curr[j], prev_prev[j - 2] + 1)  # transposition
            prev_prev, prev, curr = prev, curr, prev_prev
        return prev[n]

    def match(self, dictionary: Iterable[str], max_expansions: int = 50) -> list[str]:
        """Walk *dictionary* and collect up to *max_expansions* matches."""
        results: list[str] = []
        pattern_len = len(self.pattern)
        max_edits = self.max_edits
        prefix_length = self.prefix_length
        prefix = self.pattern[:prefix_length] if prefix_length > 0 else ""
        dl = self._damerau_levenshtein

        # Fast-path: if dictionary has prefix buckets, exploit them
        if hasattr(dictionary, "_terms_by_length_prefix"):
            terms_by_prefix = dictionary._terms_by_length_prefix  # type: ignore[attr-defined]
            for length in range(
                max(pattern_len - max_edits, prefix_length), pattern_len + max_edits + 1
            ):
                bucket = terms_by_prefix.get((length, prefix), ()) if prefix_length > 0 else ()
                if not bucket and prefix_length > 0:
                    continue
                for term in bucket or dictionary._terms_by_length.get(length, ()):
                    if len(results) >= max_expansions:
                        return results
                    if prefix_length > 0 and term[:prefix_length] != prefix:
                        continue
                    if dl(self.pattern, term) <= max_edits:
                        results.append(term)
                if len(results) >= max_expansions:
                    return results
            return results

        # Fast-path: if dictionary has length buckets only
        if hasattr(dictionary, "_terms_by_length"):
            terms_by_length = dictionary._terms_by_length  # type: ignore[attr-defined]
            for length in range(
                max(pattern_len - max_edits, prefix_length), pattern_len + max_edits + 1
            ):
                for term in terms_by_length.get(length, ()):
                    if len(results) >= max_expansions:
                        return results
                    if prefix_length > 0 and term[:prefix_length] != prefix:
                        continue
                    if dl(self.pattern, term) <= max_edits:
                        results.append(term)
                if len(results) >= max_expansions:
                    return results
            return results

        for term in dictionary:
            if len(results) >= max_expansions:
                break
            term_len = len(term)
            if abs(term_len - pattern_len) > max_edits:
                continue
            if prefix_length > 0:
                if term_len >= prefix_length and term[:prefix_length] != prefix:
                    continue
            if dl(self.pattern, term) <= max_edits:
                results.append(term)
        return results


class Searcher:
    """Query pipeline orchestrator: normalize -> tokenize -> score -> rank."""

    __slots__ = (
        "index",
        "tokenizer",
        "scorer",
        "k1",
        "b",
        "min_should_match",
        "fuzziness",
        "max_expansions",
        "prefix_length",
    )

    def __init__(
        self,
        index: InvertedIndex,
        tokenizer: NgramTokenizer | None = None,
        scorer: BM25Scorer | None = None,
        k1: float = 1.2,
        b: float = 0.75,
        min_should_match: float = 0.5,
        fuzziness: str | int = "AUTO",
        max_expansions: int = 50,
        prefix_length: int = 1,
    ) -> None:
        self.index = index
        self.tokenizer = tokenizer or NgramTokenizer()
        self.scorer = scorer or BM25Scorer(index, k1=k1, b=b)
        self.k1 = k1
        self.b = b
        self.min_should_match = min_should_match
        self.fuzziness = fuzziness
        self.max_expansions = max_expansions
        self.prefix_length = prefix_length

    @staticmethod
    def _is_latin_token(token: str) -> bool:
        """Heuristic: token is primarily Latin/ASCII."""
        return bool(token) and all(ord(c) < 128 for c in token)

    def _expand_token(self, token: str) -> list[str]:
        """Fuzzy-expand a Latin token; CJK tokens are returned verbatim if present."""
        if not self._is_latin_token(token):
            return [token] if self.index.has_term(token) else []

        max_edits = (
            LevenshteinAutomaton.auto_fuzziness(token)
            if self.fuzziness == "AUTO"
            else int(self.fuzziness)
        )
        if max_edits == 0:
            return [token] if self.index.has_term(token) else []

        automaton = LevenshteinAutomaton(
            token, max_edits=max_edits, prefix_length=self.prefix_length
        )
        matches = automaton.match(
            self.index, max_expansions=self.max_expansions
        )
        return matches if matches else ([token] if self.index.has_term(token) else [])

    def search(self, query: str, top_k: int = 10) -> list[tuple[int, float]]:
        """Run the full query pipeline and return top-k *(doc_id, score)* pairs."""
        if self.index.N == 0:
            return []

        query_tokens = self.tokenizer.tokenize(query)
        if not query_tokens:
            return []

        # Expand tokens and enforce minimum-match
        expanded_tokens: list[str] = []
        unique_query = list(dict.fromkeys(query_tokens))
        hits = 0
        for token in unique_query:
            expanded = self._expand_token(token)
            if expanded:
                hits += 1
            expanded_tokens.extend(expanded)

        min_match = max(1, int(len(unique_query) * self.min_should_match))
        if hits < min_match:
            return []

        if not expanded_tokens:
            return []

        scores = self.scorer.score(expanded_tokens)
        if not scores:
            return []

        if top_k * 10 < len(scores):
            ranked = heapq.nlargest(top_k, scores.items(), key=lambda x: x[1])
        else:
            ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]
