from __future__ import annotations

from pathlib import Path

import apsw
import orjson
import pytest
from kosong.message import Message

from kimi_cli.legacy_sqlite_migration import (
    migrate_legacy_context,
    migrate_session_directory,
    migrate_subagent_directories,
)
from kimi_cli.soul.context import Context, replay_context
from kimi_cli.soul.history_index import HistoryIndex


def _create_context_db(path: Path, *, invalid_message: bool = False) -> None:
    conn = apsw.Connection(str(path))
    try:
        conn.execute(
            "CREATE TABLE messages (rowid INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL, content_text TEXT, created_at REAL NOT NULL)"
        )
        conn.execute("CREATE TABLE system_prompt (id INTEGER PRIMARY KEY, content TEXT NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, message_rowid INTEGER, created_at REAL NOT NULL)")
        conn.execute("CREATE TABLE usage_snapshots (rowid INTEGER PRIMARY KEY AUTOINCREMENT, token_count INTEGER NOT NULL, message_rowid INTEGER, created_at REAL NOT NULL)")
        first = Message(role="user", content="first 中文").model_dump(mode="json", exclude_none=True)
        second = Message(role="assistant", content=[{"type": "text", "text": "second"}], tool_calls=None).model_dump(mode="json", exclude_none=True)
        conn.execute(
            "INSERT INTO messages(rowid,role,content,created_at) VALUES(1,'user',?,101.5)",
            (orjson.dumps(first).decode(),),
        )
        conn.execute(
            "INSERT INTO messages(rowid,role,content,created_at) VALUES(3,'assistant',?,103.5)",
            ("not json" if invalid_message else orjson.dumps(second).decode(),),
        )
        if not invalid_message:
            conn.execute("INSERT INTO system_prompt(id,content,updated_at) VALUES(1,'legacy prompt',100)")
            conn.execute("INSERT INTO checkpoints(id,message_rowid,created_at) VALUES(0,1,102)")
            conn.execute("INSERT INTO checkpoints(id,message_rowid,created_at) VALUES(1,3,105)")
            conn.execute("INSERT INTO usage_snapshots(rowid,token_count,message_rowid,created_at) VALUES(1,100,1,102.5)")
            conn.execute("INSERT INTO usage_snapshots(rowid,token_count,message_rowid,created_at) VALUES(2,200,3,104)")
    finally:
        conn.close()


def _create_history_db(path: Path) -> None:
    conn = apsw.Connection(str(path))
    try:
        conn.execute("CREATE TABLE turns (turn_id INTEGER PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL, timestamp REAL NOT NULL, is_compacted INTEGER NOT NULL DEFAULT 0)")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO turns VALUES(4,'user','old question',100.0,0)")
        conn.execute("INSERT INTO turns VALUES(7,'assistant','old answer',101.0,1)")
    finally:
        conn.close()


def test_context_and_history_migration_success(tmp_path: Path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    _create_context_db(session_dir / "context.db")
    _create_history_db(session_dir / "history.db")

    migrate_session_directory(session_dir)

    context_path = session_dir / "context.jsonl"
    history_path = session_dir / "history.jsonl"
    assert context_path.exists()
    assert history_path.exists()
    assert not (session_dir / "context.db").exists()
    assert not (session_dir / "history.db").exists()
    assert (session_dir / "context.db.bak").exists()
    assert (session_dir / "history.db.bak").exists()
    assert not list(session_dir.glob("*.db-wal"))
    assert not list(session_dir.glob("*.db-shm"))

    state = replay_context(context_path)
    assert state.system_prompt == "legacy prompt"
    assert [entry.message.extract_text() for entry in state.entries] == ["first 中文", "second"]
    assert [entry.created_at for entry in state.entries] == [101.5, 103.5]
    assert [usage.token_count for usage in state.usages] == [100, 200]
    assert [usage.message_count for usage in state.usages] == [1, 2]
    assert list(state.checkpoints) == [0, 1]
    assert [checkpoint.message_count for checkpoint in state.checkpoints.values()] == [1, 2]
    for line in context_path.read_bytes().splitlines():
        assert line == orjson.dumps(orjson.loads(line))

    history = HistoryIndex(jsonl_path=history_path)
    assert history.load() is True
    assert [turn["turn_id"] for turn in history._turns] == [4, 7]
    assert [turn["is_compacted"] for turn in history._turns] == [False, True]
    assert history.search("old question")[0]["text"] == "old question"
    history.close()


@pytest.mark.asyncio
async def test_migrated_checkpoints_revert_with_active_usage(tmp_path: Path) -> None:
    _create_context_db(tmp_path / "context.db")
    assert migrate_legacy_context(tmp_path / "context.db", tmp_path / "context.jsonl")
    ctx = Context.from_file_backend(tmp_path / "context.jsonl")
    await ctx.restore()
    await ctx.revert_to(1)
    assert [m.extract_text() for m in ctx.history] == ["first 中文", "second"]
    assert ctx.token_count == 200
    await ctx.revert_to(0)
    assert [m.extract_text() for m in ctx.history] == ["first 中文"]
    assert ctx.token_count == 0
    assert ctx.token_count_with_pending > 0
    await ctx.close()


def test_both_files_precedence_leaves_db_untouched(tmp_path: Path) -> None:
    _create_context_db(tmp_path / "context.db")
    canonical = tmp_path / "context.jsonl"
    canonical.write_bytes(orjson.dumps({"role": "user", "content": "canonical"}, option=orjson.OPT_APPEND_NEWLINE))
    assert migrate_legacy_context(tmp_path / "context.db", canonical) is False
    assert (tmp_path / "context.db").exists()
    assert not (tmp_path / "context.db.bak").exists()
    assert [m.extract_text() for m in replay_context(canonical).messages] == ["canonical"]


def test_context_migration_failure_preserves_db_and_creates_no_jsonl(tmp_path: Path) -> None:
    db = tmp_path / "context.db"
    _create_context_db(db, invalid_message=True)
    with pytest.raises(orjson.JSONDecodeError):
        migrate_legacy_context(db, tmp_path / "context.jsonl")
    assert db.exists()
    assert not (tmp_path / "context.jsonl").exists()
    assert not list(tmp_path.glob("*.migrating"))


def test_empty_db_migrates_to_empty_jsonl(tmp_path: Path) -> None:
    db = tmp_path / "context.db"
    db.touch()
    assert migrate_legacy_context(db, tmp_path / "context.jsonl")
    assert (tmp_path / "context.jsonl").read_bytes() == b""
    assert (tmp_path / "context.db.bak").read_bytes() == b""


def test_history_failure_is_nonfatal_after_context_migration(tmp_path: Path) -> None:
    _create_context_db(tmp_path / "context.db")
    (tmp_path / "history.db").write_bytes(b"not a database")
    migrate_session_directory(tmp_path)
    assert (tmp_path / "context.jsonl").exists()
    assert not (tmp_path / "history.jsonl").exists()
    assert (tmp_path / "history.db").read_bytes() == b"not a database"


def test_missing_legacy_db_is_never_created(tmp_path: Path) -> None:
    assert migrate_legacy_context(tmp_path / "missing.db", tmp_path / "context.jsonl") is False
    assert not (tmp_path / "missing.db").exists()
    assert not (tmp_path / "context.jsonl").exists()


def test_existing_subagent_instances_are_migrated(tmp_path: Path) -> None:
    root = tmp_path / "session"
    first = root / "subagents" / "a"
    second = root / "subagents" / "b"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _create_context_db(first / "context.db")
    _create_context_db(second / "context.db")
    _create_history_db(first / "history.db")
    migrate_subagent_directories(root)
    assert (first / "context.jsonl").exists()
    assert (first / "history.jsonl").exists()
    assert (second / "context.jsonl").exists()
    assert not (first / "context.db").exists()
    assert not (second / "context.db").exists()


def test_migration_replaces_stale_temporary_file(tmp_path: Path) -> None:
    _create_context_db(tmp_path / "context.db")
    (tmp_path / "context.jsonl.migrating").write_bytes(b"stale partial data")
    assert migrate_legacy_context(tmp_path / "context.db", tmp_path / "context.jsonl")
    assert len(replay_context(tmp_path / "context.jsonl").entries) == 2
    assert not (tmp_path / "context.jsonl.migrating").exists()


def test_concurrent_canonical_file_leaves_legacy_db_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kimi_cli.legacy_sqlite_migration as migration

    _create_context_db(tmp_path / "context.db")
    canonical = tmp_path / "context.jsonl"
    real_replay = migration.replay_context

    def create_canonical_during_verify(temporary: Path):
        state = real_replay(temporary)
        canonical.write_bytes(b'{"role":"user","content":"other writer"}\n')
        return state

    monkeypatch.setattr(migration, "replay_context", create_canonical_during_verify)
    assert migrate_legacy_context(tmp_path / "context.db", canonical) is False
    assert (tmp_path / "context.db").exists()
    assert [m.extract_text() for m in replay_context(canonical).messages] == ["other writer"]
