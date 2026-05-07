"""BM25-based retrieve algorithm."""

from __future__ import annotations

import functools
import heapq
import math
import struct
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np
from numpy.typing import NDArray


class NgramTokenizer:
    __slots__ = ("n",)

    def __init__(self, n: int = 2) -> None:
        self.n = n

    @staticmethod
    def normalize(text: str) -> str:
        return unicodedata.normalize("NFKC", text.lower())

    @staticmethod
    def _is_cjk(char: str) -> bool:
        cp = ord(char)
        return (
            (0x4E00 <= cp <= 0x9FFF)
            or (0xAC00 <= cp <= 0xD7AF)
            or (0x3040 <= cp <= 0x309F)
            or (0x30A0 <= cp <= 0x30FF)
            or (0x3400 <= cp <= 0x4DBF)
            or (0x20000 <= cp <= 0x2EBEF)
        )

    def _detect_n(self, text: str) -> int:
        if not text:
            return self.n
        cjk_count = 0
        threshold = len(text) * 3 // 10
        is_cjk = self._is_cjk
        for c in text:
            if is_cjk(c):
                cjk_count += 1
                if cjk_count > threshold:
                    return 2
        return 3 if self.n < 3 else self.n

    def tokenize(self, text: str, n: int | None = None) -> list[str]:
        text = self.normalize(text).strip()
        if not text:
            return []
        use_n = n if n is not None else self._detect_n(text)
        if len(text) < use_n:
            return [text]
        return [text[i : i + use_n] for i in range(len(text) - use_n + 1)]


class InvertedIndex:
    __slots__ = (
        "_term_to_id",
        "_temp_postings",
        "_doc_lengths",
        "_doc_lengths_arr",
        "_sum_doc_lengths",
        "_N",
        "_avgdl",
        "_posting_docs",
        "_posting_tfs",
        "_finalized",
        "_terms_by_length",
        "_terms_by_length_prefix",
        "_symmetric_delete_index",
    )

    _MAGIC = b"KIMX"
    _VERSION = 1

    def __init__(self) -> None:
        self._term_to_id: dict[str, int] = {}
        self._temp_postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._doc_lengths: list[int] = []
        self._doc_lengths_arr: NDArray[np.int32] = np.array([], dtype=np.int32)
        self._sum_doc_lengths: int = 0
        self._N: int = 0
        self._avgdl: float = 0.0
        self._posting_docs: list[NDArray[np.int32]] = []
        self._posting_tfs: list[NDArray[np.uint16]] = []
        self._finalized: bool = False
        self._terms_by_length: dict[int, tuple[str, ...]] = {}
        self._terms_by_length_prefix: dict[tuple[int, str], tuple[str, ...]] = {}
        self._symmetric_delete_index: dict[int, dict[str, tuple[str, ...]]] = {}

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
        if self._finalized:
            raise RuntimeError("Cannot add documents after finalize().")
        counter = Counter(tokens)
        self._doc_lengths.append(len(tokens))
        self._sum_doc_lengths += len(tokens)
        for token, freq in counter.items():
            if token not in self._term_to_id:
                self._term_to_id[token] = len(self._term_to_id)
            self._temp_postings[token].append((doc_id, freq))
        self._N = max(self._N, doc_id + 1)

    def _is_stop_ngram(self, token: str, df: int, threshold: float = 0.5) -> bool:
        if not token:
            return True
        if df > self._N * threshold:
            return True
        if all(unicodedata.category(c).startswith("P") for c in token):
            return True
        return False

    @staticmethod
    def _generate_deletes(term: str, max_edits: int) -> set[str]:
        """Generate all unique strings obtainable by deleting up to max_edits chars."""
        deletes: set[str] = {term}
        for _ in range(max_edits):
            new_deletes: set[str] = set()
            for t in deletes:
                for i in range(len(t)):
                    new_deletes.add(t[:i] + t[i + 1 :])
            deletes |= new_deletes
        return deletes

    def _build_symmetric_delete_index(self) -> None:
        """Build Symmetric Delete indices for max_edits 1 and 2."""
        if not self._term_to_id:
            self._symmetric_delete_index = {1: {}, 2: {}}
            return
        sd1: dict[str, list[str]] = defaultdict(list)
        sd2: dict[str, list[str]] = defaultdict(list)
        for term in self._term_to_id:
            for variant in self._generate_deletes(term, 1):
                if variant != term:
                    sd1[variant].append(term)
            for variant in self._generate_deletes(term, 2):
                if variant != term:
                    sd2[variant].append(term)
        self._symmetric_delete_index = {
            1: {k: tuple(v) for k, v in sd1.items()},
            2: {k: tuple(v) for k, v in sd2.items()},
        }

    def finalize(self, stop_threshold: float = 0.5, prune_df: int | None = None) -> None:
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
        by_len: dict[int, list[str]] = defaultdict(list)
        by_len_prefix: dict[tuple[int, str], list[str]] = defaultdict(list)
        for term in kept_terms:
            length = len(term)
            by_len[length].append(term)
            by_len_prefix[(length, term[:1])].append(term)
        self._terms_by_length = {length: tuple(terms) for length, terms in by_len.items()}
        self._terms_by_length_prefix = {key: tuple(terms) for key, terms in by_len_prefix.items()}
        if self._doc_lengths:
            self._avgdl = self._sum_doc_lengths / len(self._doc_lengths)
            self._doc_lengths_arr = np.array(self._doc_lengths, dtype=np.int32)
        self._build_symmetric_delete_index()
        self._temp_postings.clear()
        self._finalized = True

    def get_postings(
        self, term: str
    ) -> tuple[NDArray[np.int32], NDArray[np.uint16]] | None:
        if not self._finalized:
            self.finalize()
        tid = self._term_to_id.get(term)
        if tid is None:
            return None
        return self._posting_docs[tid], self._posting_tfs[tid]

    def doc_freq(self, term: str) -> int:
        postings = self.get_postings(term)
        if postings is None:
            return 0
        return len(postings[0])

    def has_term(self, term: str) -> bool:
        return term in self._term_to_id

    def terms(self) -> Iterable[str]:
        return self._term_to_id.keys()

    def save(self, path: str | Path) -> None:
        if not self._finalized:
            self.finalize()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        terms = list(self._term_to_id.keys())
        term_ids = self._term_to_id
        num_terms = len(terms)
        num_docs = len(self._doc_lengths)

        with open(path, "wb") as f:
            # Header: magic(4) + version(1) + N(4) + num_docs(4) + avgdl(8) + num_terms(4)
            f.write(self._MAGIC)
            f.write(struct.pack("<B", self._VERSION))
            f.write(struct.pack("<I", self._N))
            f.write(struct.pack("<I", num_docs))
            f.write(struct.pack("<d", self._avgdl))
            f.write(struct.pack("<I", num_terms))

            # Term table
            for term in terms:
                term_bytes = term.encode("utf-8")
                f.write(struct.pack("<H", len(term_bytes)))
                f.write(term_bytes)

            # Doc lengths
            if num_docs:
                f.write(np.array(self._doc_lengths, dtype=np.int32).tobytes())

            # Posting lists (in term_id order)
            for term in terms:
                tid = term_ids[term]
                docs = self._posting_docs[tid]
                tfs = self._posting_tfs[tid]
                df = len(docs)
                f.write(struct.pack("<I", df))
                if df:
                    f.write(docs.tobytes())
                    f.write(tfs.tobytes())

    def load(self, path: str | Path) -> None:
        path = Path(path)
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic != self._MAGIC:
                raise ValueError(f"Invalid file format: expected {self._MAGIC!r}, got {magic!r}")
            version = struct.unpack("<B", f.read(1))[0]
            if version != self._VERSION:
                raise ValueError(f"Unsupported version: {version}")

            self._N = struct.unpack("<I", f.read(4))[0]
            num_docs = struct.unpack("<I", f.read(4))[0]
            self._avgdl = struct.unpack("<d", f.read(8))[0]
            num_terms = struct.unpack("<I", f.read(4))[0]

            terms: list[str] = []
            term_to_id: dict[str, int] = {}
            for i in range(num_terms):
                term_len = struct.unpack("<H", f.read(2))[0]
                term = f.read(term_len).decode("utf-8")
                terms.append(term)
                term_to_id[term] = i

            self._term_to_id = term_to_id

            if num_docs:
                self._doc_lengths = np.frombuffer(f.read(num_docs * 4), dtype=np.int32).tolist()
            else:
                self._doc_lengths = []
            self._sum_doc_lengths = sum(self._doc_lengths)
            self._doc_lengths_arr = np.array(self._doc_lengths, dtype=np.int32)

            self._posting_docs = []
            self._posting_tfs = []
            for _ in range(num_terms):
                df = struct.unpack("<I", f.read(4))[0]
                if df:
                    docs = np.frombuffer(f.read(df * 4), dtype=np.int32).copy()
                    tfs = np.frombuffer(f.read(df * 2), dtype=np.uint16).copy()
                else:
                    docs = np.array([], dtype=np.int32)
                    tfs = np.array([], dtype=np.uint16)
                self._posting_docs.append(docs)
                self._posting_tfs.append(tfs)

        by_len: dict[int, list[str]] = defaultdict(list)
        by_len_prefix: dict[tuple[int, str], list[str]] = defaultdict(list)
        for term in self._term_to_id:
            length = len(term)
            by_len[length].append(term)
            by_len_prefix[(length, term[:1])].append(term)
        self._terms_by_length = {length: tuple(terms) for length, terms in by_len.items()}
        self._terms_by_length_prefix = {key: tuple(terms) for key, terms in by_len_prefix.items()}
        self._build_symmetric_delete_index()
        self._finalized = True


class BM25Scorer:
    __slots__ = ("index", "k1", "b", "_denom_base", "_tfs_buf", "_denom_buf")

    def __init__(
        self,
        index: InvertedIndex,
        k1: float = 1.2,
        b: float = 0.75,
    ) -> None:
        self.index = index
        self.k1 = k1
        self.b = b
        self._denom_base: NDArray[np.float64] | None = None
        self._tfs_buf: NDArray[np.float64] = np.empty(0, dtype=np.float64)
        self._denom_buf: NDArray[np.float64] = np.empty(0, dtype=np.float64)
        self._build_denom_base()

    def _build_denom_base(self) -> None:
        avgdl = self.index.avgdl
        if avgdl == 0:
            return
        k1 = self.k1
        b = self.b
        self._denom_base = k1 * ((1.0 - b) + (b / avgdl) * self.index.doc_lengths_arr)

    @staticmethod
    def _idf(df: int, N: int) -> float:
        return math.log(1 + (N - df + 0.5) / (df + 0.5))

    def _ensure_buffers(self, size: int) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
        if len(self._tfs_buf) < size:
            new_size = max(size * 2, 256)
            self._tfs_buf = np.empty(new_size, dtype=np.float64)
            self._denom_buf = np.empty(new_size, dtype=np.float64)
        return self._tfs_buf, self._denom_buf

    def _accumulate(
        self,
        query_tokens: list[str],
        candidate_docs: set[int] | None,
    ) -> np.ndarray:
        N = self.index.N
        scores_arr = np.zeros(N, dtype=np.float64)
        if N == 0 or self._denom_base is None:
            return scores_arr
        if candidate_docs is not None and len(candidate_docs) == 0:
            return scores_arr

        k1_plus_1 = self.k1 + 1.0
        denom_base = self._denom_base
        token_counts = Counter(query_tokens)

        if candidate_docs is not None:
            cand_sorted = np.array(sorted(candidate_docs), dtype=np.int32)
            for token, q_weight in token_counts.items():
                postings = self.index.get_postings(token)
                if postings is None:
                    continue
                docs, tfs = postings
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
                n = len(docs)
                tfs_buf, denom_buf = self._ensure_buffers(n)
                tfs_f = tfs_buf[:n]
                denom = denom_buf[:n]
                tfs_f[:] = tfs
                np.add(tfs_f, denom_base[docs], out=denom)
                tfs_f *= idf * k1_plus_1 * q_weight
                np.divide(tfs_f, denom, out=tfs_f)
                scores_arr[docs] += tfs_f
        else:
            for token, q_weight in token_counts.items():
                postings = self.index.get_postings(token)
                if postings is None:
                    continue
                docs, tfs = postings
                df = len(docs)
                idf = self._idf(df, N)
                n = len(docs)
                tfs_buf, denom_buf = self._ensure_buffers(n)
                tfs_f = tfs_buf[:n]
                denom = denom_buf[:n]
                tfs_f[:] = tfs
                np.add(tfs_f, denom_base[docs], out=denom)
                tfs_f *= idf * k1_plus_1 * q_weight
                np.divide(tfs_f, denom, out=tfs_f)
                scores_arr[docs] += tfs_f

        return scores_arr

    def _accumulate_sparse(
        self,
        query_tokens: list[str],
        candidate_docs: set[int] | None,
    ) -> dict[int, float]:
        scores: dict[int, float] = {}
        N = self.index.N
        if N == 0 or self._denom_base is None:
            return scores
        if candidate_docs is not None and len(candidate_docs) == 0:
            return scores

        k1_plus_1 = self.k1 + 1.0
        denom_base = self._denom_base
        token_counts = Counter(query_tokens)

        if candidate_docs is not None:
            cand_sorted = np.array(sorted(candidate_docs), dtype=np.int32)
            for token, q_weight in token_counts.items():
                postings = self.index.get_postings(token)
                if postings is None:
                    continue
                docs, tfs = postings
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
                n = len(docs)
                tfs_buf, denom_buf = self._ensure_buffers(n)
                tfs_f = tfs_buf[:n]
                denom = denom_buf[:n]
                tfs_f[:] = tfs
                np.add(tfs_f, denom_base[docs], out=denom)
                tfs_f *= idf * k1_plus_1 * q_weight
                np.divide(tfs_f, denom, out=tfs_f)
                for i in range(n):
                    d = int(docs[i])
                    scores[d] = scores.get(d, 0.0) + float(tfs_f[i])
        else:
            for token, q_weight in token_counts.items():
                postings = self.index.get_postings(token)
                if postings is None:
                    continue
                docs, tfs = postings
                df = len(docs)
                idf = self._idf(df, N)
                n = len(docs)
                tfs_buf, denom_buf = self._ensure_buffers(n)
                tfs_f = tfs_buf[:n]
                denom = denom_buf[:n]
                tfs_f[:] = tfs
                np.add(tfs_f, denom_base[docs], out=denom)
                tfs_f *= idf * k1_plus_1 * q_weight
                np.divide(tfs_f, denom, out=tfs_f)
                for i in range(n):
                    d = int(docs[i])
                    scores[d] = scores.get(d, 0.0) + float(tfs_f[i])

        return scores

    def score(
        self,
        query_tokens: list[str],
        candidate_docs: set[int] | None = None,
    ) -> dict[int, float]:
        N = self.index.N
        # Use sparse accumulation for large indices to avoid allocating huge zero arrays
        if N > 50000:
            return self._accumulate_sparse(query_tokens, candidate_docs)
        scores_arr = self._accumulate(query_tokens, candidate_docs)
        nonzero = np.flatnonzero(scores_arr)
        return {int(i): float(scores_arr[i]) for i in nonzero}

    def score_topk(
        self,
        query_tokens: list[str],
        top_k: int,
        candidate_docs: set[int] | None = None,
    ) -> list[tuple[int, float]]:
        if self.index.N == 0 or self._denom_base is None or top_k <= 0:
            return []

        N = self.index.N
        # Use sparse accumulation when N is large and top_k is small relative to N,
        # or when candidate_docs is small
        cand_size = len(candidate_docs) if candidate_docs is not None else N
        use_sparse = (N > 50000 and top_k < N // 20) or (cand_size < 5000 and N > 100000)

        if use_sparse:
            scores = self._accumulate_sparse(query_tokens, candidate_docs)
            if not scores:
                return []
            if top_k >= len(scores):
                return sorted(scores.items(), key=lambda x: (-x[1], x[0]))
            return heapq.nlargest(top_k, scores.items(), key=lambda x: (x[1], -x[0]))

        scores_arr = self._accumulate(query_tokens, candidate_docs)
        if top_k >= N:
            nonzero = np.flatnonzero(scores_arr)
            return [(int(i), float(scores_arr[i])) for i in nonzero]

        partitioned = np.argpartition(scores_arr, -top_k)[-top_k:]
        mask = scores_arr[partitioned] > 0
        top_indices = partitioned[mask]
        top_scores = scores_arr[top_indices]
        order = np.argsort(-top_scores)
        return [(int(top_indices[i]), float(top_scores[i])) for i in order]


class LevenshteinAutomaton:
    __slots__ = ("pattern", "max_edits", "prefix_length", "_pattern_counts", "_pattern_counts_items")

    def __init__(
        self,
        pattern: str,
        max_edits: int,
        prefix_length: int = 1,
    ) -> None:
        self.pattern = pattern
        self.max_edits = max_edits
        self.prefix_length = prefix_length
        pc: dict[str, int] = {}
        for c in pattern:
            pc[c] = pc.get(c, 0) + 1
        self._pattern_counts = pc
        self._pattern_counts_items = list(pc.items())

    @staticmethod
    def auto_fuzziness(term: str) -> int:
        length = len(term)
        if length <= 2:
            return 0
        if length <= 5:
            return 1
        return 2

    @staticmethod
    @functools.lru_cache(maxsize=65536)
    def _damerau_levenshtein(s: str, t: str) -> int:
        if len(s) < len(t):
            s, t = t, s
        m, n = len(s), len(t)
        if n == 0:
            return m

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
            si_1 = s[i - 1]
            for j in range(1, n + 1):
                cost = 0 if si_1 == t[j - 1] else 1
                curr[j] = min(
                    curr[j - 1] + 1,
                    prev[j] + 1,
                    prev[j - 1] + cost,
                )
                if (
                    i > 1
                    and j > 1
                    and si_1 == t[j - 2]
                    and s[i - 2] == t[j - 1]
                ):
                    curr[j] = min(curr[j], prev_prev[j - 2] + 1)
            prev_prev, prev, curr = prev, curr, prev_prev
        return prev[n]

    def _freq_lower_bound(self, term: str) -> int:
        total = 0
        matched = 0
        term_len = len(term)
        # For short terms, str.count in C is very fast; for longer terms, Counter avoids
        # repeated O(|term|) scans and wins when |alphabet| * |term| would be large.
        if term_len <= 32:
            for c, pc in self._pattern_counts_items:
                tc = term.count(c)
                matched += tc
                if pc != tc:
                    total += abs(pc - tc)
        else:
            tc = Counter(term)
            for c, pc in self._pattern_counts_items:
                tc_c = tc.get(c, 0)
                matched += tc_c
                if pc != tc_c:
                    total += abs(pc - tc_c)
        total += term_len - matched
        return (total + 1) // 2

    def match(self, dictionary: Iterable[str], max_expansions: int = 50) -> list[str]:
        results: list[str] = []
        pattern_len = len(self.pattern)
        max_edits = self.max_edits
        prefix_length = self.prefix_length
        prefix = self.pattern[:prefix_length] if prefix_length > 0 else ""
        dl = self._damerau_levenshtein
        has_freq_filter = len(self.pattern) <= 64

        # Use Symmetric Delete index for prefix_length == 1 when available
        if prefix_length == 1 and max_edits > 0 and hasattr(dictionary, "_symmetric_delete_index"):
            sd_index = dictionary._symmetric_delete_index  # type: ignore[attr-defined]
            sd = sd_index.get(max_edits) or sd_index.get(1, {})
            if sd:
                candidates: set[str] = set()
                for variant in InvertedIndex._generate_deletes(self.pattern, max_edits):
                    if variant in sd:
                        candidates.update(sd[variant])
                # Also include exact match
                if dictionary.has_term(self.pattern):  # type: ignore[attr-defined]
                    candidates.add(self.pattern)
                # Filter by prefix and length bounds, then verify
                for term in candidates:
                    if len(results) >= max_expansions:
                        return results
                    term_len = len(term)
                    if abs(term_len - pattern_len) > max_edits:
                        continue
                    if prefix and term[:1] != prefix:
                        continue
                    if has_freq_filter and self._freq_lower_bound(term) > max_edits:
                        continue
                    if dl(self.pattern, term) <= max_edits:
                        results.append(term)
                return results

        if hasattr(dictionary, "_terms_by_length_prefix"):
            terms_by_prefix = dictionary._terms_by_length_prefix  # type: ignore[attr-defined]
            for length in range(
                max(pattern_len - max_edits, prefix_length), pattern_len + max_edits + 1
            ):
                bucket = terms_by_prefix.get((length, prefix), ()) if prefix_length > 0 else ()
                if not bucket and prefix_length > 0:
                    continue
                candidates = bucket or dictionary._terms_by_length.get(length, ())  # type: ignore[attr-defined]
                if prefix_length == 1:
                    for term in candidates:
                        if len(results) >= max_expansions:
                            return results
                        if has_freq_filter and self._freq_lower_bound(term) > max_edits:
                            continue
                        if dl(self.pattern, term) <= max_edits:
                            results.append(term)
                else:
                    for term in candidates:
                        if len(results) >= max_expansions:
                            return results
                        if prefix_length > 0 and term[:prefix_length] != prefix:
                            continue
                        if has_freq_filter and self._freq_lower_bound(term) > max_edits:
                            continue
                        if dl(self.pattern, term) <= max_edits:
                            results.append(term)
                if len(results) >= max_expansions:
                    return results
            return results

        if hasattr(dictionary, "_terms_by_length"):
            terms_by_length = dictionary._terms_by_length  # type: ignore[attr-defined]
            for length in range(
                max(pattern_len - max_edits, prefix_length), pattern_len + max_edits + 1
            ):
                for term in terms_by_length.get(length, ()):
                    if len(results) >= max_expansions:
                        return results
                    if prefix_length == 1:
                        if term[0] != prefix[0]:
                            continue
                    elif prefix_length > 0 and term[:prefix_length] != prefix:
                        continue
                    if has_freq_filter and self._freq_lower_bound(term) > max_edits:
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
            if prefix_length == 1:
                if term_len >= 1 and term[0] != prefix[0]:
                    continue
            elif prefix_length > 0:
                if term_len >= prefix_length and term[:prefix_length] != prefix:
                    continue
            if has_freq_filter and self._freq_lower_bound(term) > max_edits:
                continue
            if dl(self.pattern, term) <= max_edits:
                results.append(term)
        return results


class Searcher:
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
        return bool(token) and all(ord(c) < 128 for c in token)

    def _expand_token(self, token: str) -> list[str]:
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
        if self.index.N == 0:
            return []

        query_tokens = self.tokenizer.tokenize(query)
        if not query_tokens:
            return []

        unique_query = list(dict.fromkeys(query_tokens))
        min_match = max(1, int(len(unique_query) * self.min_should_match))

        token_expansions: list[list[str]] = []
        hits = 0
        for token in unique_query:
            expanded = self._expand_token(token)
            if expanded:
                hits += 1
            token_expansions.append(expanded)

        if hits < min_match:
            return []

        # Pre-compute candidate_docs from posting intersections to avoid full scan
        candidate_docs: set[int] | None = None
        if min_match > 1 or (self.index.N > 50000 and min_match >= 1):
            doc_token_counts: dict[int, int] = {}
            for expanded in token_expansions:
                if not expanded:
                    continue
                seen_docs: set[int] = set()
                for term in expanded:
                    postings = self.index.get_postings(term)
                    if postings is None:
                        continue
                    docs, _ = postings
                    for d in docs:
                        d_int = int(d)
                        if d_int not in seen_docs:
                            seen_docs.add(d_int)
                            doc_token_counts[d_int] = doc_token_counts.get(d_int, 0) + 1
            if doc_token_counts:
                candidate_docs = {
                    doc_id for doc_id, count in doc_token_counts.items()
                    if count >= min_match
                }
            else:
                candidate_docs = set()

        expanded_tokens: list[str] = []
        for expanded in token_expansions:
            expanded_tokens.extend(expanded)

        if not expanded_tokens:
            return []

        if candidate_docs is not None and len(candidate_docs) == 0:
            return []

        expanded_tokens = list(dict.fromkeys(expanded_tokens))
        return self.scorer.score_topk(expanded_tokens, top_k=top_k, candidate_docs=candidate_docs)
