"""Working memory: current conversation context, limited capacity."""

from __future__ import annotations

from collections import deque
from dataclasses import replace

from kimix.memory.types import MemoryEntry, MemoryType


class WorkingMemory:
    __slots__ = ("max_items", "items", "current_focus")

    def __init__(self, max_items: int = 10) -> None:
        self.max_items = max_items
        self.items: deque[MemoryEntry] = deque(maxlen=max_items)
        self.current_focus: str | None = None

    def add(self, entry: MemoryEntry) -> None:
        self.items.append(replace(entry, memory_type=MemoryType.WORKING))

    def get_context(self, n: int = 5) -> list[MemoryEntry]:
        if n <= 0:
            return []
        return list(self.items)[-n:]

    def clear(self) -> None:
        self.items.clear()
        self.current_focus = None

    def summarize(self) -> str:
        if not self.items:
            return ""
        return " | ".join(item.content for item in list(self.items)[-3:])
