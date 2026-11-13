"""Cross-process ``broadcast`` bus backed by one shared SQLite database.

Several agent processes can run in the same work directory at the same time
(the root session, subagent shells, an IDE-spawned helper, ...).  The
``broadcast`` tool lets one of them publish a short message that every other
process picks up and injects into its own context.

Storage
-------
``<work_dir>/.kimix_cache/_broadcast.db``::

    CREATE TABLE broadcast (time INTEGER PRIMARY KEY NOT NULL, content TEXT NOT NULL)

``time`` is a millisecond epoch stamp and the *primary key* of the table, so
the table btree **is** the index used by the ``WHERE time >= ?`` range scan
(an extra secondary index would only pay for a second btree write per
message).  ``content`` is the message body.

Concurrency
-----------
The database is opened in WAL mode (readers never block the writer and vice
versa), every connection sets a ``busy_timeout`` so a busy/locked file is
waited out instead of failing, and writers use an explicit ``BEGIN IMMEDIATE``
transaction.  Taking the write lock up front matters: in WAL mode a deferred
transaction that reads and then writes can fail with ``SQLITE_BUSY_SNAPSHOT``
when another process committed in between, and that failure is *not*
retryable by the busy handler.  Inside the immediate transaction the writer
reads ``MAX(time)`` and inserts ``max(now_ms, max_time + 1)``, so keys stay
strictly increasing and unique even when several processes broadcast inside
the same millisecond.  A strictly increasing key is what makes
``time >= cursor`` loss-free for readers.

Failure isolation
-----------------
Every storage failure surfaces as :class:`BroadcastError`; callers (the tool
and the soul poller) decide whether to report or swallow it.  A corrupt file
is quarantined (renamed aside, WAL/SHM companions included) and recreated once,
and reads/prunes never create the database file as a side effect.
"""

from __future__ import annotations

import os
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import apsw

from kimi_cli.utils.logging import logger

T = TypeVar("T")

#: Directory under the work dir that holds the shared broadcast database.
CACHE_DIR_NAME = ".kimix_cache"
#: File name of the shared broadcast database.
DB_FILE_NAME = "_broadcast.db"
#: Messages older than this are pruned when a session starts (7 days).
DEFAULT_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
#: How long a blocked connection waits for a lock before reporting ``busy``.
DEFAULT_BUSY_TIMEOUT_MS = 1500
#: Hard cap for a single message body (protects the shared file).
MAX_CONTENT_CHARS = 32_000
#: Upper bound for one poll's result set (the rest is read on the next poll).
READ_LIMIT = 256
#: Total attempts for one operation when the database reports busy/locked.
_BUSY_ATTEMPTS = 3
#: Base backoff between busy retries (doubled per attempt, plus jitter).
_BUSY_BASE_DELAY_S = 0.02

_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS broadcast (
        time INTEGER PRIMARY KEY NOT NULL,
        content TEXT NOT NULL
    )
    """,
)

#: Applied to every fresh connection. ``journal_mode=WAL`` is persistent in the
#: database file, but re-asserting it is a cheap read for an already-WAL file
#: and makes the first writer on a new file switch modes.
_PRAGMA_STATEMENTS: tuple[str, ...] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
)


#: Keys published by *this* process, per database.  The soul poller skips them
#: so an agent never reads its own broadcast back (which would also be a
#: feedback loop: broadcast -> own steer -> reply -> broadcast -> ...).
_LOCAL_KEY_LIMIT = 8192
_local_keys_lock = threading.Lock()
_local_keys: deque[tuple[str, int]] = deque()
_local_key_set: set[tuple[str, int]] = set()
#: ``str(db_path) -> abspath`` memo.  The poller resolves one database path per
#: message otherwise (``os.path.abspath`` costs microseconds), and a poll can
#: carry up to ``READ_LIMIT`` messages.
_LOCAL_PATH_CACHE: dict[str, str] = {}
_LOCAL_PATH_CACHE_LIMIT = 64


def _normalized_path(db_path: str | os.PathLike[str] | Path) -> str:
    """Absolute form of *db_path*, memoized for the hot filter loop."""
    raw = db_path if isinstance(db_path, str) else str(db_path)
    cached = _LOCAL_PATH_CACHE.get(raw)
    if cached is not None:
        return cached
    normalized = os.path.abspath(raw)
    with _local_keys_lock:
        if len(_LOCAL_PATH_CACHE) >= _LOCAL_PATH_CACHE_LIMIT:
            _LOCAL_PATH_CACHE.clear()
        _LOCAL_PATH_CACHE[raw] = normalized
    return normalized


def _local_key(key: int, db_path: str | os.PathLike[str] | Path) -> tuple[str, int]:
    """Registry key: the database file *and* the message key."""
    return (_normalized_path(db_path), int(key))


def remember_local_publish(
    key: int, db_path: str | os.PathLike[str] | Path
) -> None:
    """Remember that *key* in *db_path* was published by this process."""
    entry = _local_key(key, db_path)
    with _local_keys_lock:
        if len(_local_keys) >= _LOCAL_KEY_LIMIT:
            _local_key_set.discard(_local_keys.popleft())
        _local_keys.append(entry)
        _local_key_set.add(entry)


def is_local_publish(key: int, db_path: str | os.PathLike[str] | Path) -> bool:
    """Whether *key* in *db_path* was published by this process (best effort)."""
    return _local_key(key, db_path) in _local_key_set


def _forget_local_publishes() -> None:
    """Drop the local-publish memory (tests)."""
    with _local_keys_lock:
        _local_keys.clear()
        _local_key_set.clear()
        _LOCAL_PATH_CACHE.clear()


class BroadcastError(RuntimeError):
    """Raised when the broadcast database cannot be used.

    Callers treat this as an out-of-band channel failure: the tool reports it,
    the soul poller logs it and retries on the next tick.
    """


@dataclass(frozen=True, slots=True)
class BroadcastMessage:
    """One broadcast message: a millisecond epoch key and its text body."""

    time: int
    content: str


def now_ms() -> int:
    """Return the current wall clock as milliseconds since the epoch."""
    return time.time_ns() // 1_000_000


def broadcast_db_path(work_dir: str | os.PathLike[str] | Path) -> Path:
    """Return the shared broadcast database path for *work_dir*."""
    return Path(str(work_dir)) / CACHE_DIR_NAME / DB_FILE_NAME


def _is_busy_error(exc: BaseException) -> bool:
    """Whether *exc* is a lock/busy condition that is worth retrying."""
    return isinstance(exc, (apsw.BusyError, apsw.LockedError))


def _is_corruption_error(exc: BaseException) -> bool:
    """Whether *exc* means the file itself is not a usable database."""
    return isinstance(exc, (apsw.CorruptError, apsw.NotADBError))


class BroadcastBus:
    """Read/write access to the work directory's shared broadcast database.

    One instance == one cached SQLite connection (plus its WAL file).  The
    instance is safe to share between threads: every operation holds an
    internal lock for its whole transaction, so two threads can never
    interleave statements on the same connection.
    """

    __slots__ = (
        "_db_path",
        "_busy_timeout_ms",
        "_retention_ms",
        "_conn",
        "_lock",
    )

    def __init__(
        self,
        *,
        work_dir: str | os.PathLike[str] | Path | None = None,
        db_path: str | os.PathLike[str] | Path | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
        retention_ms: int = DEFAULT_RETENTION_MS,
    ) -> None:
        """Point the bus at a work directory or at an explicit database file.

        Both arguments are keyword-only on purpose: ``work_dir`` and ``db_path``
        are easy to confuse, and passing a full database path positionally would
        silently nest it (``<db>/.../_broadcast.db``) instead of failing.
        """
        if work_dir is not None and db_path is not None:
            raise ValueError("BroadcastBus takes either work_dir or db_path, not both")
        if db_path is None:
            if work_dir is None:
                raise ValueError("BroadcastBus needs either work_dir or db_path")
            db_path = broadcast_db_path(work_dir)
        self._db_path = Path(db_path)
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._retention_ms = max(0, int(retention_ms))
        self._conn: apsw.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Introspection / lifecycle
    # ------------------------------------------------------------------
    @property
    def path(self) -> Path:
        """Path of the shared database file."""
        return self._db_path

    @property
    def retention_ms(self) -> int:
        """Age after which messages are pruned at session start."""
        return self._retention_ms

    def exists(self) -> bool:
        """Whether the shared database file is present."""
        return self._db_path.exists()

    def close(self) -> None:
        """Close the cached connection. Safe to call more than once."""
        with self._lock:
            self._close_locked()

    def __enter__(self) -> BroadcastBus:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def append(self, content: str, *, timestamp: int | None = None) -> BroadcastMessage:
        """Append *content* and return the stored message.

        Raises:
            BroadcastError: on validation failure or when the database cannot
                be written (busy, corrupt, no permission, ...).
        """
        body = content.strip() if isinstance(content, str) else ""
        if not body:
            raise BroadcastError("broadcast content must be a non-empty string")
        if len(body) > MAX_CONTENT_CHARS:
            raise BroadcastError(
                f"broadcast content is too long ({len(body)} chars > "
                f"{MAX_CONTENT_CHARS} char limit)"
            )
        stamp = now_ms() if timestamp is None else int(timestamp)

        def _operation() -> BroadcastMessage:
            with self._lock:
                conn = self._connect_locked()
                self._exec_locked(conn, "BEGIN IMMEDIATE")
                try:
                    previous = self._exec_locked(
                        conn, "SELECT COALESCE(MAX(time), 0) FROM broadcast"
                    )
                    key = max(stamp, int(previous[0][0]) + 1)
                    self._exec_locked(
                        conn,
                        "INSERT INTO broadcast(time, content) VALUES (?, ?)",
                        (key, body),
                    )
                    self._exec_locked(conn, "COMMIT")
                except BaseException:
                    with suppress(Exception):
                        self._exec_locked(conn, "ROLLBACK")
                    raise
                return BroadcastMessage(time=key, content=body)

        return self._call("append", _operation)

    def read_since(self, since_ms: int, *, limit: int = READ_LIMIT) -> list[BroadcastMessage]:
        """Return messages with ``time >= since_ms``, oldest first.

        The database file is *not* created when it does not exist yet: a fresh
        work directory simply has no messages.
        """
        cutoff = int(since_ms)
        max_rows = max(1, int(limit))
        if not self.exists():
            return []

        def _operation() -> list[BroadcastMessage]:
            with self._lock:
                conn = self._connect_locked()
                rows = self._exec_locked(
                    conn,
                    "SELECT time, content FROM broadcast "
                    "WHERE time >= ? ORDER BY time ASC LIMIT ?",
                    (cutoff, max_rows),
                )
                return [BroadcastMessage(time=int(key), content=text) for key, text in rows]

        return self._call("read", _operation)

    def latest_time(self) -> int | None:
        """Return the newest message key, or ``None`` when the bus is empty."""
        if not self.exists():
            return None

        def _operation() -> int | None:
            with self._lock:
                conn = self._connect_locked()
                rows = self._exec_locked(conn, "SELECT MAX(time) FROM broadcast")
            value = rows[0][0] if rows else None
            return None if value is None else int(value)

        return self._call("latest", _operation)

    def count(self) -> int:
        """Return the number of stored messages (used by tests/benchmarks)."""
        if not self.exists():
            return 0

        def _operation() -> int:
            with self._lock:
                conn = self._connect_locked()
                rows = self._exec_locked(conn, "SELECT COUNT(*) FROM broadcast")
            return int(rows[0][0]) if rows else 0

        return self._call("count", _operation)

    def prune(
        self,
        *,
        timestamp: int | None = None,
        retention_ms: int | None = None,
    ) -> int:
        """Delete messages older than the retention window; return how many.

        Called when a session starts so the shared file cannot grow forever.
        A missing database file is not an error (and is not created here).
        """
        keep_ms = self._retention_ms if retention_ms is None else max(0, int(retention_ms))
        cutoff = (now_ms() if timestamp is None else int(timestamp)) - keep_ms
        if not self.exists():
            return 0

        def _operation() -> int:
            with self._lock:
                conn = self._connect_locked()
                rows = self._exec_locked(
                    conn, "SELECT COUNT(*) FROM broadcast WHERE time < ?", (cutoff,)
                )
                stale = int(rows[0][0]) if rows else 0
                if stale <= 0:
                    # Nothing to delete: skip the write transaction entirely so
                    # starting a session on a hot file stays read-only.
                    return 0
                self._exec_locked(conn, "BEGIN IMMEDIATE")
                try:
                    self._exec_locked(
                        conn, "DELETE FROM broadcast WHERE time < ?", (cutoff,)
                    )
                    self._exec_locked(conn, "COMMIT")
                except BaseException:
                    with suppress(Exception):
                        self._exec_locked(conn, "ROLLBACK")
                    raise
                return stale

        return self._call("prune", _operation)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    def _connect_locked(self) -> apsw.Connection:
        """Return the cached connection, opening/preparing it when needed."""
        conn = self._conn
        if conn is not None:
            return conn
        # Re-create the parent directory when another process (or the user)
        # removed ``.kimix_cache`` while we were running.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = apsw.Connection(str(self._db_path))
        try:
            conn.setbusytimeout(self._busy_timeout_ms)
            self._prepare_locked(conn)
        except BaseException:
            with suppress(Exception):
                conn.close()
            self._conn = None
            raise
        self._conn = conn
        return conn

    def _prepare_locked(self, conn: apsw.Connection) -> None:
        for pragma in _PRAGMA_STATEMENTS:
            self._exec_locked(conn, pragma)
        for statement in _SCHEMA_STATEMENTS:
            self._exec_locked(conn, statement)

    def _close_locked(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        with suppress(Exception):
            conn.close()

    @staticmethod
    def _exec_locked(
        conn: apsw.Connection, sql: str, params: tuple[object, ...] = ()
    ) -> list[tuple]:
        """Execute *sql* and fully drain the cursor.

        Draining matters: apsw executes statements lazily and an unfinished
        cursor keeps its read lock, while unfetched ``PRAGMA`` results would
        never apply.
        """
        cursor = conn.execute(sql, params) if params else conn.execute(sql)
        return cursor.fetchall()

    def _call(self, label: str, operation: Callable[[], T]) -> T:
        """Run *operation* with busy retries and corrupt-file recovery."""
        delay = _BUSY_BASE_DELAY_S
        quarantined = False
        for attempt in range(_BUSY_ATTEMPTS):
            try:
                return operation()
            except (apsw.Error, OSError) as exc:
                # OSError covers the filesystem side: a read-only work dir, a
                # ``.kimix_cache`` that is a file, a permission denied on the
                # WAL companion, ...  All of it surfaces as BroadcastError so
                # callers have exactly one exception to handle.
                # Drop the connection: a failed statement may have left a
                # transaction/cursor state behind, and reopening re-runs the
                # WAL/schema setup.
                with self._lock:
                    self._close_locked()
                if not quarantined and _is_corruption_error(exc):
                    quarantined = True
                    recovered, moved = self._quarantine_locked()
                    if not recovered:
                        raise BroadcastError(
                            f"broadcast database {self._db_path} is unreadable and "
                            f"could not be moved aside: {exc}"
                        ) from exc
                    logger.warning(
                        "Broadcast database {path} was unreadable ({error}); "
                        "moved it to {moved} and starting a fresh one",
                        path=self._db_path,
                        error=exc,
                        moved=moved if moved is not None else "<already removed>",
                    )
                    continue
                if _is_busy_error(exc) and attempt + 1 < _BUSY_ATTEMPTS:
                    logger.debug(
                        "Broadcast database busy during {op}; retrying ({attempt}/{attempts})",
                        op=label,
                        attempt=attempt + 1,
                        attempts=_BUSY_ATTEMPTS,
                    )
                    time.sleep(delay + random.random() * delay)
                    delay *= 2
                    continue
                raise BroadcastError(
                    f"broadcast database {self._db_path} unavailable: {exc}"
                ) from exc
        raise BroadcastError(  # pragma: no cover - loop always returns or raises
            f"broadcast database {self._db_path} unavailable after retries"
        )

    def _quarantine_locked(self) -> tuple[bool, Path | None]:
        """Move a corrupt database (and its WAL/SHM companions) out of the way.

        Returns ``(recovered, moved_to)``:

        * ``recovered`` is True when the corrupt file no longer sits at
          ``self._db_path`` — either moved aside here, or already removed by a
          sibling process that hit the same error.  The caller can then retry on
          a fresh database.
        * ``moved_to`` is the quarantine file name, or ``None`` when there was
          nothing left to move.
        """
        with self._lock:
            self._close_locked()
        stamp = now_ms()
        target = self._db_path.with_name(f"{self._db_path.name}.corrupt-{stamp}")
        try:
            os.replace(str(self._db_path), str(target))
        except FileNotFoundError:
            # Another process won the race: the path is already clean.
            return True, None
        except OSError:
            # Locked by another process (Windows keeps open files undeletable):
            # last resort is to unlink so the next connection starts clean.
            with suppress(OSError):
                os.remove(str(self._db_path))
                return True, None
            return False, None
        for suffix in ("-wal", "-shm"):
            companion = self._db_path.with_name(self._db_path.name + suffix)
            with suppress(OSError):
                os.replace(str(companion), str(target.with_name(target.name + suffix)))
        return True, target
