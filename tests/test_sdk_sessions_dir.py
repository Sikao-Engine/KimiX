"""Tests for SDK session storage in ``<work dir>/.kimix_cache``.

The SDK creates/resumes its sessions inside the work directory itself —
``<work dir>/.kimix_cache/<session id>`` — instead of the hashed share-dir
location under ``~/.kimi``, and anonymous sessions are still deleted from
there on destroy.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kaos.path import KaosPath

from kimi_agent_sdk._session import _sdk_sessions_dir
from kimi_cli.session import KIMIX_CACHE_DIR_NAME, Session as CliSession


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


def _patch_kimi_cli_create(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``KimiCLI.create`` with a stub that keeps the real CliSession.

    Returns a dict that captures the created CLI/session for assertions.
    """
    from kimi_agent_sdk import _session as sdk_session_module

    captured: dict[str, Any] = {}

    class _FakeSoul:
        def __init__(self, toolset: Any = None) -> None:
            self.agent = SimpleNamespace(toolset=toolset)

        async def close(self) -> None:
            return None

    class _FakeCLI:
        def __init__(self, session: CliSession) -> None:
            self.session = session
            self.soul = _FakeSoul()

    async def fake_create(cli_session: CliSession, **kwargs: Any) -> _FakeCLI:
        captured["cli_session"] = cli_session
        captured["kwargs"] = kwargs
        return _FakeCLI(cli_session)

    monkeypatch.setattr(sdk_session_module.KimiCLI, "create", fake_create)
    return captured


def test_sdk_sessions_dir_points_into_work_dir(work_dir: KaosPath) -> None:
    cache_dir = _sdk_sessions_dir(work_dir)
    assert cache_dir == Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME
    assert cache_dir.name == ".kimix_cache"


async def test_session_create_stores_in_kimix_cache(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    work_dir: KaosPath,
) -> None:
    from kimi_agent_sdk._session import Session

    captured = _patch_kimi_cli_create(monkeypatch)

    session = await Session.create(work_dir=work_dir)

    cli_session: CliSession = captured["cli_session"]
    cache_dir = Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME
    assert cli_session.sessions_dir_override == cache_dir
    assert cli_session.dir == cache_dir / cli_session.id
    assert (cache_dir / cli_session.id / "context.db").exists()
    # Anonymous by default (no explicit session ID): must register cleanup.
    assert session._anonymous is True
    # Nothing in the default share-dir location.
    assert not (isolated_share_dir / "sessions").exists()


async def test_session_create_with_explicit_id_not_anonymous(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    work_dir: KaosPath,
) -> None:
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)

    session = await Session.create(work_dir=work_dir, session_id="my-session")
    assert session.id == "my-session"
    assert session._anonymous is False
    cache_dir = Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME
    assert (cache_dir / "my-session" / "context.db").exists()


async def test_session_resume_finds_kimix_cache_session(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    work_dir: KaosPath,
) -> None:
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    created = await Session.create(work_dir=work_dir, session_id="my-session")

    resumed = await Session.resume(work_dir, "my-session")
    assert resumed is not None
    assert resumed.id == "my-session"
    cache_dir = Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME
    assert resumed._cli.session.dir == cache_dir / "my-session"
    # Same on-disk session as the one created above.
    assert resumed._cli.session.id == created.id


async def test_session_resume_missing_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    work_dir: KaosPath,
) -> None:
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)

    assert await Session.resume(work_dir, "does-not-exist") is None


async def test_anonymous_destroy_deletes_kimix_cache_session(
    monkeypatch: pytest.MonkeyPatch,
    isolated_share_dir: Path,
    work_dir: KaosPath,
) -> None:
    """Anonymous cleanup must remove the session dir from .kimix_cache."""
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)

    session = await Session.create(work_dir=work_dir)
    cli_session = session._cli.session
    cache_dir = Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME
    assert (cache_dir / cli_session.id).exists()

    # Simulate __del__ / shutdown-callback best-effort destruction.
    session._delete_sync_best_effort()

    assert not (cache_dir / cli_session.id).exists()


# ---------------------------------------------------------------------------
# Anonymous cleanup must not fail silently
# ---------------------------------------------------------------------------
#
# ``Session.close()`` is the only place that removes an anonymous session's
# directory during a normal run, and it has to survive three failure modes that
# used to leave ``<work dir>/.kimix_cache/<session id>`` behind:
#
# 1. a tool ``cleanup()`` raising — it aborted ``close()`` *before* the deletion
#    while ``_closed`` was already set, which also blocked ``__del__`` and every
#    later ``close()`` (permanent leak),
# 2. ``CliSession.delete()`` raising (e.g. the aiosqlite context connection
#    refusing to close),
# 3. a file inside the directory still being held for a moment (Windows: the
#    aiosqlite worker thread, an anti-virus scan, a file tool): ``delete()``
#    calls ``shutil.rmtree(..., ignore_errors=True)`` exactly once, which
#    silently leaves a half-deleted directory behind.


def _cache_dir(work_dir: KaosPath, session_id: str) -> Path:
    return Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME / session_id


class _RaisingToolset:
    """A toolset whose ``cleanup()`` raises (a tool failing to release a file)."""

    def cleanup(self) -> None:
        raise RuntimeError("tool cleanup exploded")


def _cache_dir(work_dir: KaosPath, session_id: str) -> Path:
    return Path(str(work_dir)) / KIMIX_CACHE_DIR_NAME / session_id


async def test_close_deletes_anonymous_dir_when_tool_cleanup_raises(
    monkeypatch, isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    """A raising tool cleanup must not abort the anonymous dir deletion.

    The teardown error is still reported to the caller (unchanged contract),
    but only *after* the directory has been removed — and it must not leave the
    session permanently un-closable (``_closed`` is already set by then).
    """
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    session = await Session.create(work_dir=work_dir)
    session_id = session._cli.session.id
    session_dir = _cache_dir(work_dir, session_id)
    assert session_dir.exists()

    session._cli.soul.agent.toolset = _RaisingToolset()

    with pytest.raises(RuntimeError, match="tool cleanup exploded"):
        await session.close()

    assert not session_dir.exists(), (
        f"session dir {session_dir} survived close() with a failing tool cleanup"
    )

    # A later close()/__del__ must not be blocked by ``_closed`` either.
    await session.close()
    del session

    assert not session_dir.exists()


async def test_close_deletes_anonymous_dir_when_async_delete_raises(
    monkeypatch, isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    """A failing ``CliSession.delete()`` must not leave the dir behind."""
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    session = await Session.create(work_dir=work_dir)
    session_id = session._cli.session.id
    session_dir = _cache_dir(work_dir, session_id)

    calls: list[int] = []
    real_delete = CliSession.delete

    async def flaky_delete(self: CliSession) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("database is locked")
        await real_delete(self)

    monkeypatch.setattr(CliSession, "delete", flaky_delete)

    await session.close()

    assert calls, "CliSession.delete should be attempted"
    assert not session_dir.exists(), (
        f"session dir {session_dir} survived a failing CliSession.delete()"
    )


async def test_close_retries_deletion_after_transient_lock(
    monkeypatch, isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    """A file released shortly after close() starts must not leak the dir.

    Mirrors the Windows situation described in ``ContextDB.stop_sync``: the
    removal fails while a handle is still open and succeeds once it is released.
    """
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    session = await Session.create(work_dir=work_dir)
    session_id = session._cli.session.id
    session_dir = _cache_dir(work_dir, session_id)
    locked_file = session_dir / "context.db"
    assert locked_file.exists()

    handle = open(locked_file, "rb")  # no FILE_SHARE_DELETE on Windows

    def _release() -> None:
        time.sleep(0.4)
        handle.close()

    releaser = threading.Thread(target=_release)
    releaser.start()
    try:
        await session.close()
    finally:
        releaser.join()

    assert not session_dir.exists(), (
        f"session dir {session_dir} survived close() with a transient lock"
    )


async def test_del_still_cleans_up_after_a_failed_close(
    monkeypatch, isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    """``__del__`` reclaims a directory that survived ``close()``.

    ``_closed`` used to short-circuit ``__del__`` unconditionally, so a close
    that could not remove the directory (locked files, teardown error) disabled
    the last retry for the lifetime of the process.  The destructor, and the
    process-shutdown callback built on the same helper, must still be able to
    reclaim it.
    """
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    session = await Session.create(work_dir=work_dir)
    session_id = session._cli.session.id
    session_dir = _cache_dir(work_dir, session_id)

    await session.close()
    assert not session_dir.exists()

    # Simulate a close that lost the deletion although the session is closed.
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "context.db").write_bytes(b"leftover")
    assert session._closed is True

    session.__del__()  # what CPython runs when the object is reclaimed

    assert not session_dir.exists(), (
        f"session dir {session_dir} survived __del__ of a closed session"
    )
    # Already clean: the retry must stay a harmless no-op.
    session.__del__()
    assert not session_dir.exists()

# ---------------------------------------------------------------------------
# ``close_sync``: the destructor-safe teardown used by cascading parents
# ---------------------------------------------------------------------------


async def test_close_sync_deletes_anonymous_session_dir(
    monkeypatch, isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    """``close_sync`` is the awaiting-free counterpart of ``close``."""
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    session = await Session.create(work_dir=work_dir)
    session_id = session._cli.session.id
    session_dir = _cache_dir(work_dir, session_id)
    assert session_dir.exists()

    session.close_sync()

    assert session._closed is True
    assert not session_dir.exists()


async def test_close_sync_keeps_named_session_dir(
    monkeypatch, isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    """A durable (non-anonymous) session only has its storage released."""
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    session = await Session.create(work_dir=work_dir, session_id="named-session")
    session_dir = _cache_dir(work_dir, "named-session")
    assert session_dir.exists()

    session.close_sync()

    assert session._closed is True
    assert session_dir.exists()


async def test_close_sync_is_idempotent(
    monkeypatch, isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    from kimi_agent_sdk._session import Session

    _patch_kimi_cli_create(monkeypatch)
    session = await Session.create(work_dir=work_dir)
    session_dir = _cache_dir(work_dir, session._cli.session.id)

    session.close_sync()
    session.close_sync()

    assert not session_dir.exists()
