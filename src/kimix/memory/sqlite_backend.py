"""SQLite backend: ACID storage for L2/L3 memory tiers.

Keeps the ``MemoryEntry`` interface compatible while replacing JSON/dict
storage with SQLite.  Embeddings are stored as BLOBs (float32 arrays).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import numpy as np

from kimix.memory.types import MemoryEntry, MemoryType


class SQLiteBackend:
    """SQLite-backed memory store with agent isolation."""

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS memories (
        id TEXT PRIMARY KEY,
        content TEXT NOT NULL,
        memory_type TEXT NOT NULL,
        timestamp REAL NOT NULL,
        importance REAL NOT NULL DEFAULT 1.0,
        access_count INTEGER NOT NULL DEFAULT 0,
        last_accessed REAL NOT NULL,
        embedding BLOB,
        tags TEXT,               -- JSON list (kept for migration / read-only mirrors)
        source TEXT,
        metadata TEXT,           -- JSON dict
        expires_at REAL,
        agent_id TEXT NOT NULL DEFAULT 'default'
    );
    CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
    CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id);
    CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
    CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories(expires_at);
    CREATE INDEX IF NOT EXISTS idx_memories_agent_type_ts
        ON memories(agent_id, memory_type, timestamp);
    CREATE INDEX IF NOT EXISTS idx_memories_agent_expires
        ON memories(agent_id, expires_at);

    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
        content, content_rowid=rowid
    );

    -- Relational tag storage for fast AND-semantics tag search
    CREATE TABLE IF NOT EXISTS memory_tags (
        entry_id TEXT NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (entry_id, tag),
        FOREIGN KEY (entry_id) REFERENCES memories(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_memory_tags_tag ON memory_tags(tag);
    """

    def __init__(self, db_path: str | Path = ".kimix_cache/memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._apply_pragmas()

    def _ensure_schema(self) -> None:
        self._conn.executescript(self._SCHEMA)
        self._conn.commit()

    def _apply_pragmas(self) -> None:
        # WAL mode allows readers to not block writers and improves concurrency.
        self._conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL is safe with WAL and much faster than FULL.
        self._conn.execute("PRAGMA synchronous=NORMAL")
        # ~64 MB page cache (negative = kiB units).
        self._conn.execute("PRAGMA cache_size=-64000")
        # Keep temp tables in memory.
        self._conn.execute("PRAGMA temp_store=MEMORY")
        # Memory-map up to 256 MB of the DB file (reduces system calls).
        self._conn.execute("PRAGMA mmap_size=268435456")
        # Foreign keys are required for CASCADE to work on memory_tags.
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.commit()

    # --- Serialization helpers ------------------------------------------------

    @staticmethod
    def _embedding_to_blob(embedding: np.ndarray | list[float] | None) -> bytes | None:
        if embedding is None:
            return None
        arr = np.asarray(embedding, dtype=np.float32)
        return arr.tobytes()

    @staticmethod
    def _blob_to_embedding(blob: bytes | None, dim: int = 384) -> np.ndarray | None:
        if blob is None:
            return None
        return np.frombuffer(blob, dtype=np.float32).copy()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row, dim: int = 384) -> MemoryEntry:
        return MemoryEntry(
            content=row["content"],
            memory_type=MemoryType(row["memory_type"]),
            timestamp=row["timestamp"],
            importance=row["importance"],
            access_count=row["access_count"],
            last_accessed=row["last_accessed"],
            embedding=SQLiteBackend._blob_to_embedding(row["embedding"], dim),
            tags=json.loads(row["tags"]) if row["tags"] else [],
            source=row["source"] or "",
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            expires_at=row["expires_at"],
            agent_id=row["agent_id"],
        )

    def _insert_tags(self, entry_id: str, tags: list[str]) -> None:
        if not tags:
            return
        self._conn.executemany(
            "INSERT OR IGNORE INTO memory_tags (entry_id, tag) VALUES (?, ?)",
            [(entry_id, t) for t in tags],
        )

    def _delete_tags(self, entry_id: str) -> None:
        self._conn.execute("DELETE FROM memory_tags WHERE entry_id = ?", (entry_id,))

    # --- CRUD -----------------------------------------------------------------

    def store(self, entry: MemoryEntry, entry_id: str, dim: int = 384) -> None:
        """Insert or replace a memory entry."""
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO memories
                (id, content, memory_type, timestamp, importance, access_count,
                 last_accessed, embedding, tags, source, metadata, expires_at, agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    entry.content,
                    entry.memory_type.value,
                    entry.timestamp,
                    entry.importance,
                    entry.access_count,
                    entry.last_accessed,
                    self._embedding_to_blob(entry.embedding),
                    json.dumps(entry.tags, ensure_ascii=False),
                    entry.source,
                    json.dumps(entry.metadata, ensure_ascii=False),
                    entry.expires_at,
                    entry.agent_id,
                ),
            )
            self._delete_tags(entry_id)
            self._insert_tags(entry_id, entry.tags)

    def store_many(self, items: list[tuple[str, MemoryEntry]], dim: int = 384) -> None:
        """Batch insert/replace entries in a single transaction."""
        with self._conn:
            for entry_id, entry in items:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO memories
                    (id, content, memory_type, timestamp, importance, access_count,
                     last_accessed, embedding, tags, source, metadata, expires_at, agent_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        entry.content,
                        entry.memory_type.value,
                        entry.timestamp,
                        entry.importance,
                        entry.access_count,
                        entry.last_accessed,
                        self._embedding_to_blob(entry.embedding),
                        json.dumps(entry.tags, ensure_ascii=False),
                        entry.source,
                        json.dumps(entry.metadata, ensure_ascii=False),
                        entry.expires_at,
                        entry.agent_id,
                    ),
                )
                self._delete_tags(entry_id)
                self._insert_tags(entry_id, entry.tags)

    def get(self, entry_id: str, dim: int = 384) -> MemoryEntry | None:
        row = self._conn.execute(
            "SELECT * FROM memories WHERE id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row, dim)

    def delete(self, entry_id: str) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM memories WHERE id = ?", (entry_id,)
            )
        return cursor.rowcount > 0

    def list_all(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
        dim: int = 384,
    ) -> list[tuple[str, MemoryEntry]]:
        """Return all entries as (entry_id, MemoryEntry) pairs."""
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id is not None:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if memory_type is not None:
            conditions.append("memory_type = ?")
            params.append(memory_type.value)
        if exclude_expired:
            conditions.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(time.time())
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = self._conn.execute(
            f"SELECT * FROM memories {where} ORDER BY timestamp DESC", params
        ).fetchall()
        return [(row["id"], self._row_to_entry(row, dim)) for row in rows]

    def iter_rows(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
    ):
        """Yield lightweight (entry_id, content, expires_at) tuples.

        Skips embedding deserialization — useful for BM25 index builds.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id is not None:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if memory_type is not None:
            conditions.append("memory_type = ?")
            params.append(memory_type.value)
        if exclude_expired:
            conditions.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(time.time())
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        cursor = self._conn.execute(
            f"SELECT id, content, expires_at FROM memories {where}", params
        )
        for row in cursor:
            yield row["id"], row["content"], row["expires_at"]

    def update_access(self, entry_id: str, now: float | None = None) -> None:
        now = now or time.time()
        with self._conn:
            self._conn.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, entry_id),
            )

    def update_access_many(self, entry_ids: list[str], now: float | None = None) -> None:
        """Batch-bump access counters."""
        now = now or time.time()
        with self._conn:
            self._conn.executemany(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                [(now, eid) for eid in entry_ids],
            )

    def update_importance(self, entry_id: str, importance: float) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE memories SET importance = ? WHERE id = ?",
                (importance, entry_id),
            )

    def count(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
    ) -> int:
        conditions: list[str] = []
        params: list[Any] = []
        if agent_id is not None:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if memory_type is not None:
            conditions.append("memory_type = ?")
            params.append(memory_type.value)
        if exclude_expired:
            conditions.append("(expires_at IS NULL OR expires_at > ?)")
            params.append(time.time())
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        row = self._conn.execute(
            f"SELECT COUNT(*) FROM memories {where}", params
        ).fetchone()
        return row[0] if row else 0

    def search_by_tag(
        self,
        tags: list[str],
        agent_id: str | None = None,
        exclude_expired: bool = True,
        dim: int = 384,
    ) -> list[tuple[str, MemoryEntry]]:
        """Return entries whose tags contain *all* provided tags (AND semantics)."""
        if not tags:
            return self.list_all(agent_id=agent_id, exclude_expired=exclude_expired, dim=dim)

        # Relational tag search via junction table.
        placeholders = ",".join("?" * len(tags))
        sql = f"""
            SELECT m.* FROM memories m
            JOIN memory_tags t ON m.id = t.entry_id
            WHERE t.tag IN ({placeholders})
        """
        params: list[Any] = list(tags)

        if agent_id is not None:
            sql += " AND m.agent_id = ?"
            params.append(agent_id)
        if exclude_expired:
            sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
            params.append(time.time())

        sql += f"""
            GROUP BY m.id
            HAVING COUNT(DISTINCT t.tag) = ?
        """
        params.append(len(tags))

        rows = self._conn.execute(sql, params).fetchall()
        return [(row["id"], self._row_to_entry(row, dim)) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def reflect(self) -> str:
        total = self.count(exclude_expired=False)
        expired = total - self.count(exclude_expired=True)
        return f"SQLite Backend: {total} rows ({expired} expired)"
