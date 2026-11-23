"""Neutral in-memory text extraction, tokenization and BM25-style search."""

from __future__ import annotations

import heapq
import math
import unicodedata
from collections import Counter
from typing import Any

_BM25_K1 = 1.2
_BM25_B = 0.75


def extract_text_from_content(content: Any) -> str:
    """Extract strings and text parts without indexing non-text content."""
    parts: list[str] = []

    def append(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, str):
            parts.append(value)
            return
        text = getattr(value, "text", None)
        if isinstance(text, str):
            parts.append(text)
            return
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                parts.append(text)

    if isinstance(content, (list, tuple)):
        for item in content:
            append(item)
    else:
        append(content)
    return "\n".join(parts)


def message_text(message: Any) -> str:
    return extract_text_from_content(getattr(message, "content", None))


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def is_cjk(char: str) -> bool:
    cp = ord(char)
    return (
        0x4E00 <= cp <= 0x9FFF
        or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF
        or 0x3000 <= cp <= 0x303F
        or 0x3040 <= cp <= 0x309F
        or 0x30A0 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
    )


def contains_cjk(text: str) -> bool:
    return any(is_cjk(char) for char in text)


def _cjk_runs(text: str) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for char in text:
        if is_cjk(char):
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def tokenize(text: str) -> list[str]:
    """Tokenize alphanumeric/code words and contiguous CJK runs into bigrams."""
    normalized = normalize_text(text)
    tokens: list[str] = []
    word: list[str] = []
    cjk: list[str] = []

    def flush_word() -> None:
        if word:
            tokens.append("".join(word))
            word.clear()

    def flush_cjk() -> None:
        if len(cjk) >= 2:
            tokens.extend("".join(cjk[index : index + 2]) for index in range(len(cjk) - 1))
        cjk.clear()

    for char in normalized:
        if is_cjk(char):
            flush_word()
            cjk.append(char)
        elif char.isalnum() or char == "_":
            flush_cjk()
            word.append(char)
        else:
            flush_word()
            flush_cjk()
    flush_word()
    flush_cjk()
    return tokens


def _needs_literal_fallback(normalized_query: str, tokens: list[str]) -> bool:
    if not tokens:
        return True
    if any(len(run) == 1 for run in _cjk_runs(normalized_query)):
        return True
    # Punctuation is literal input, never query grammar.  This also covers %,
    # _, quotes and boolean-looking text without assigning special meaning.
    return any(
        not char.isspace() and not char.isalnum() and not is_cjk(char) and char != "_"
        for char in normalized_query
    )


def _heap_key(score: float, turn_id: int) -> tuple[float, int, int]:
    # The worst retained item has the lowest score; for ties the larger ID is
    # evicted first, giving deterministic ascending-ID tie-breaking.
    return score, -turn_id, turn_id


class InvertedSearchIndex:
    """Incrementally maintained postings and BM25 ranking over cached turns."""

    def __init__(self) -> None:
        self._postings: dict[str, dict[int, int]] = {}
        self._doc_lengths: dict[int, int] = {}
        self._total_length = 0
        self._turns_by_id: dict[int, dict[str, Any]] = {}
        self.add_turn_calls = 0
        self.search_calls = 0
        self.fallback_search_calls = 0

    @property
    def turns(self) -> list[dict[str, Any]]:
        return list(self._turns_by_id.values())

    @property
    def turn_count(self) -> int:
        return len(self._turns_by_id)

    def add_turn(self, turn: dict[str, Any]) -> None:
        turn_id = int(turn["turn_id"])
        if turn_id in self._turns_by_id:
            raise ValueError(f"Duplicate active turn ID: {turn_id}")
        text = turn["text"]
        if not isinstance(text, str):
            raise TypeError("turn text must be a string")
        counts = Counter(tokenize(text))
        stored = dict(turn)
        self._turns_by_id[turn_id] = stored
        self._doc_lengths[turn_id] = sum(counts.values())
        self._total_length += self._doc_lengths[turn_id]
        for token, frequency in counts.items():
            self._postings.setdefault(token, {})[turn_id] = frequency
        self.add_turn_calls += 1

    def mark_compacted(self, through_turn_id: int) -> None:
        for turn_id, turn in self._turns_by_id.items():
            if turn_id <= through_turn_id:
                turn["is_compacted"] = True

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        self.search_calls += 1
        if top_k <= 0 or not isinstance(query, str) or not query.strip():
            return []
        normalized = normalize_text(query.strip())
        tokens = tokenize(normalized)
        if _needs_literal_fallback(normalized, tokens):
            return self._substring_search(normalized, top_k)

        unique_tokens = list(dict.fromkeys(tokens))
        candidates: set[int] | None = None
        for token in unique_tokens:
            posting = self._postings.get(token)
            if not posting:
                return []
            ids = set(posting)
            candidates = ids if candidates is None else candidates & ids
            if not candidates:
                return []
        if not candidates:
            return []

        document_count = len(self._turns_by_id)
        average_length = self._total_length / document_count if document_count else 0.0
        heap: list[tuple[tuple[float, int, int], int, float]] = []
        for turn_id in candidates:
            score = 0.0
            for token in unique_tokens:
                posting = self._postings[token]
                frequency = posting.get(turn_id, 0)
                if frequency == 0:
                    continue
                document_frequency = len(posting)
                inverse_document_frequency = math.log(
                    1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                length = self._doc_lengths[turn_id]
                length_ratio = length / average_length if average_length else 0.0
                denominator = frequency + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * length_ratio)
                if denominator:
                    score += inverse_document_frequency * frequency * (_BM25_K1 + 1.0) / denominator
            item = (_heap_key(score, turn_id), turn_id, score)
            if len(heap) < top_k:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
        ranked = sorted(heap, key=lambda item: (-item[2], item[1]))
        return [{**self._turns_by_id[turn_id], "score": score} for _, turn_id, score in ranked]

    def _substring_search(self, normalized_query: str, top_k: int) -> list[dict[str, Any]]:
        self.fallback_search_calls += 1
        terms = normalized_query.split() or [normalized_query]
        normalized_turns = {
            turn_id: normalize_text(turn["text"])
            for turn_id, turn in self._turns_by_id.items()
        }
        exact_ids = {
            turn_id
            for turn_id, text in normalized_turns.items()
            if normalized_query in text
        }
        heap: list[tuple[tuple[float, int, int], int, float]] = []
        for turn_id, turn in self._turns_by_id.items():
            normalized_text = normalized_turns[turn_id]
            if exact_ids:
                if turn_id not in exact_ids:
                    continue
                score = 1.0
            else:
                matched = sum(1 for term in terms if term in normalized_text)
                if not matched:
                    continue
                score = matched / len(terms)
            item = (_heap_key(score, turn_id), turn_id, score)
            if len(heap) < top_k:
                heapq.heappush(heap, item)
            elif item[0] > heap[0][0]:
                heapq.heapreplace(heap, item)
        ranked = sorted(heap, key=lambda item: (-item[2], item[1]))
        return [{**self._turns_by_id[turn_id], "score": score} for _, turn_id, score in ranked]


def make_snippet(text: str, query: str, *, max_chars: int = 120) -> str:
    """Return a compact snippet with the first literal match marked."""
    if not text:
        return ""
    normalized_text = normalize_text(text)
    normalized_query = normalize_text(query.strip())
    terms = normalized_query.split() or [normalized_query]
    positions = [normalized_text.find(term) for term in terms if term]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return text[:max_chars] + ("..." if len(text) > max_chars else "")
    # NFKC can change character offsets.  Use the normalized text for the
    # marked display snippet; the canonical result content remains verbatim.
    position = min(positions)
    term = min(terms, key=lambda value: normalized_text.find(value) if value in normalized_text else len(normalized_text))
    start = max(0, position - 40)
    end = min(len(normalized_text), position + len(term) + 80)
    prefix = "..." if start else ""
    suffix = "..." if end < len(normalized_text) else ""
    body = normalized_text[start:end]
    relative = position - start
    body = body[:relative] + ">>>" + body[relative : relative + len(term)] + "<<<" + body[relative + len(term) :]
    return prefix + body + suffix
