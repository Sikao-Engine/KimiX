from __future__ import annotations

import math
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import orjson

# Use the existing BM25 engine from kimix
from kimix.retrieval import InvertedIndex, NgramTokenizer, Searcher
from kosong.message import Message

from kimi_cli.wire.types import TextPart

_MAX_TURNS: int = 500

class HistoryIndex:
    """In-memory BM25 index over conversation turns.

    Each turn (user or assistant message) is stored as a small document with
    metadata.  The index is persisted to disk on :meth:`save` and reloaded on
    :meth:`load`.
    """

    def __init__(self, persist_path: Path | None = None) -> None:
        self._index = InvertedIndex()
        self._tokenizer = NgramTokenizer(n=2)
        self._searcher: Searcher | None = None
        self._turns: list[dict[str, Any]] = []
        self._persist_path = persist_path
        self._doc_id_counter = 0

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _message_to_text(self, message: Message) -> str:
        parts: list[str] = []
        for part in message.content:
            if isinstance(part, TextPart):
                parts.append(part.text)
        return "\n".join(parts)

    def index_messages(self, messages: Sequence[Message]) -> None:
        """Add *messages* to the index, including tool role messages."""
        # If the index has been finalized (e.g. after a search), rebuild it
        # from the existing turns so new documents can be added.
        if self._index._finalized:
            old_turns = list(self._turns)
            self._index = InvertedIndex()
            for turn in old_turns:
                tokens = self._tokenizer.tokenize(turn["text"])
                self._index.add_document(turn["turn_id"], tokens)

        for msg in messages:
            if msg.role not in {"user", "assistant", "tool"}:
                continue
            text = self._message_to_text(msg)
            if not text.strip():
                continue

            turn = {
                "turn_id": self._doc_id_counter,
                "timestamp": time.time(),
                "role": msg.role,
                "text": text,
                "is_compacted": False,
            }
            self._turns.append(turn)
            tokens = self._tokenizer.tokenize(text)
            self._index.add_document(self._doc_id_counter, tokens)
            self._doc_id_counter += 1

        # Enforce size bound — drop oldest turns
        while len(self._turns) > _MAX_TURNS:
            self._turns.pop(0)
            # We do **not** rebuild the inverted index on every drop;
            # stale doc_ids in the index are harmless for read-only search
            # because the searcher returns doc_ids that we map back to
            # ``self._turns`` (which no longer contains the dropped entry).

        self._searcher = None  # invalidate cached searcher

    def mark_compacted(self) -> None:
        """Mark all currently-indexed turns as compacted/archived."""
        for turn in self._turns:
            turn["is_compacted"] = True

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Return the top-*k* matching turns as dicts."""
        if not self._turns:
            return []
        if not self._index._finalized:
            self._index.finalize()
        if self._searcher is None:
            self._searcher = Searcher(self._index, tokenizer=self._tokenizer)
        results = self._searcher.search(query, top_k=top_k)
        out: list[dict[str, Any]] = []
        for doc_id, score in results:
            # doc_id is the turn_id we assigned at indexing time
            for turn in self._turns:
                if turn["turn_id"] == doc_id:
                    out.append({**turn, "score": score})
                    break
        return out

    def get_by_id(self, ref: str) -> dict[str, Any] | None:
        """Retrieve a turn by its reference ID (turn_id).

        The *ref* may be a plain integer string (``"42"``) or a prefixed
        reference like ``"prune_42"`` — both ``"42"`` and the numeric
        suffix are tried.
        """
        # Parse the numeric part
        ref_str = str(ref)
        if ref_str.startswith("prune_"):
            ref_str = ref_str[len("prune_"):]
        try:
            turn_id = int(ref_str)
        except ValueError:
            return None

        for turn in self._turns:
            if turn["turn_id"] == turn_id:
                return dict(turn)
        return None

    def search_with_recency(
        self,
        query: str,
        *,
        top_k: int = 3,
        recency_weight: float = 1.0,
    ) -> list[dict[str, Any]]:
        """BM25 search with recency boosting.

        boosted_score = bm25_score * (1 + recency_weight * exp(-hours_ago / 24.0))
        """
        if not self._turns:
            return []

        # Fetch a larger candidate pool so recency re-ranking has room to work
        candidates = self.search(query, top_k=top_k * 3)
        if not candidates:
            return []

        now = time.time()
        scored: list[tuple[float, dict[str, Any]]] = []
        for turn in candidates:
            bm25_score = turn.get("score", 0.0)
            hours_ago = (now - turn["timestamp"]) / 3600.0
            boost = 1.0 + recency_weight * math.exp(-hours_ago / 24.0)
            boosted_score = bm25_score * boost
            scored.append((boosted_score, {**turn, "boosted_score": boosted_score}))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [turn for _, turn in scored[:top_k]]

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist turn metadata to disk."""
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "doc_id_counter": self._doc_id_counter,
            "turns": self._turns,
        }
        self._persist_path.write_text(orjson.dumps(data).decode("utf-8"), encoding="utf-8")

    def load(self) -> bool:
        """Load turn metadata from disk.  Returns ``True`` on success."""
        if self._persist_path is None or not self._persist_path.exists():
            return False
        try:
            data = orjson.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception:
            return False

        self._doc_id_counter = data.get("doc_id_counter", 0)
        self._turns = data.get("turns", [])
        # Rebuild the inverted index from loaded turns
        self._index = InvertedIndex()
        for turn in self._turns:
            tokens = self._tokenizer.tokenize(turn["text"])
            self._index.add_document(turn["turn_id"], tokens)
        self._searcher = None
        return True

    def clear(self) -> None:
        """Clear all in-memory data and delete the persisted file."""
        self._index = InvertedIndex()
        self._searcher = None
        self._turns = []
        self._doc_id_counter = 0
        if self._persist_path is not None and self._persist_path.exists():
            self._persist_path.unlink()
