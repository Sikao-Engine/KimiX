"""Tests for the ``_sessions_dir`` override on kimi_cli Session.

The default sessions root is ``<work dir>/.kimix_cache``
(``WorkDirMeta.sessions_dir``); the share dir (``~/.kimi/sessions``) is
never created or used.  The override lets callers (e.g. the SDK, which pins
the location explicitly) redirect the root.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from kaos.path import KaosPath

from kimi_cli.metadata import load_metadata, save_metadata
from kimi_cli.session import KIMIX_CACHE_DIR_NAME, Session
from kimi_cli.wire.file import WireFileMetadata, WireMessageRecord
from kimi_cli.wire.protocol import WIRE_PROTOCOL_VERSION
from kimi_cli.wire.types import TextPart, TurnBegin


@pytest.fixture
def isolated_share_dir(monkeypatch, tmp_path: Path) -> Path:
    """Provide an isolated share directory for metadata operations."""

    share_dir = tmp_path / "share"
    share_dir.mkdir()

    def _get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("kimi_cli.share.get_share_dir", _get_share_dir)
    monkeypatch.setattr("kimi_cli.metadata.get_share_dir", _get_share_dir)
    return share_dir


@pytest.fixture
def work_dir(tmp_path: Path) -> KaosPath:
    path = tmp_path / "work"
    path.mkdir()
    return KaosPath.unsafe_from_local_path(path)


@pytest.fixture
def sessions_dir(tmp_path: Path) -> Path:
    """An override sessions root, mimicking ``<work dir>/.kimix_cache``."""
    return tmp_path / "work" / KIMIX_CACHE_DIR_NAME


async def test_create_uses_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    session = await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)

    assert session.sessions_dir_override == sessions_dir
    assert session.dir == sessions_dir / "sess-1"
    assert (sessions_dir / "sess-1" / "context.jsonl").exists()
    assert session.wire_file.path == sessions_dir / "sess-1" / "wire.jsonl"
    # Nothing should be created in the default share-dir location.
    assert not (isolated_share_dir / "sessions").exists()


async def test_create_default_location_is_work_dir_cache(
    isolated_share_dir: Path, work_dir: KaosPath
):
    session = await Session.create(work_dir, "sess-1")

    assert session.sessions_dir_override is None
    assert session.dir == Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME / "sess-1"
    assert (Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME / "sess-1" / "context.jsonl").exists()
    # The share dir must not be involved at all.
    assert not (isolated_share_dir / "sessions").exists()


async def test_find_with_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)

    found = await Session.find(work_dir, "sess-1", _sessions_dir=sessions_dir)
    assert found is not None
    assert found.sessions_dir_override == sessions_dir
    assert found.dir == sessions_dir / "sess-1"

    # The default root is the same cache dir, so a lookup without the
    # override resolves to the same session (override stays None).
    default_found = await Session.find(work_dir, "sess-1")
    assert default_found is not None
    assert default_found.sessions_dir_override is None
    assert default_found.dir == sessions_dir / "sess-1"


async def test_find_override_missing_session(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    assert await Session.find(work_dir, "nope", _sessions_dir=sessions_dir) is None


def _write_wire_turn(session_dir: Path, text: str) -> None:
    """Make a session non-empty by writing a wire turn record."""
    wire_file = session_dir / "wire.jsonl"
    wire_file.parent.mkdir(parents=True, exist_ok=True)
    metadata = WireFileMetadata(protocol_version=WIRE_PROTOCOL_VERSION)
    record = WireMessageRecord.from_wire_message(
        TurnBegin(user_input=[TextPart(text=text)]),
        timestamp=time.time(),
    )
    with wire_file.open("w", encoding="utf-8") as f:
        f.write(json.dumps(metadata.model_dump(mode="json")) + "\n")
        f.write(json.dumps(record.model_dump(mode="json")) + "\n")


async def test_list_with_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)
    await Session.create(work_dir, "sess-2", _sessions_dir=sessions_dir)
    # Empty sessions are filtered out by list(); give each a wire turn.
    _write_wire_turn(sessions_dir / "sess-1", "hello")
    _write_wire_turn(sessions_dir / "sess-2", "world")

    sessions = await Session.list(work_dir, _sessions_dir=sessions_dir)
    assert {s.id for s in sessions} == {"sess-1", "sess-2"}
    assert all(s.sessions_dir_override == sessions_dir for s in sessions)

    # The default root is the same cache dir, so listing without the
    # override finds the same sessions (override stays None).
    default_sessions = await Session.list(work_dir)
    assert {s.id for s in default_sessions} == {"sess-1", "sess-2"}
    assert all(s.sessions_dir_override is None for s in default_sessions)


async def test_list_override_empty_root(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    assert await Session.list(work_dir, _sessions_dir=sessions_dir) == []


async def test_continue_with_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)

    # Record the last session ID as the CLI post-run logic would.
    metadata = load_metadata()
    work_dir_meta = metadata.get_work_dir_meta(work_dir)
    assert work_dir_meta is not None
    work_dir_meta.last_session_id = "sess-1"
    save_metadata(metadata)

    session = await Session.continue_(work_dir, _sessions_dir=sessions_dir)
    assert session is not None
    assert session.id == "sess-1"
    assert session.dir == sessions_dir / "sess-1"


async def test_rename_with_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)

    renamed = await Session.rename(work_dir, "sess-1", "sess-2", _sessions_dir=sessions_dir)
    assert renamed is not None
    assert renamed.id == "sess-2"
    assert renamed.sessions_dir_override == sessions_dir
    assert renamed.dir == sessions_dir / "sess-2"
    assert (sessions_dir / "sess-2" / "context.jsonl").exists()
    assert not (sessions_dir / "sess-1").exists()

    # The renamed session is also visible without the override (same root).
    default_found = await Session.find(work_dir, "sess-2")
    assert default_found is not None
    assert default_found.dir == sessions_dir / "sess-2"


async def test_copy_with_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)

    copied = await Session.copy(work_dir, "sess-1", "sess-2", _sessions_dir=sessions_dir)
    assert copied.id == "sess-2"
    assert copied.sessions_dir_override == sessions_dir
    assert (sessions_dir / "sess-2" / "context.jsonl").exists()
    assert (sessions_dir / "sess-1" / "context.jsonl").exists()


async def test_delete_with_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    session = await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)
    await session.delete()
    assert not (sessions_dir / "sess-1").exists()


async def test_delete_sync_with_sessions_dir_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    session = await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)
    session.delete_sync()
    assert not (sessions_dir / "sess-1").exists()


async def test_subagents_dir_uses_override(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    session = await Session.create(work_dir, "sess-1", _sessions_dir=sessions_dir)
    assert session.subagents_dir == sessions_dir / "sess-1" / "subagents"


async def test_active_view_reset_makes_session_empty_without_erasing_bytes(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    session = await Session.create(work_dir, "sess-reset", _sessions_dir=sessions_dir)
    session.context_file.write_bytes(
        b'{"role":"user","content":"obsolete"}\n'
        b'{"role":"_reset"}\n'
    )
    assert session.is_empty()
    exported = await session.export()
    assert exported.messages == []
    assert len(session.context_file.read_bytes().splitlines()) == 2


async def test_find_migrates_legacy_db_in_override_and_default_layouts(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    import apsw

    await Session.create(work_dir, "metadata-seed", _sessions_dir=sessions_dir)
    session_dir = sessions_dir / "legacy"
    session_dir.mkdir(parents=True)
    conn = apsw.Connection(str(session_dir / "context.db"))
    try:
        conn.execute(
            "CREATE TABLE messages (rowid INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO messages VALUES(1,'user','{\"role\":\"user\",\"content\":\"legacy\"}',12.5)"
        )
    finally:
        conn.close()

    found = await Session.find(work_dir, "legacy", _sessions_dir=sessions_dir)
    assert found is not None
    assert found.context_file == session_dir / "context.jsonl"
    assert found.context_file.exists()
    assert not (session_dir / "context.db").exists()
    assert (session_dir / "context.db.bak").exists()
    exported = await found.export()
    assert [m.extract_text() for m in exported.messages] == ["legacy"]

    default_found = await Session.find(work_dir, "legacy")
    assert default_found is not None
    assert default_found.context_file == found.context_file


async def test_find_uses_existing_jsonl_without_touching_legacy_db(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    session = await Session.create(work_dir, "both", _sessions_dir=sessions_dir)
    session.context_file.write_bytes(b'{"role":"user","content":"canonical"}\n')
    (session.dir / "context.db").write_bytes(b"do not read")
    found = await Session.find(work_dir, "both", _sessions_dir=sessions_dir)
    assert found is not None
    assert [m.extract_text() for m in (await found.export()).messages] == ["canonical"]
    assert (session.dir / "context.db").read_bytes() == b"do not read"


async def test_custom_db_context_path_is_rejected(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path, tmp_path: Path
):
    with pytest.raises(ValueError, match="must use JSONL"):
        await Session.create(
            work_dir,
            "custom-db",
            _context_file=tmp_path / "custom.db",
            _sessions_dir=sessions_dir,
        )


async def test_copy_finalizes_valid_records_and_ignores_incomplete_tail(
    isolated_share_dir: Path, work_dir: KaosPath, sessions_dir: Path
):
    source = await Session.create(work_dir, "copy-source", _sessions_dir=sessions_dir)
    source.context_file.write_bytes(
        b'{"role":"user","content":"safe","created_at":12.0}\n{"role":"assistant","content":'
    )
    original = source.context_file.read_bytes()
    copied = await Session.copy(
        work_dir, "copy-source", "copy-target", _sessions_dir=sessions_dir
    )
    assert source.context_file.read_bytes() == original
    assert copied.context_file.read_bytes() == b'{"role":"user","content":"safe","created_at":12.0}\n'
    assert [m.extract_text() for m in (await copied.export()).messages] == ["safe"]
