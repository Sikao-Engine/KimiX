"""Tests for anonymous-session cleanup of pre-compaction exports."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kimi_cli.soul.kimisoul import KimiSoul


def test_sync_cleanup_anonymous_removes_all_cache_files(tmp_path: Path) -> None:
    """_sync_cleanup must delete every pre-compact export and the context file."""
    cache_dir = tmp_path / ".kimix_cache" / "session-1"
    cache_dir.mkdir(parents=True)
    files = [cache_dir / f"context_{i}.md" for i in range(3)]
    for f in files:
        f.write_text("export")
    context_file = tmp_path / "context.jsonl"
    context_file.write_text("context")
    history_file = tmp_path / "history.jsonl"
    history_file.write_text("history")

    KimiSoul._sync_cleanup(
        compact_cache_paths=files,
        file_backend=context_file,
        anonymous=True,
    )

    for f in files:
        assert not f.exists()
    assert not context_file.exists()
    assert not history_file.exists()


def test_sync_cleanup_non_anonymous_does_nothing(tmp_path: Path) -> None:
    """_sync_cleanup must leave files alone when the session is not anonymous."""
    cache_dir = tmp_path / ".kimix_cache" / "session-1"
    cache_dir.mkdir(parents=True)
    files = [cache_dir / f"context_{i}.md" for i in range(2)]
    for f in files:
        f.write_text("export")
    context_file = tmp_path / "context.jsonl"
    context_file.write_text("context")

    KimiSoul._sync_cleanup(
        compact_cache_paths=files,
        file_backend=context_file,
        anonymous=False,
    )

    for f in files:
        assert f.exists()
    assert context_file.exists()


@pytest.mark.asyncio
async def test_close_anonymous_removes_session_cache_files(tmp_path: Path) -> None:
    """close() must remove the pre-compact export files and context file for anonymous sessions."""
    soul = object.__new__(KimiSoul)

    cache_dir = tmp_path / ".kimix_cache" / "session-1"
    cache_dir.mkdir(parents=True)
    export_files = [cache_dir / f"context_{i}.md" for i in range(3)]
    for f in export_files:
        f.write_text("export")

    context_file = tmp_path / "context.jsonl"
    context_file.write_text("context")
    history_file = tmp_path / "history.jsonl"
    history_file.write_text("history")

    soul._anonymous = True
    soul._compact_cache_dir = export_files[:]
    soul._context = MagicMock()
    soul._context.file_backend = context_file
    soul._history_index = MagicMock()
    soul._runtime = MagicMock()
    soul._runtime.llm = None
    soul._finalizer = MagicMock()

    await soul.close()

    for f in export_files:
        assert not f.exists()
    assert not context_file.exists()
    assert not history_file.exists()
    soul._finalizer.detach.assert_called_once_with()
