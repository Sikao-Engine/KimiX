"""SQLite backend: ACID storage for L2/L3 memory tiers."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from kimix.memory.types import MemoryEntry, MemoryType


_COLS_WITH_EMBEDDING = (
    "id, content, memory_type, timestamp, importance, access_count, "
    "last_accessed, embedding, source, metadata, expires_at, agent_id"
)
_COLS_WITHOUT_EMBEDDING = (
    "id, content, memory_type, timestamp, importance, access_count, "
    "last_accessed, source, metadata, expires_at, agent_id"
)


class SQLiteBackend:
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
        source TEXT,
        metadata TEXT,
        expires_at REAL,
        agent_id TEXT NOT NULL DEFAULT 'default'
    );
    CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
    CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp);
    CREATE INDEX IF NOT EXISTS idx_memories_expires ON memories(expires_at);
    CREATE INDEX IF NOT EXISTS idx_memories_agent_type_ts ON memories(agent_id, memory_type, timestamp);
    CREATE INDEX IF NOT EXISTS idx_memories_agent_expires ON memories(agent_id, expires_at);

    CREATE TABLE IF NOT EXISTS memory_tags (
        entry_id TEXT NOT NULL,
        tag TEXT NOT NULL,
        PRIMARY KEY (entry_id, tag),
        FOREIGN KEY (entry_id) REFERENCES memories(id) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_memory_tags_tag_entry ON memory_tags(tag, entry_id);
    """

    def __init__(self, db_path: str | Path = ".kimix_cache/memory.db") -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._write_conn.row_factory = sqlite3.Row
        self._ensure_schema(self._write_conn)
        self._apply_pragmas(self._write_conn)

        self._read_pool: list[sqlite3.Connection] = []
        self._pool_lock = threading.Lock()
        self._max_read_pool = 3

    def _create_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _acquire_read(self) -> sqlite3.Connection:
        with self._pool_lock:
            if self._read_pool:
                return self._read_pool.pop()
        return self._create_conn()

    def _release_read(self, conn: sqlite3.Connection) -> None:
        with self._pool_lock:
            if len(self._read_pool) < self._max_read_pool:
                self._read_pool.append(conn)
                return
        conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(self._SCHEMA)
        conn.commit()

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA page_size=4096")
        conn.execute("PRAGMA wal_autocheckpoint=1000")
        conn.commit()

    @staticmethod
    def _embedding_to_blob(embedding: np.ndarray | list[float] | None) -> bytes | None:
        if embedding is None:
            return None
        if isinstance(embedding, np.ndarray) and embedding.dtype == np.float32:
            return embedding.tobytes()
        return np.asarray(embedding, dtype=np.float32).tobytes()

    @staticmethod
    def _row_to_entry(row: sqlite3.Row, dim: int = 384, include_embedding: bool = True, tags: list[str] | None = None) -> MemoryEntry:
        if include_embedding:
            emb = row[7]
            if emb is not None:
                try:
                    emb = np.frombuffer(emb, dtype=np.float32).reshape(dim)
                except ValueError:
                    emb = np.frombuffer(emb, dtype=np.float32).reshape(-1)
            source_idx = 8
        else:
            emb = None
            source_idx = 7
        meta_raw = row[source_idx + 1]
        metadata = orjson.loads(meta_raw) if meta_raw is not None else {}
        return MemoryEntry(
            content=row[1],
            memory_type=MemoryType(row[2]),
            timestamp=row[3],
            importance=row[4],
            access_count=row[5],
            last_accessed=row[6],
            embedding=emb,
            tags=tags or [],
            source=row[source_idx] or "",
            metadata=metadata,
            expires_at=row[source_idx + 2],
            agent_id=row[source_idx + 3],
        )

    @staticmethod
    def _entry_id_valid(entry_id: str) -> bool:
        if not entry_id or not isinstance(entry_id, str):
            return False
        return True

    @staticmethod
    def _fetch_tags_conn(conn: sqlite3.Connection, entry_ids: list[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if not entry_ids:
            return result
        chunk_size = 900
        for i in range(0, len(entry_ids), chunk_size):
            chunk = entry_ids[i : i + chunk_size]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT entry_id, tag FROM memory_tags WHERE entry_id IN ({placeholders})",
                chunk,
            ).fetchall()
            for eid, tag in rows:
                result.setdefault(eid, []).append(tag)
        return result

    def _insert_tags(self, entry_id: str, tags: list[str]) -> None:
        if not tags:
            return
        self._write_conn.executemany(
            "INSERT OR IGNORE INTO memory_tags (entry_id, tag) VALUES (?, ?)",
            [(entry_id, t) for t in tags],
        )

    def _delete_tags(self, entry_id: str) -> None:
        self._write_conn.execute("DELETE FROM memory_tags WHERE entry_id = ?", (entry_id,))

    def store(self, entry: MemoryEntry, entry_id: str, dim: int = 384) -> None:
        if not self._entry_id_valid(entry_id):
            raise ValueError("entry_id must be a non-empty string")
        meta_blob = orjson.dumps(entry.metadata).decode() if entry.metadata else None
        with self._write_conn:
            self._write_conn.execute(
                """
                INSERT OR REPLACE INTO memories
                (id, content, memory_type, timestamp, importance, access_count,
                 last_accessed, embedding, source, metadata, expires_at, agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    entry.source,
                    meta_blob,
                    entry.expires_at,
                    entry.agent_id,
                ),
            )
            if entry.tags:
                self._delete_tags(entry_id)
                self._insert_tags(entry_id, entry.tags)

    def store_many(self, items: list[tuple[str, MemoryEntry]], dim: int = 384) -> None:
        if not items:
            return
        chunk_size = 500
        for i in range(0, len(items), chunk_size):
            chunk = items[i : i + chunk_size]
            self._store_many_chunk(chunk, dim)

    def _store_many_chunk(self, items: list[tuple[str, MemoryEntry]], dim: int = 384) -> None:
        mem_params = [
            (
                entry_id,
                entry.content,
                entry.memory_type.value,
                entry.timestamp,
                entry.importance,
                entry.access_count,
                entry.last_accessed,
                self._embedding_to_blob(entry.embedding),
                entry.source,
                orjson.dumps(entry.metadata).decode() if entry.metadata else None,
                entry.expires_at,
                entry.agent_id,
            )
            for entry_id, entry in items
        ]
        tag_delete_params = [(entry_id,) for entry_id, entry in items if entry.tags]
        tag_insert_params = [
            (entry_id, tag)
            for entry_id, entry in items
            for tag in entry.tags
        ]

        with self._write_conn:
            self._write_conn.executemany(
                """
                INSERT OR REPLACE INTO memories
                (id, content, memory_type, timestamp, importance, access_count,
                 last_accessed, embedding, source, metadata, expires_at, agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                mem_params,
            )
            if tag_delete_params:
                self._write_conn.executemany("DELETE FROM memory_tags WHERE entry_id = ?", tag_delete_params)
            if tag_insert_params:
                self._write_conn.executemany(
                    "INSERT OR IGNORE INTO memory_tags (entry_id, tag) VALUES (?, ?)",
                    tag_insert_params,
                )

    def get(self, entry_id: str, dim: int = 384, include_embedding: bool = True) -> MemoryEntry | None:
        if not self._entry_id_valid(entry_id):
            return None
        conn = self._acquire_read()
        try:
            cols = _COLS_WITH_EMBEDDING if include_embedding else _COLS_WITHOUT_EMBEDDING
            row = conn.execute(f"SELECT {cols} FROM memories WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                return None
            tags = self._fetch_tags_conn(conn, [entry_id]).get(entry_id, [])
            return self._row_to_entry(row, dim, include_embedding, tags)
        finally:
            self._release_read(conn)

    def delete(self, entry_id: str) -> bool:
        if not self._entry_id_valid(entry_id):
            return False
        with self._write_conn:
            cursor = self._write_conn.execute("DELETE FROM memories WHERE id = ?", (entry_id,))
        return cursor.rowcount > 0

    def _build_list_query(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
        include_embedding: bool = False,
    ) -> tuple[str, list[Any]]:
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
        cols = _COLS_WITH_EMBEDDING if include_embedding else _COLS_WITHOUT_EMBEDDING
        return f"SELECT {cols} FROM memories {where} ORDER BY timestamp DESC", params

    def list_all(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
        dim: int = 384,
        include_embedding: bool = False,
    ) -> list[tuple[str, MemoryEntry]]:
        conn = self._acquire_read()
        try:
            sql, params = self._build_list_query(agent_id, memory_type, exclude_expired, include_embedding)
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            tags_map = self._fetch_tags_conn(conn, ids)
            return [(row["id"], self._row_to_entry(row, dim, include_embedding, tags_map.get(row["id"], []))) for row in rows]
        finally:
            self._release_read(conn)

    def iter_all(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
        dim: int = 384,
        include_embedding: bool = False,
    ):
        conn = self._acquire_read()
        try:
            sql, params = self._build_list_query(agent_id, memory_type, exclude_expired, include_embedding)
            cursor = conn.execute(sql, params)
            rows = list(cursor)
            if rows:
                ids = [row["id"] for row in rows]
                tags_map = self._fetch_tags_conn(conn, ids)
                for row in rows:
                    eid = row["id"]
                    yield eid, self._row_to_entry(row, dim, include_embedding, tags_map.get(eid, []))
        finally:
            self._release_read(conn)

    def iter_rows(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
    ):
        conn = self._acquire_read()
        try:
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
            cursor = conn.execute(f"SELECT id, content, expires_at FROM memories {where}", params)
            for row in cursor:
                yield row["id"], row["content"], row["expires_at"]
        finally:
            self._release_read(conn)

    def update_access(self, entry_id: str, now: float | None = None) -> None:
        if not self._entry_id_valid(entry_id):
            return
        now = now or time.time()
        with self._write_conn:
            self._write_conn.execute(
                "UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                (now, entry_id),
            )

    def update_access_many(self, entry_ids: list[str], now: float | None = None) -> None:
        if not entry_ids:
            return
        now = now or time.time()
        chunk_size = 900
        if len(entry_ids) <= chunk_size:
            with self._write_conn:
                placeholders = ",".join("?" * len(entry_ids))
                self._write_conn.execute(
                    f"UPDATE memories SET access_count = access_count + 1, last_accessed = ? WHERE id IN ({placeholders})",
                    (now, *entry_ids),
                )
            return

        with self._write_conn:
            self._write_conn.execute("CREATE TEMP TABLE IF NOT EXISTS temp_update_ids (id TEXT PRIMARY KEY)")
            self._write_conn.execute("DELETE FROM temp_update_ids")
            self._write_conn.executemany("INSERT OR IGNORE INTO temp_update_ids (id) VALUES (?)", [(eid,) for eid in entry_ids])
            self._write_conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1, last_accessed = ?
                WHERE id IN (SELECT id FROM temp_update_ids)
                """,
                (now,),
            )
            self._write_conn.execute("DROP TABLE temp_update_ids")

    def update_importance(self, entry_id: str, importance: float) -> None:
        if not self._entry_id_valid(entry_id):
            return
        with self._write_conn:
            self._write_conn.execute(
                "UPDATE memories SET importance = ? WHERE id = ?",
                (importance, entry_id),
            )

    def count(
        self,
        agent_id: str | None = None,
        memory_type: MemoryType | None = None,
        exclude_expired: bool = True,
    ) -> int:
        conn = self._acquire_read()
        try:
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
            row = conn.execute(f"SELECT COUNT(*) FROM memories {where}", params).fetchone()
            return row[0] if row else 0
        finally:
            self._release_read(conn)

    def search_by_tag(
        self,
        tags: list[str],
        agent_id: str | None = None,
        exclude_expired: bool = True,
        dim: int = 384,
        include_embedding: bool = False,
    ) -> list[tuple[str, MemoryEntry]]:
        if not tags:
            return self.list_all(agent_id=agent_id, exclude_expired=exclude_expired, dim=dim, include_embedding=include_embedding)

        conn = self._acquire_read()
        try:
            cols = _COLS_WITH_EMBEDDING if include_embedding else _COLS_WITHOUT_EMBEDDING
            cols = ", ".join(f"m.{c}" for c in cols.split(", "))

            params: list[Any] = list(tags)
            if len(tags) == 1:
                sql = f"SELECT {cols} FROM memories m JOIN memory_tags t ON m.id = t.entry_id WHERE t.tag = ?"
            else:
                intersect_parts = " INTERSECT ".join(
                    "SELECT entry_id FROM memory_tags WHERE tag = ?" for _ in tags
                )
                sql = f"SELECT {cols} FROM memories m JOIN ({intersect_parts}) AS t ON m.id = t.entry_id"

            if agent_id is not None:
                sql += " AND m.agent_id = ?"
                params.append(agent_id)
            if exclude_expired:
                sql += " AND (m.expires_at IS NULL OR m.expires_at > ?)"
                params.append(time.time())

            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return []
            ids = [row["id"] for row in rows]
            tags_map = self._fetch_tags_conn(conn, ids)
            return [(row["id"], self._row_to_entry(row, dim, include_embedding, tags_map.get(row["id"], []))) for row in rows]
        finally:
            self._release_read(conn)

    def purge_expired(self, now: float | None = None) -> int:
        now = now or time.time()
        with self._write_conn:
            cursor = self._write_conn.execute("DELETE FROM memories WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
            deleted = cursor.rowcount
        if deleted > 0:
            self._write_conn.execute("ANALYZE memories")
            self._write_conn.execute("ANALYZE memory_tags")
            self._write_conn.commit()
        return deleted

    def optimize(self) -> None:
        self._write_conn.execute("ANALYZE")
        self._write_conn.execute("VACUUM")
        self._write_conn.commit()

    def close(self) -> None:
        self._write_conn.close()
        with self._pool_lock:
            for conn in self._read_pool:
                conn.close()
            self._read_pool.clear()

    def reflect(self) -> str:
        conn = self._acquire_read()
        try:
            now = time.time()
            row = conn.execute(
                """
                SELECT COUNT(*), COUNT(CASE WHEN expires_at IS NOT NULL AND expires_at <= ? THEN 1 END)
                FROM memories
                """,
                (now,),
            ).fetchone()
            total = row[0] if row else 0
            expired = row[1] if row else 0
            return f"SQLite Backend: {total} rows ({expired} expired)"
        finally:
            self._release_read(conn)
