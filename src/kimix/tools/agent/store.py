from __future__ import annotations

import contextlib
from typing import Any, Callable, Literal

from pydantic import BaseModel

from kimi_agent_sdk import Session
from kimix.utils import close_session_async


class ConversationTurn(BaseModel):
    role: Literal["user", "assistant", "system", "tool", "error"]
    content: str | list[Any]
    timestamp: float
    metadata: dict[str, Any] | None = None


class AgentSessionEntry:
    def __init__(
        self,
        session: Session,
        session_id: str,
        created_at: float,
        last_accessed: float,
        conversation_history: list[ConversationTurn],
        total_turns: int,
        is_active: bool = True,
        state: Literal["running", "completed"] = "running",
    ) -> None:
        self.session = session
        self.session_id = session_id
        self.created_at = created_at
        self.last_accessed = last_accessed
        self.conversation_history = conversation_history
        self.total_turns = total_turns
        self.is_active = is_active
        self.state = state


class AgentSessionStore:
    MAX_SESSIONS: int = 10
    # Bounded history of session ids closed through ``close()`` (tombstones).
    # A run that is still finishing when its session is closed externally
    # (``interrupt_agent``) must not resurrect it via a late ``put``; the
    # tombstone lets ``_update_store`` tell "closed while running" apart from
    # "never registered".  The cap is FIFO: the oldest tombstone is dropped
    # once the set is full, so unbounded growth is impossible.
    MAX_TOMBSTONES: int = 64

    def __init__(self, on_close: "Callable[[str], None] | None" = None) -> None:
        """``on_close`` is notified with a session id whenever it leaves the store.

        The store only owns sessions it kept alive (``close_session=False``);
        the module-level registries (live SDK session, parent/child ownership,
        queued messages) are notified so an evicted session does not stay
        referenced — or listed — after it was closed.
        """
        self.entries: dict[str, AgentSessionEntry] = {}
        # Insertion-ordered set of closed session ids (dict-as-ordered-set).
        self._closed_ids: dict[str, None] = {}
        self._on_close = on_close

    def _notify_closed(self, session_id: str) -> None:
        if self._on_close is None:
            return
        with contextlib.suppress(Exception):
            self._on_close(session_id)

    def get(self, session_id: str) -> AgentSessionEntry | None:
        return self.entries.get(session_id)

    def put(self, entry: AgentSessionEntry) -> None:
        self.entries[entry.session_id] = entry
        # A fresh put means the id is alive again: drop any stale tombstone so
        # a later resume of the same id is not mistaken for a dead session.
        self.discard_closed(entry.session_id)

    def close(self, session_id: str) -> bool:
        entry = self.entries.pop(session_id, None)
        self._remember_closed(session_id)
        if entry is not None:
            self._notify_closed(session_id)
        return entry is not None

    def was_closed(self, session_id: str) -> bool:
        """True if *session_id* was closed through ``close()`` and the
        tombstone has not since been discarded by a fresh ``put``."""
        return session_id in self._closed_ids

    def discard_closed(self, session_id: str) -> None:
        """Drop the tombstone for *session_id* (a fresh run reuses the id)."""
        self._closed_ids.pop(session_id, None)

    def _remember_closed(self, session_id: str) -> None:
        self._closed_ids.pop(session_id, None)
        self._closed_ids[session_id] = None
        while len(self._closed_ids) > self.MAX_TOMBSTONES:
            self._closed_ids.pop(next(iter(self._closed_ids)))

    def list_active(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for entry in self.entries.values():
            if entry.is_active:
                result.append({
                    "session_id": entry.session_id,
                    "created_at": entry.created_at,
                    "last_accessed": entry.last_accessed,
                    "total_turns": entry.total_turns,
                    "state": entry.state,
                    "is_active": entry.is_active,
                })
        return result

    async def evict_lru_if_needed(self) -> None:
        while len(self.entries) >= self.MAX_SESSIONS:
            # Prefer evicting finished sessions: a completed background
            # subagent stays listed (list_agents) and resumable
            # (subagent(session_id=...)) until LRU pressure picks it, so a
            # freshly finished child is never dropped just because a newer
            # run started.
            completed = [
                sid for sid, e in self.entries.items() if e.state == "completed"
            ]
            pool = completed if completed else list(self.entries.keys())
            lru_id = min(
                pool,
                key=lambda sid: self.entries[sid].last_accessed,
            )
            entry = self.entries.pop(lru_id)
            entry.is_active = False
            self._notify_closed(lru_id)
            with contextlib.suppress(Exception):
                await close_session_async(entry.session)
