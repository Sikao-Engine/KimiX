"""Storage-layer tests for the cross-process broadcast bus.

The ``broadcast`` tool writes into ``<work_dir>/.kimix_cache/_broadcast.db``,
a SQLite file shared by every agent process running in that work directory.
These probes pin the properties the soul-side poller relies on:

* keys are strictly increasing millisecond stamps (so ``time >= cursor`` is
  loss-free even when several processes broadcast inside one millisecond);
* the range filter is served by the primary-key btree (no table scan);
* WAL mode is on (readers never block the writer);
* every failure mode -- deleted file, corrupt file, locked file, unusable
  directory -- surfaces as ``BroadcastError`` instead of leaking ``apsw`` /
  ``OSError`` or hanging;
* old messages are pruned at session start.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import apsw
import pytest
from kimi_cli.tools.broadcast.bus import (
    CACHE_DIR_NAME,
    DB_FILE_NAME,
    MAX_CONTENT_CHARS,
    READ_LIMIT,
    BroadcastBus,
    BroadcastError,
    _forget_local_publishes,
    broadcast_db_path,
    is_local_publish,
    now_ms,
    remember_local_publish,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def bus(tmp_path: Path):
    """A bus pointed at a throw-away work directory."""
    instance = BroadcastBus(work_dir=tmp_path / "work")
    try:
        yield instance
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# Location / schema
# ---------------------------------------------------------------------------


def test_database_lives_in_the_work_dir_cache(tmp_path: Path) -> None:
    expected = tmp_path / CACHE_DIR_NAME / DB_FILE_NAME
    assert broadcast_db_path(tmp_path) == expected
    assert BroadcastBus(work_dir=tmp_path).path == expected


def test_append_creates_cache_dir_and_database(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    assert not (work_dir / CACHE_DIR_NAME).exists()
    bus = BroadcastBus(work_dir=work_dir)
    try:
        bus.append("hello")
        assert bus.path.is_file()
        assert bus.path.parent.name == CACHE_DIR_NAME
    finally:
        bus.close()


def test_wal_journal_mode_is_enabled(bus: BroadcastBus) -> None:
    bus.append("wal probe")
    conn = apsw.Connection(str(bus.path))
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    finally:
        conn.close()


def test_time_range_filter_uses_the_primary_key_index(bus: BroadcastBus) -> None:
    bus.append("plan probe")
    conn = apsw.Connection(str(bus.path))
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT time, content FROM broadcast WHERE time >= ? ORDER BY time ASC LIMIT ?",
            (0, 10),
        ).fetchall()
    finally:
        conn.close()
    detail = " ".join(str(row[-1]) for row in plan)
    assert "broadcast" in detail
    # A rowid/PK range search, not a full scan and not a temp b-tree sort.
    assert "SEARCH" in detail.upper() or "USING INTEGER PRIMARY KEY" in detail.upper()
    assert "SCAN" not in detail.upper()
    assert "TEMP B-TREE" not in detail.upper()


# ---------------------------------------------------------------------------
# Append / read semantics
# ---------------------------------------------------------------------------


def test_append_then_read_since_roundtrip(bus: BroadcastBus) -> None:
    first = bus.append("first message")
    second = bus.append("second message")

    assert second.time > first.time
    assert bus.read_since(0) == [first, second]
    assert bus.read_since(first.time) == [first, second]  # `>=`, not `>`
    assert bus.read_since(first.time + 1) == [second]
    assert bus.read_since(second.time + 1) == []
    assert bus.latest_time() == second.time
    assert bus.count() == 2


def test_content_is_stripped_and_returned_verbatim(bus: BroadcastBus) -> None:
    stored = bus.append("  spaced  ")
    assert stored.content == "spaced"
    assert bus.read_since(0)[0].content == "spaced"


def test_keys_stay_strictly_increasing_within_one_millisecond(bus: BroadcastBus) -> None:
    """Two agents broadcasting in the same millisecond must not collide."""
    frozen = 1_700_000_000_000
    keys = [bus.append(f"m{index}", timestamp=frozen).time for index in range(5)]
    assert keys == sorted(set(keys))
    assert keys[0] == frozen
    assert keys[-1] >= frozen + 4


def test_read_since_respects_the_row_limit(bus: BroadcastBus) -> None:
    for index in range(5):
        bus.append(f"m{index}")
    rows = bus.read_since(0, limit=2)
    assert [row.content for row in rows] == ["m0", "m1"]
    # The poller advances its cursor to the last delivered key, so the rest is
    # picked up on the next tick.
    assert [row.content for row in bus.read_since(rows[-1].time + 1)] == ["m2", "m3", "m4"]


def test_read_limit_default_is_bounded() -> None:
    assert 1 <= READ_LIMIT <= 4096


def test_read_since_on_missing_database_returns_empty_and_creates_nothing(
    tmp_path: Path,
) -> None:
    bus = BroadcastBus(work_dir=tmp_path / "empty-work")
    try:
        assert bus.read_since(0) == []
        assert bus.latest_time() is None
        assert bus.count() == 0
        assert bus.prune() == 0
        assert not bus.path.exists()
        assert not (tmp_path / "empty-work" / CACHE_DIR_NAME).exists()
    finally:
        bus.close()


def test_empty_and_oversized_content_are_rejected(bus: BroadcastBus) -> None:
    with pytest.raises(BroadcastError):
        bus.append("   ")
    with pytest.raises(BroadcastError):
        bus.append("x" * (MAX_CONTENT_CHARS + 1))
    assert bus.count() == 0


def test_constructor_rejects_ambiguous_or_positional_paths(tmp_path: Path) -> None:
    """``work_dir`` and ``db_path`` must not be confused with each other."""
    with pytest.raises(ValueError):
        BroadcastBus(work_dir=tmp_path, db_path=tmp_path / "x.db")
    with pytest.raises(ValueError):
        BroadcastBus()
    with pytest.raises(TypeError):
        # Positional use would silently nest a database path under a work dir.
        BroadcastBus(tmp_path)  # type: ignore[misc]


def test_close_is_idempotent_and_reusable(bus: BroadcastBus) -> None:
    bus.append("before close")
    bus.close()
    bus.close()
    assert bus.append("after close").content == "after close"
    assert bus.count() == 2


# ---------------------------------------------------------------------------
# Retention (7-day prune at session start)
# ---------------------------------------------------------------------------


def test_prune_drops_only_messages_older_than_the_window(bus: BroadcastBus) -> None:
    reference = 1_700_000_000_000
    day_ms = 86_400_000
    ancient = bus.append("8 days old", timestamp=reference - 8 * day_ms)
    recent = bus.append("2 days old", timestamp=reference - 2 * day_ms)

    removed = bus.prune(timestamp=reference)
    assert removed == 1
    assert [row.content for row in bus.read_since(0)] == [recent.content]
    assert ancient.time < recent.time


def test_default_retention_is_seven_days(bus: BroadcastBus) -> None:
    assert bus.retention_ms == 7 * 86_400_000


def test_prune_keeps_everything_when_nothing_is_stale(bus: BroadcastBus) -> None:
    bus.append("fresh")
    assert bus.prune() == 0
    assert bus.count() == 1


# ---------------------------------------------------------------------------
# Corner case: database deleted / corrupt / unusable / locked
# ---------------------------------------------------------------------------


def test_database_removed_between_sessions_is_recreated(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    first = BroadcastBus(work_dir=work_dir)
    try:
        first.append("one")
    finally:
        first.close()
    # Another process (or a ``.kimix_cache`` clean) wipes the whole directory.
    shutil.rmtree(work_dir / CACHE_DIR_NAME)

    second = BroadcastBus(work_dir=work_dir)
    try:
        assert second.read_since(0) == []  # no file yet: nothing to read
        assert not second.path.exists()
        stored = second.append("two")
        assert [row.content for row in second.read_since(0)] == ["two"]
        assert stored.time > 0
    finally:
        second.close()


def test_database_deleted_while_the_connection_is_cached(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    bus = BroadcastBus(work_dir=work_dir)
    try:
        bus.append("one")
        bus.close()  # release the file handle so the delete can succeed
        bus.path.unlink()
        assert bus.append("two").content == "two"
        assert [row.content for row in bus.read_since(0)] == ["two"]
    finally:
        bus.close()


def test_corrupt_database_is_quarantined_and_recreated(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    db_path = broadcast_db_path(work_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"this is definitely not a sqlite database" * 32)

    bus = BroadcastBus(work_dir=work_dir)
    try:
        stored = bus.append("after corruption")
        assert stored.content == "after corruption"
        assert [row.content for row in bus.read_since(0)] == ["after corruption"]
    finally:
        bus.close()
    quarantined = list(db_path.parent.glob(f"{DB_FILE_NAME}.corrupt-*"))
    assert quarantined, "the corrupt file should be moved aside, not silently overwritten"


def test_corrupt_database_read_does_not_raise(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    db_path = broadcast_db_path(work_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"\x00garbage" * 64)

    bus = BroadcastBus(work_dir=work_dir)
    try:
        assert bus.read_since(0) == []
    finally:
        bus.close()


def test_corrupt_database_removed_by_a_sibling_process_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Corner case #2: two processes see the corruption, one quarantines first."""
    import kimi_cli.tools.broadcast.bus as bus_module

    work_dir = tmp_path / "work"
    db_path = broadcast_db_path(work_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"definitely not sqlite" * 64)

    real_replace = bus_module.os.replace

    def _lost_the_race(source: str, target: str) -> None:
        if str(source) == str(db_path):
            bus_module.os.remove(source)  # the sibling moved it away already
            raise FileNotFoundError(source)
        real_replace(source, target)

    monkeypatch.setattr(bus_module.os, "replace", _lost_the_race)

    bus = BroadcastBus(work_dir=work_dir)
    try:
        assert bus.append("after the race").content == "after the race"
        assert [row.content for row in bus.read_since(0)] == ["after the race"]
    finally:
        bus.close()
    assert not list(db_path.parent.glob(f"{DB_FILE_NAME}.corrupt-*"))


def test_unrecoverable_corruption_raises_broadcast_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt file that can neither be moved nor removed is reported, not hung."""
    import kimi_cli.tools.broadcast.bus as bus_module

    work_dir = tmp_path / "work"
    db_path = broadcast_db_path(work_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"definitely not sqlite" * 64)

    def _denied(*args: object, **kwargs: object) -> None:
        raise PermissionError("held open by another process")

    monkeypatch.setattr(bus_module.os, "replace", _denied)
    monkeypatch.setattr(bus_module.os, "remove", _denied)

    bus = BroadcastBus(work_dir=work_dir)
    try:
        with pytest.raises(BroadcastError, match="unreadable"):
            bus.append("doomed")
    finally:
        bus.close()


def test_unusable_cache_path_raises_broadcast_error(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # ``.kimix_cache`` exists but is a *file*, so the directory cannot be made.
    (work_dir / CACHE_DIR_NAME).write_text("not a directory")
    bus = BroadcastBus(work_dir=work_dir)
    try:
        with pytest.raises(BroadcastError):
            bus.append("doomed")
        # Reads stay harmless: there is no database file to read.
        assert bus.read_since(0) == []
        assert bus.prune() == 0
    finally:
        bus.close()


def test_locked_database_raises_broadcast_error_then_recovers(tmp_path: Path) -> None:
    """Another process holding an exclusive txn must not hang or crash us."""
    work_dir = tmp_path / "work"
    seed = BroadcastBus(work_dir=work_dir)
    try:
        seed.append("seed")
    finally:
        seed.close()

    holder = apsw.Connection(str(broadcast_db_path(work_dir)))
    holder.execute("BEGIN EXCLUSIVE")
    try:
        busy_bus = BroadcastBus(work_dir=work_dir, busy_timeout_ms=50)
        try:
            started = time.monotonic()
            with pytest.raises(BroadcastError):
                busy_bus.append("blocked")
            # bounded: attempts * busy_timeout + backoff, never an endless wait
            assert time.monotonic() - started < 10
            # Reads keep working in WAL mode while a writer is blocked.
            assert [row.content for row in busy_bus.read_since(0)] == ["seed"]
        finally:
            busy_bus.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()

    recovered = BroadcastBus(work_dir=work_dir, busy_timeout_ms=2000)
    try:
        assert recovered.append("after unlock").content == "after unlock"
        assert [row.content for row in recovered.read_since(0)] == ["seed", "after unlock"]
    finally:
        recovered.close()


_WORKER = textwrap.dedent(
    """
    import sys
    from kimi_cli.tools.broadcast.bus import BroadcastBus

    db_path, worker, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    bus = BroadcastBus(db_path=db_path, busy_timeout_ms=20_000)
    try:
        for index in range(count):
            bus.append(f"{worker}:{index}")
    finally:
        bus.close()
    """
)


def test_concurrent_writers_from_several_processes(tmp_path: Path) -> None:
    """Corner case #1: many agents broadcasting at the same time."""
    work_dir = tmp_path / "work"
    db_path = broadcast_db_path(work_dir)
    # Create the schema up-front so the workers only contend on writes.
    seed = BroadcastBus(work_dir=work_dir)
    seed.close()

    workers, per_worker = 4, 25
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", _WORKER, str(db_path), f"w{index}", str(per_worker)],
            cwd=str(REPO_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(workers)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=120)
        assert process.returncode == 0, stderr.decode("utf-8", "replace") + stdout.decode(
            "utf-8", "replace"
        )

    bus = BroadcastBus(work_dir=work_dir)
    try:
        rows = bus.read_since(0, limit=workers * per_worker + 10)
        assert len(rows) == workers * per_worker
        keys = [row.time for row in rows]
        assert keys == sorted(keys)
        assert len(set(keys)) == len(keys), "keys must be unique across processes"
        seen = {row.content for row in rows}
        assert seen == {f"w{w}:{i}" for w in range(workers) for i in range(per_worker)}
    finally:
        bus.close()


# ---------------------------------------------------------------------------
# Per-process publish registry (the poller skips our own messages)
# ---------------------------------------------------------------------------


def test_local_publish_registry_roundtrip(tmp_path: Path) -> None:
    _forget_local_publishes()
    db_path = tmp_path / "_broadcast.db"
    key = now_ms()
    remember_local_publish(key, db_path)
    assert is_local_publish(key, db_path) is True
    assert is_local_publish(key + 1, db_path) is False
    _forget_local_publishes()
    assert is_local_publish(key, db_path) is False


def test_local_publish_registry_is_scoped_per_database(tmp_path: Path) -> None:
    """The same millisecond key in another work dir is a different message."""
    _forget_local_publishes()
    key = now_ms()
    remember_local_publish(key, tmp_path / "a" / DB_FILE_NAME)
    assert is_local_publish(key, tmp_path / "a" / DB_FILE_NAME) is True
    assert is_local_publish(key, tmp_path / "b" / DB_FILE_NAME) is False
    _forget_local_publishes()


def test_local_publish_registry_stays_bounded(tmp_path: Path) -> None:
    _forget_local_publishes()
    db_path = tmp_path / "_broadcast.db"
    for offset in range(10_000):
        remember_local_publish(offset, db_path)
    # The oldest keys fall out, the newest stay.
    assert is_local_publish(9_999, db_path) is True
    assert is_local_publish(0, db_path) is False
    _forget_local_publishes()
