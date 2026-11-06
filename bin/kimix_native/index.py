"""kimix_native.index — index kernels: ngram tokenizer + incremental index.

Native implementations live in ``runtime_py.index`` (compiled kernels, GIL
released, bytes in/out). The pure-Python ``_compat`` functions below mirror
the same contract (retrieval.py::NgramTokenizer + InvertedIndex semantics,
with the plan 004 incremental design).

Encoding contract (like text.py): native kernels are bytes-in/bytes-out;
strings are encoded with ``errors="surrogatepass"`` so lone surrogates reach
the kernel unchanged. The NFKC hook lives here: the C++ ``normalize``
implements the ASCII-lowercase fast path only (non-ASCII bytes pass through
unchanged); for non-ASCII text this shim composes ``.lower()`` + NFKC —
which is byte-exact with the reference's ``text.lower()`` then
``unicodedata.normalize("NFKC", ...)`` for every input.
"""

from __future__ import annotations

import unicodedata

from . import _native, use_native
from ._common import _enc, _dec

# ---------------------------------------------------------------------------
# _compat — pure-Python mirrors (reference algorithms + plan 004 contract)
# ---------------------------------------------------------------------------

# The 16 CJK ranges of retrieval.py::NgramTokenizer._is_cjk.
_CJK_RANGES_16 = (
    (0x4E00, 0x9FFF),      # CJK Unified Ideographs
    (0xAC00, 0xD7AF),      # Hangul Syllables
    (0x3040, 0x309F),      # Hiragana
    (0x30A0, 0x30FF),      # Katakana
    (0x3400, 0x4DBF),      # Extension A
    (0x20000, 0x2EBEF),    # Extensions B-F
    (0xF900, 0xFAFF),      # CJK Compatibility Ideographs
    (0x2F800, 0x2FA1F),    # CJK Compatibility Ideographs Supplement
    (0x30000, 0x3134F),    # Extension G
    (0x31350, 0x323AF),    # Extension H
    (0x2EBF0, 0x2EE5F),    # Extension I
    (0x1100, 0x11FF),      # Hangul Jamo
    (0xA960, 0xA97F),      # Hangul Jamo Extended-A
    (0xD7B0, 0xD7FF),      # Hangul Jamo Extended-B
    (0x31C0, 0x31EF),      # CJK Strokes
    (0x3200, 0x32FF),      # Enclosed CJK Letters and Months
)


def _compat_is_cjk_cp(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES_16)


class _CompatNgramTokenizer:
    """Exact mirror of retrieval.py::NgramTokenizer (bytes-level contract:
    tokenize operates on the normalized string like the reference)."""

    __slots__ = ("n",)

    def __init__(self, n: int = 2) -> None:
        self.n = n

    def normalize(self, text: str) -> str:
        lowered = text.lower()
        if lowered.isascii():
            return lowered
        return unicodedata.normalize("NFKC", lowered)

    def _detect_n(self, text: str) -> int:
        if not text:
            return self.n
        if text.isascii():
            return 3 if self.n < 3 else self.n
        cjk_count = 0
        threshold = len(text) * 3 // 10
        for c in text:
            if _compat_is_cjk_cp(ord(c)):
                cjk_count += 1
                if cjk_count > threshold:
                    return 2
        return 3 if self.n < 3 else self.n

    def detect_n(self, text: str) -> int:
        return self._detect_n(text)

    def _tokenize_impl(self, text: str, n: int):
        if len(text) < n:
            return (text,)
        return tuple(text[i : i + n] for i in range(len(text) - n + 1))

    def tokenize(self, text: str, n: int | None = None) -> list[str]:
        text = self.normalize(text).strip()
        if not text:
            return []
        use_n = n if n is not None else self._detect_n(text)
        return list(self._tokenize_impl(text, use_n))


class _CompatInvertedIndex:
    """Pure-Python mirror of the plan 004 INCREMENTAL InvertedIndex contract
    (delta buffer + immutable segments + k-way merge). Deliberately mirrors
    the native contract: add_document is always allowed (even after
    finalize), no stop-ngram pruning, per-term postings sorted by doc_id."""

    __slots__ = ("_delta", "_doc_lengths", "_segments", "_max_doc_id",
                 "_sum_doc_lengths", "_finalized")

    def __init__(self) -> None:
        self._delta: dict[str, list[tuple[int, int]]] = {}
        self._doc_lengths: dict[int, int] = {}
        self._segments: list[tuple[list[str], dict[str, list[tuple[int, int]]]]] = []
        self._max_doc_id = 0
        self._sum_doc_lengths = 0
        self._finalized = False

    # -- public API (mirrors the native binding) --
    def add_document(self, doc_id: int, tokens: list[str]) -> None:
        from collections import Counter

        counter = Counter(tokens)
        if doc_id in self._doc_lengths:
            self._sum_doc_lengths += len(tokens) - self._doc_lengths[doc_id]
        else:
            self._sum_doc_lengths += len(tokens)
        self._doc_lengths[doc_id] = len(tokens)
        self._max_doc_id = max(self._max_doc_id, doc_id)
        for token, freq in counter.items():
            self._delta.setdefault(token, []).append((doc_id, freq))

    def finalize(self) -> None:
        if not self._delta:
            self._finalized = True
            return
        terms: dict[str, list[tuple[int, int]]] = {}
        for term, pl in self._delta.items():
            terms[term] = sorted(pl, key=lambda e: e[0])
        keys = sorted(terms)
        self._segments.append((keys, terms))
        self._delta.clear()
        self._finalized = True

    def finalized(self) -> bool:
        return self._finalized

    def get_postings(self, term: str):
        pl: list[tuple[int, int]] = []
        if term in self._delta:
            pl.extend(self._delta[term])
        for _keys, terms in self._segments:
            if term in terms:
                pl.extend(terms[term])
        if not pl:
            return None
        pl.sort(key=lambda e: e[0])
        return [(int(d), int(tf)) for d, tf in pl]

    def has_term(self, term: str) -> bool:
        return term in self._delta or any(term in terms for _k, terms in self._segments)

    def doc_count(self) -> int:
        return len(self._doc_lengths)

    def max_doc_id(self) -> int:
        return self._max_doc_id

    def doc_length(self, doc_id: int) -> int:
        return self._doc_lengths.get(doc_id, 0)

    def sum_doc_lengths(self) -> int:
        return self._sum_doc_lengths

    def avg_doc_len(self) -> float:
        n = self.doc_count()
        return 0.0 if n == 0 else self._sum_doc_lengths / n

    def total_postings(self) -> int:
        total = sum(len(pl) for pl in self._delta.values())
        for _k, terms in self._segments:
            total += sum(len(pl) for pl in terms.values())
        return total

    def segment_count(self) -> int:
        return len(self._segments)

    def compact(self) -> None:
        if len(self._segments) <= 1:
            return
        merged: dict[str, list[tuple[int, int]]] = {}
        for _k, terms in self._segments:
            for term, pl in terms.items():
                merged.setdefault(term, []).extend(pl)
        for term in merged:
            merged[term].sort(key=lambda e: e[0])
        keys = sorted(merged)
        self._segments = [(keys, merged)]
        self._finalized = True

    def save(self) -> bytes:
        # Mirror the KNIDX1 blob produced by the native kernel so the shim
        # round-trips too. Format (little-endian):
        #   "KNIDX1" | u32 doc_count | u32 max_doc_id | u64 sum_doc_lengths
        #   | u32 meta_count + (u32 doc_id, u32 len)*
        #   | u32 seg_count | per segment: u32 term_count,
        #     (u32 len + bytes)*, (u32 offset)*(term_count+1), u32 pc, (u32,u32)*
        # NOTE: this layout is pinned to the C++ kernel (KNIDX1), so the
        # stdlib ``struct`` module is deliberately retained here instead of
        # msgspec — msgspec's Struct encoding cannot express the variable-
        # length term/postings sections of this wire format.
        import struct

        self.finalize()
        out = bytearray()
        out += b"KNIDX1"
        meta = sorted(self._doc_lengths.items())
        out += struct.pack("<IIQ", len(meta), self._max_doc_id, self._sum_doc_lengths)
        out += struct.pack("<I", len(meta))
        for doc_id, length in meta:
            out += struct.pack("<II", doc_id, length)
        out += struct.pack("<I", len(self._segments))
        for keys, terms in self._segments:
            out += struct.pack("<I", len(keys))
            for term in keys:
                b = term.encode("utf-8", "surrogatepass")
                out += struct.pack("<I", len(b)) + b
            offsets = [0]
            for term in keys:
                offsets.append(offsets[-1] + len(terms[term]))
            for off in offsets:
                out += struct.pack("<I", off)
            out += struct.pack("<I", offsets[-1])
            for term in keys:
                for doc_id, tf in terms[term]:
                    out += struct.pack("<II", doc_id, tf)
        return bytes(out)

    def load(self, blob: bytes) -> bool:
        import struct

        self._delta = {}
        self._doc_lengths = {}
        self._segments = []
        self._max_doc_id = 0
        self._sum_doc_lengths = 0
        self._finalized = False
        try:
            if blob[:6] != b"KNIDX1":
                return False
            pos = 6
            doc_count, max_doc_id, sum_lengths = struct.unpack_from("<IIQ", blob, pos)
            pos += 16
            meta_count, = struct.unpack_from("<I", blob, pos)
            pos += 4
            if meta_count != doc_count:
                return False
            for _ in range(meta_count):
                doc_id, length = struct.unpack_from("<II", blob, pos)
                pos += 8
                self._doc_lengths[doc_id] = length
            self._max_doc_id = max_doc_id
            self._sum_doc_lengths = sum_lengths
            seg_count, = struct.unpack_from("<I", blob, pos)
            pos += 4
            for _ in range(seg_count):
                term_count, = struct.unpack_from("<I", blob, pos)
                pos += 4
                keys: list[str] = []
                for _ in range(term_count):
                    (blen,) = struct.unpack_from("<I", blob, pos)
                    pos += 4
                    keys.append(blob[pos : pos + blen].decode("utf-8", "surrogatepass"))
                    pos += blen
                offsets = list(struct.unpack_from("<%dI" % (term_count + 1), blob, pos))
                pos += 4 * (term_count + 1)
                pc, = struct.unpack_from("<I", blob, pos)
                pos += 4
                if pc != offsets[-1]:
                    return False
                terms: dict[str, list[tuple[int, int]]] = {}
                for i, term in enumerate(keys):
                    start, end = offsets[i], offsets[i + 1]
                    pl = []
                    for _ in range(end - start):
                        doc_id, tf = struct.unpack_from("<II", blob, pos)
                        pos += 8
                        pl.append((doc_id, tf))
                    terms[term] = pl
                self._segments.append((keys, terms))
            self._finalized = True
            return True
        except (struct.error, UnicodeDecodeError):
            return False

    def reset(self) -> None:
        self._delta = {}
        self._doc_lengths = {}
        self._segments = []
        self._max_doc_id = 0
        self._sum_doc_lengths = 0
        self._finalized = False

    def __repr__(self) -> str:
        return f"<CompatInvertedIndex docs={self.doc_count()} segments={len(self._segments)}>"


# ---------------------------------------------------------------------------
# Public API (native with _compat fallback)
# ---------------------------------------------------------------------------


class NgramTokenizer:
    """Overlapping n-gram tokenizer (native; _compat fallback)."""

    def __init__(self, default_n: int = 2) -> None:
        self.default_n = default_n
        self._native = None
        if use_native("INDEX") and _native is not None:
            self._native = _native.index.NgramTokenizer(default_n)

    def normalize(self, text: str) -> str:
        """Lower-case + NFKC (byte-exact with the reference for every input).
        The kernel does the ASCII fast path; non-ASCII text gets .lower() +
        NFKC composed here."""
        if self._native is not None:
            out = self._native.normalize(_enc(text))
            if not out.isascii():
                out = unicodedata.normalize("NFKC", out.decode("utf-8", "surrogatepass").lower()).encode(
                    "utf-8", "surrogatepass"
                )
            return out.decode("utf-8", "surrogatepass")
        return _CompatNgramTokenizer(self.default_n).normalize(text)

    def detect_n(self, text: str) -> int:
        if self._native is not None:
            return int(self._native.detect_n(_enc(text)))
        return _CompatNgramTokenizer(self.default_n).detect_n(text)

    def tokenize(self, text: str, n: int | None = None) -> list[str]:
        """Normalize -> strip -> auto-detect n -> overlapping n-grams."""
        if self._native is not None:
            norm = self.normalize(text).strip()
            if not norm:
                return []
            use_n = n if n is not None else self.detect_n(norm)
            grams = self._native.tokenize(_enc(norm), int(use_n))
            return [_dec(g) for g in grams]
        return _CompatNgramTokenizer(self.default_n).tokenize(text, n)


class InvertedIndex:
    """Incremental inverted index (native; _compat fallback)."""

    def __init__(self) -> None:
        self._native = None
        if use_native("INDEX") and _native is not None:
            self._native = _native.index.InvertedIndex()
        else:
            self._compat = _CompatInvertedIndex()

    def add_document(self, doc_id: int, tokens: list[str]) -> None:
        if self._native is not None:
            self._native.add_document(int(doc_id), [_enc(t) for t in tokens])
        else:
            self._compat.add_document(int(doc_id), tokens)

    def finalize(self) -> None:
        if self._native is not None:
            self._native.finalize()
        else:
            self._compat.finalize()

    def finalized(self) -> bool:
        if self._native is not None:
            return bool(self._native.finalized())
        return self._compat.finalized()

    def get_postings(self, term: str):
        """Merged (doc_id, tf) postings for *term*, or None."""
        if self._native is not None:
            pl = self._native.get_postings(_enc(term))
            if pl is None:
                return None
            return [(int(d), int(tf)) for d, tf in pl]
        return self._compat.get_postings(term)

    def has_term(self, term: str) -> bool:
        if self._native is not None:
            return bool(self._native.has_term(_enc(term)))
        return self._compat.has_term(term)

    def doc_count(self) -> int:
        if self._native is not None:
            return int(self._native.doc_count())
        return self._compat.doc_count()

    def max_doc_id(self) -> int:
        if self._native is not None:
            return int(self._native.max_doc_id())
        return self._compat.max_doc_id()

    def doc_length(self, doc_id: int) -> int:
        if self._native is not None:
            return int(self._native.doc_length(int(doc_id)))
        return self._compat.doc_length(int(doc_id))

    def sum_doc_lengths(self) -> int:
        if self._native is not None:
            return int(self._native.sum_doc_lengths())
        return self._compat.sum_doc_lengths()

    def avg_doc_len(self) -> float:
        if self._native is not None:
            return float(self._native.avg_doc_len())
        return self._compat.avg_doc_len()

    def total_postings(self) -> int:
        if self._native is not None:
            return int(self._native.total_postings())
        return self._compat.total_postings()

    def segment_count(self) -> int:
        if self._native is not None:
            return int(self._native.segment_count())
        return self._compat.segment_count()

    def compact(self) -> None:
        if self._native is not None:
            self._native.compact()
        else:
            self._compat.compact()

    def save(self) -> bytes:
        if self._native is not None:
            return bytes(self._native.save())
        return self._compat.save()

    def load(self, blob: bytes) -> bool:
        if self._native is not None:
            return bool(self._native.load(bytes(blob)))
        return self._compat.load(bytes(blob))

    def reset(self) -> None:
        if self._native is not None:
            self._native.reset()
        else:
            self._compat.reset()


# ---------------------------------------------------------------------------
# HistoryIndex — turn-metadata BM25 index (plan 006)
# ---------------------------------------------------------------------------

_ROLE_TO_INT = {"user": 0, "assistant": 1, "tool": 2}
_INT_TO_ROLE = {0: "user", 1: "assistant", 2: "tool", 3: "other"}


def _role_to_int(role) -> int:
    if isinstance(role, str):
        return _ROLE_TO_INT.get(role, 3)
    return int(role)


def _normalize_text(text: str) -> str:
    """Reference NgramTokenizer.normalize: lower() then NFKC when non-ASCII.
    The native kernel implements only the ASCII fast path, so the shim
    pre-normalizes (and strips) every text before handing it to the kernel."""
    lowered = text.lower()
    if lowered.isascii():
        return lowered
    return unicodedata.normalize("NFKC", lowered)


def _parse_ref(ref) -> int | None:
    """Reference get_by_id ref parsing: '42', 'prune_42', or an int."""
    ref_str = str(ref)
    if ref_str.startswith("prune_"):
        ref_str = ref_str[len("prune_"):]
    try:
        return int(ref_str)
    except ValueError:
        return None


class _CompatHistoryIndex:
    """Pure-Python mirror of the native HistoryIndex contract: reference
    history_index.py semantics (doc_id == turn_id, the index is never pruned
    on eviction, _MAX_TURNS = 500) + the plan 006 KNHIX1 blob. Turn text is
    stored pre-normalized (like the native kernel) so blobs and get_by_id
    are byte-consistent between the two implementations."""

    _MAX_TURNS = 500

    def __init__(self) -> None:
        self._turns: list[dict] = []
        self._tokenizer = _CompatNgramTokenizer(n=2)
        self._index = _CompatInvertedIndex()

    def append_turns(self, turns) -> None:
        for turn_id, timestamp, role, is_compacted, text in turns:
            # Store the same pre-normalized + stripped text the native kernel
            # receives (blobs and get_by_id stay byte-consistent).
            stripped = _normalize_text(text).strip()
            if not stripped:
                continue
            tokens = self._tokenizer.tokenize(stripped)
            self._turns.append({
                "turn_id": int(turn_id),
                "timestamp": float(timestamp),
                "role": _role_to_int(role),
                "text": stripped,
                "is_compacted": bool(is_compacted),
            })
            self._index.add_document(int(turn_id), tokens)
        while len(self._turns) > self._MAX_TURNS:
            self._turns.pop(0)

    def mark_compacted(self) -> None:
        for turn in self._turns:
            turn["is_compacted"] = True

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        from .search import _compat_bm25_idf, _compat_bm25_score, _compat_top_k

        if not self._turns:
            return []
        if not self._index.finalized():
            self._index.finalize()
        tokens = self._tokenizer.tokenize(query)
        postings = [self._index.get_postings(t) or [] for t in tokens]
        doc_count = self._index.max_doc_id() + 1
        idf = [_compat_bm25_idf(doc_count, len(pl)) for pl in postings]
        doc_lengths = [self._index.doc_length(i) for i in range(doc_count)]
        scores = _compat_bm25_score(postings, idf, doc_lengths,
                                    self._index.avg_doc_len(), doc_count)
        out: list[dict] = []
        for doc_id in _compat_top_k(scores, top_k):
            for turn in self._turns:
                if turn["turn_id"] == doc_id:
                    out.append({**turn, "score": scores[doc_id]})
                    break
        # Reference-shaped output: role as str (stored internally as int).
        for t in out:
            t["role"] = _INT_TO_ROLE.get(int(t["role"]), "other")
        return out

    def get_by_id(self, turn_id: int):
        for turn in self._turns:
            if turn["turn_id"] == turn_id:
                d = dict(turn)
                d["role"] = _INT_TO_ROLE.get(int(d["role"]), "other")
                return d
        return None

    def save(self) -> bytes:
        # Mirrors the native KNHIX1 blob byte-for-byte (the KNIDX1 payload is
        # produced by _CompatInvertedIndex.save, which test_index.py verifies
        # matches the native kernel's).
        import struct

        out = bytearray()
        out += b"KNHIX1"
        out += struct.pack("<II", len(self._turns), self._index.doc_count())
        for t in self._turns:
            flags = 1 if t["is_compacted"] else 0
            out += struct.pack("<IdBB", t["turn_id"], t["timestamp"], t["role"], flags)
            b = t["text"].encode("utf-8", "surrogatepass")
            out += struct.pack("<I", len(b)) + b
        out += self._index.save()
        return bytes(out)

    def load(self, blob: bytes) -> bool:
        import struct

        try:
            if blob[:6] != b"KNHIX1":
                return False
            pos = 6
            turn_count, doc_count = struct.unpack_from("<II", blob, pos)
            pos += 8
            turns = []
            for _ in range(turn_count):
                tid, ts = struct.unpack_from("<Id", blob, pos)
                pos += 12
                role, flags = struct.unpack_from("<BB", blob, pos)
                pos += 2
                (tlen,) = struct.unpack_from("<I", blob, pos)
                pos += 4
                text = blob[pos : pos + tlen].decode("utf-8", "surrogatepass")
                pos += tlen
                turns.append({
                    "turn_id": int(tid),
                    "timestamp": float(ts),
                    "role": int(role),
                    "text": text,
                    "is_compacted": bool(flags & 1),
                })
            idx = _CompatInvertedIndex()
            if not idx.load(blob[pos:]):
                return False
            if idx.doc_count() != doc_count:
                return False
            self._turns = turns
            self._index = idx
            return True
        except (struct.error, UnicodeDecodeError, IndexError):
            return False

    def turn_count(self) -> int:
        return len(self._turns)

    def pop_front(self) -> None:
        if self._turns:
            self._turns.pop(0)

    def reset(self) -> None:
        self._turns = []
        self._index = _CompatInvertedIndex()

    def __repr__(self) -> str:
        return f"<CompatHistoryIndex turns={len(self._turns)}>"


class HistoryIndex:
    """Turn-metadata BM25 index over conversation turns (native
    ``runtime_py.index.HistoryIndex``; ``_compat`` fallback).

    Reference-shaped API (history_index.py semantics):

      append_turns([(turn_id, timestamp, role, is_compacted, text), ...])
      search(query, top_k=3) -> list[dict]  # turn_id/timestamp/role/text/
                                            # is_compacted/score
      mark_compacted(); get_by_id(ref); save() -> bytes; load(bytes) -> bool
      turn_count(); pop_front(); reset()

    ``role`` may be a str ('user'/'assistant'/'tool'/'other') or int 0..3.
    Text is pre-normalized (lower + NFKC) and stripped before indexing — the
    native kernel implements only the ASCII normalize fast path. Turns whose
    text is empty after stripping are skipped (reference
    ``if not text.strip(): continue``).
    """

    def __init__(self, persist_path=None) -> None:
        self._persist_path = persist_path
        self._native = None
        if use_native("INDEX") and _native is not None:
            self._native = _native.index.HistoryIndex()
        else:
            self._compat = _CompatHistoryIndex()

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _role_int(role) -> int:
        return _role_to_int(role)

    @staticmethod
    def _role_str(role) -> str:
        return _INT_TO_ROLE.get(int(role), "other")

    @staticmethod
    def _to_turn_dict(d: dict, with_score: bool) -> dict:
        text = d["text"]
        if isinstance(text, bytes):
            text = _dec(text)
        out = {
            "turn_id": int(d["turn_id"]),
            "timestamp": float(d["timestamp"]),
            "role": HistoryIndex._role_str(d["role"]),
            "text": text,
            "is_compacted": bool(d["is_compacted"]),
        }
        if with_score:
            out["score"] = float(d["score"])
        return out

    @staticmethod
    def _normalized(turns):
        """Yield (turn_id, timestamp, role_int, is_compacted, norm_text) for
        turns whose text is non-empty after normalize + strip."""
        for turn_id, timestamp, role, is_compacted, text in turns:
            stripped = _normalize_text(text).strip()
            if not stripped:
                continue
            yield (int(turn_id), float(timestamp),
                   HistoryIndex._role_int(role), bool(is_compacted), stripped)

    # -- turns -------------------------------------------------------------
    def append_turns(self, turns) -> None:
        if self._native is not None:
            norm = list(self._normalized(turns))
            if norm:
                self._native.append_turns(
                    [(t, ts, r, c, _enc(text)) for t, ts, r, c, text in norm]
                )
        else:
            self._compat.append_turns(self._normalized(turns))

    def mark_compacted(self) -> None:
        if self._native is not None:
            self._native.mark_compacted()
        else:
            self._compat.mark_compacted()

    def set_persist_path(self, path) -> None:
        self._persist_path = path
        if self._native is not None:
            self._native.set_persist_path(_enc(str(path)))

    # -- search ------------------------------------------------------------
    def search(self, query: str, top_k: int = 3) -> list[dict]:
        if self._native is not None:
            q = _enc(_normalize_text(query).strip())
            raw = self._native.search(q, int(top_k))
            return [self._to_turn_dict(d, with_score=True) for d in raw]
        # _compat already returns reference-shaped dicts (str role, str text).
        return self._compat.search(query, int(top_k))

    def get_by_id(self, ref):
        turn_id = _parse_ref(ref)
        if turn_id is None:
            return None
        if self._native is not None:
            d = self._native.get_by_id(turn_id)
            return None if d is None else self._to_turn_dict(d, with_score=False)
        return self._compat.get_by_id(turn_id)

    # -- persistence -------------------------------------------------------
    def save(self) -> bytes:
        if self._native is not None:
            return bytes(self._native.save())
        return self._compat.save()

    def load(self, blob: bytes) -> bool:
        if self._native is not None:
            return bool(self._native.load(bytes(blob)))
        return self._compat.load(bytes(blob))

    # -- misc --------------------------------------------------------------
    def turn_count(self) -> int:
        if self._native is not None:
            return int(self._native.turn_count())
        return self._compat.turn_count()

    def pop_front(self) -> None:
        if self._native is not None:
            self._native.pop_front()
        else:
            self._compat.pop_front()

    def reset(self) -> None:
        if self._native is not None:
            self._native.reset()
        else:
            self._compat.reset()


# ---------------------------------------------------------------------------
# Durable FTS5 backend (report C2 needs it reachable from Python)
# ---------------------------------------------------------------------------
# Turn blobs are packed, never list[tuple]: the append path of a prune pass
# carries every elided turn's text, and a tuple list would rebuild the whole
# Python object graph (report §2).
import struct as _struct  # noqa: E402  (kept local to this section)

_TURN_IN = _struct.Struct("<IdBBI")   # u32 id, f64 ts, u8 role, u8 flags, u32 len
_HIT_OUT = _struct.Struct("<IdBBddI")  # + f64 score, f64 boosted, u32 len
_U32 = _struct.Struct("<I")

ROLE_TO_INT = {"user": 0, "assistant": 1, "tool": 2}
INT_TO_ROLE = ("user", "assistant", "tool", "other")


def pack_turns(turns) -> bytes:
    """``u32 count | {u32 id, f64 ts, u8 role, u8 flags, u32 len, text}``.

    ``turns`` items may be dicts (``id``/``turn_id``, ``timestamp``/``ts``,
    ``role``, ``is_compacted``, ``text``) or 5-tuples.
    """
    out = bytearray(_U32.pack(len(turns)))
    for turn in turns:
        if isinstance(turn, dict):
            turn_id = int(turn.get("turn_id", turn.get("id", 0)))
            ts = float(turn.get("timestamp", turn.get("ts", 0.0)))
            role = turn.get("role", "other")
            role_id = ROLE_TO_INT.get(role, 3) if isinstance(role, str) else int(role)
            flags = 1 if turn.get("is_compacted") else 0
            text = _enc(turn.get("text") or "")
        else:
            turn_id, ts, role_id, flags, text = turn
            text = _enc(text) if isinstance(text, str) else text
        out += _TURN_IN.pack(turn_id, ts, role_id, flags, len(text))
        out += text
    return bytes(out)


def unpack_turns(blob: bytes) -> list[dict]:
    view = memoryview(blob)
    if len(view) < 4:
        return []
    (count,) = _U32.unpack_from(view, 0)
    offset = 4
    out = []
    for _ in range(count):
        turn_id, ts, role_id, flags, length = _TURN_IN.unpack_from(view, offset)
        offset += _TURN_IN.size
        text = bytes(view[offset:offset + length])
        offset += length
        out.append({
            "turn_id": turn_id,
            "timestamp": ts,
            "role": INT_TO_ROLE[role_id] if role_id < 4 else "other",
            "is_compacted": bool(flags & 1),
            "text": _dec(text),
        })
    return out


def unpack_hits(blob: bytes) -> list[dict]:
    view = memoryview(blob)
    if len(view) < 4:
        return []
    (count,) = _U32.unpack_from(view, 0)
    offset = 4
    out = []
    for _ in range(count):
        (turn_id, ts, role_id, flags, score, boosted,
         length) = _HIT_OUT.unpack_from(view, offset)
        offset += _HIT_OUT.size
        text = bytes(view[offset:offset + length])
        offset += length
        out.append({
            "turn_id": turn_id,
            "timestamp": ts,
            "role": INT_TO_ROLE[role_id] if role_id < 4 else "other",
            "is_compacted": bool(flags & 1),
            "score": score,
            "boosted_score": boosted,
            "text": _dec(text),
        })
    return out


def _index_native_active() -> bool:
    return _native is not None and use_native("INDEX")


class SqliteHistoryIndex:
    """Durable FTS5 history index (the apsw backend of history_index.py)."""

    __slots__ = ("_i", "_compat", "_path")

    def __init__(self, path):
        self._path = path
        self._i = (
            _native.index.SqliteHistoryIndex(path) if _index_native_active() else None
        )
        self._compat = _compat_SqliteHistoryIndex(path)

    def open(self):
        if self._i is not None:
            return tuple(self._i.open())
        return self._compat.open()

    def close(self) -> None:
        if self._i is not None:
            self._i.close()
        self._compat.close()

    @property
    def is_open(self) -> bool:
        return bool(self._i.is_open) if self._i is not None else self._compat.is_open

    @property
    def fts_stale(self) -> bool:
        return bool(self._i.fts_stale) if self._i is not None else self._compat.fts_stale

    @property
    def next_turn_id(self) -> int:
        return int(self._i.next_turn_id) if self._i is not None else self._compat.next_turn_id

    def reserve_turn_id(self) -> int:
        return (
            int(self._i.reserve_turn_id()) if self._i is not None
            else self._compat.reserve_turn_id()
        )

    def turn_count(self) -> int:
        return (
            int(self._i.turn_count()) if self._i is not None
            else self._compat.turn_count()
        )

    def append_turns(self, turns) -> int:
        blob = pack_turns(turns)
        if self._i is not None:
            return int(self._i.append_turns_packed(blob))
        return self._compat.append_turns_packed(blob)

    def append_turns_packed(self, blob: bytes) -> int:
        if self._i is not None:
            return int(self._i.append_turns_packed(blob))
        return self._compat.append_turns_packed(blob)

    def insert_turn_with_id(self, turn_id: int, timestamp: float, role, text: str) -> None:
        role_id = ROLE_TO_INT.get(role, 3) if isinstance(role, str) else int(role)
        if self._i is not None:
            self._i.insert_turn_with_id(int(turn_id), float(timestamp), role_id,
                                        _enc(text))
        else:
            self._compat.insert_turn_with_id(turn_id, timestamp, role_id, _enc(text))

    def mark_compacted(self) -> None:
        if self._i is not None:
            self._i.mark_compacted()
        else:
            self._compat.mark_compacted()

    def save(self) -> None:
        if self._i is not None:
            self._i.save()
        else:
            self._compat.save()

    def rebuild_fts(self) -> None:
        if self._i is not None:
            self._i.rebuild_fts()
        else:
            self._compat.rebuild_fts()

    def clear(self) -> None:
        if self._i is not None:
            self._i.clear()
        else:
            self._compat.clear()

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        if self._i is not None:
            return unpack_hits(bytes(self._i.search(_enc(query), int(top_k))))
        return self._compat.search(query, top_k)

    def search_with_recency(self, query: str, top_k: int = 10,
                            weight: float = 1.0) -> list[dict]:
        if self._i is not None:
            return unpack_hits(
                bytes(self._i.search_with_recency(_enc(query), int(top_k), float(weight)))
            )
        return self._compat.search_with_recency(query, top_k, weight)

    def get_by_id(self, turn_id: int) -> str | None:
        if self._i is not None:
            text = self._i.get_by_id(int(turn_id))
            return None if text is None else _dec(text)
        return self._compat.get_by_id(turn_id)

    def non_compacted_turn_ids(self) -> list[int]:
        from ._structure import unpack_u32

        if self._i is not None:
            return unpack_u32(bytes(self._i.non_compacted_turn_ids()))
        return self._compat.non_compacted_turn_ids()

    def bytes_owned(self) -> int:
        return int(self._i.bytes_owned()) if self._i is not None else 0


class _compat_SqliteHistoryIndex:
    """Pure-Python reference for the durable backend.

    The real backend is SQLite/FTS5; this mirror keeps the API (ids, compaction
    flags, substring matching) usable when ``KIMIX_NATIVE_INDEX=0``. Scores are
    0.0 - the BM25 ordering is the native/reference implementation's job, and
    the parity tests compare ids, texts and flags, not scores.
    """

    __slots__ = ("_path", "_turns", "_next", "_open")

    def __init__(self, path):
        self._path = path
        self._turns: dict[int, dict] = {}
        self._next = 0
        self._open = False

    def open(self):
        self._open = True
        return True, ""

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def fts_stale(self) -> bool:
        return False

    @property
    def next_turn_id(self) -> int:
        return self._next

    def reserve_turn_id(self) -> int:
        turn_id = self._next
        self._next += 1
        return turn_id

    def turn_count(self) -> int:
        return len(self._turns)

    def append_turns(self, turns) -> int:
        blob = pack_turns(turns)
        return self.append_turns_packed(blob)

    def append_turns_packed(self, blob: bytes) -> int:
        added = 0
        for turn in unpack_turns(blob):
            role = turn["role"]
            if role == "other" or not turn["text"].strip():
                continue
            turn = dict(turn)
            turn["turn_id"] = self._next
            self._next += 1
            self._turns[turn["turn_id"]] = turn
            added += 1
        return added

    def insert_turn_with_id(self, turn_id, timestamp, role_id, text: bytes) -> None:
        self._turns[int(turn_id)] = {
            "turn_id": int(turn_id),
            "timestamp": float(timestamp),
            "role": INT_TO_ROLE[int(role_id)] if int(role_id) < 4 else "other",
            "is_compacted": False,
            "text": _dec(text),
        }

    def mark_compacted(self) -> None:
        for turn in self._turns.values():
            turn["is_compacted"] = True

    def save(self) -> None:
        return None

    def rebuild_fts(self) -> None:
        return None

    def clear(self) -> None:
        self._turns.clear()
        self._next = 0
        self._open = False

    def _matches(self, query: str, top_k: int) -> list[dict]:
        needle = query.lower()
        hits = []
        for turn in self._turns.values():
            if needle and needle in turn["text"].lower():
                hit = dict(turn)
                hit["score"] = 0.0
                hit["boosted_score"] = 0.0
                hits.append(hit)
        hits.sort(key=lambda h: h["turn_id"])
        return hits[:top_k]

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        return self._matches(query, top_k)

    def search_with_recency(self, query: str, top_k: int = 10,
                            weight: float = 1.0) -> list[dict]:
        del weight
        return self._matches(query, top_k)

    def get_by_id(self, turn_id: int) -> str | None:
        turn = self._turns.get(int(turn_id))
        return None if turn is None else turn["text"]

    def non_compacted_turn_ids(self) -> list[int]:
        return sorted(
            turn_id for turn_id, turn in self._turns.items()
            if not turn["is_compacted"]
        )


# ---------------------------------------------------------------------------
# Message rows (report B2, serialisation only)
# ---------------------------------------------------------------------------
_ROWS_ENTRY = _struct.Struct("<II")


def _row_json(message: dict) -> bytes:
    """``Message.model_dump_json(exclude_none=True)`` for one message."""
    import orjson as _orjson

    from ._structure import normalize_message, part_kind as _kind

    message = normalize_message(message)
    content = message["content"]
    if len(content) == 1 and _kind(content[0]) == 0:
        payload = content[0].get("text") or ""
    else:
        payload = []
        for part in content:
            if _kind(part) == 1:
                payload.append({"type": "think", "think": part.get("think") or "",
                                "encrypted": None})
            else:
                payload.append(part)
    row = {"role": message["role"], "content": payload}
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            function = {"name": call["function"]["name"]}
            if call["function"].get("arguments") is not None:
                function["arguments"] = call["function"]["arguments"]
            calls.append({"type": "function", "id": call["id"], "function": function})
        row["tool_calls"] = calls
    if message.get("tool_call_id") is not None:
        row["tool_call_id"] = message["tool_call_id"]
    return _orjson.dumps(row)


def _row_fts(message: dict) -> bytes:
    from ._structure import part_kind as _kind

    return _enc(
        "\n".join(
            part.get("text") or ""
            for part in message.get("content") or []
            if _kind(part) == 0
        )
    )


def _compat_context_rows(history) -> bytes:
    entries = []
    for message in history:
        entries.append((_row_json(message), _row_fts(message)))
    out = bytearray(_U32.pack(len(entries)))
    for row, fts in entries:
        out += _ROWS_ENTRY.pack(len(row), len(fts))
        out += row
        out += fts
    return bytes(out)


def context_rows(history) -> bytes:
    """Packed ``(json row, fts text)`` pairs for a message list."""
    if _index_native_active():
        from ._structure import build as _build

        hist_bytes, structure = _build(list(history))
        return bytes(_native.index.context_rows(structure, hist_bytes))
    return _compat_context_rows(history)


def _compat_parse_rows(blob: bytes) -> list[dict]:
    import orjson as _orjson

    from ._structure import normalize_message

    view = memoryview(blob)
    if len(view) < 4:
        return []
    (count,) = _U32.unpack_from(view, 0)
    offset = 4
    out = []
    for _ in range(count):
        json_len, fts_len = _ROWS_ENTRY.unpack_from(view, offset)
        offset += _ROWS_ENTRY.size
        row = _orjson.loads(bytes(view[offset:offset + json_len]))
        offset += json_len + fts_len
        if isinstance(row.get("content"), str):
            row["content"] = [{"type": "text", "text": row["content"]}]
        else:
            parts = []
            for part in row.get("content") or []:
                if part.get("type") == "think":
                    parts.append({"type": "think", "think": part.get("think") or ""})
                else:
                    parts.append({k: v for k, v in part.items() if v is not None})
            row["content"] = parts
        calls = []
        for call in row.get("tool_calls") or []:
            function = call.get("function") or {}
            entry = {"id": call.get("id", ""), "function": {"name": function.get("name", "")}}
            if function.get("arguments") is not None:
                entry["function"]["arguments"] = function["arguments"]
            calls.append(entry)
        message = {"role": row.get("role", "user"), "content": row["content"]}
        if calls:
            message["tool_calls"] = calls
        if row.get("tool_call_id") is not None:
            message["tool_call_id"] = row["tool_call_id"]
        out.append(normalize_message(message))
    return out


def parse_rows(blob: bytes, history_handle=None) -> int:
    """Decode packed rows straight into a ``soul.History`` arena.

    ``history_handle`` is a ``kimix_native.soul.History`` (its native handle is
    used) or anything else, in which case the rows are decoded in Python and
    returned as a list when no handle is given.
    """
    native = getattr(history_handle, "_h", None)
    if _index_native_active() and native is not None:
        return int(_native.index.parse_rows(blob, native))
    rows = _compat_parse_rows(blob)
    if history_handle is None:
        return rows  # type: ignore[return-value]
    if hasattr(history_handle, "extend"):
        history_handle.extend(rows)
    return len(rows)
