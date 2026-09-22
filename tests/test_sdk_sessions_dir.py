"""Tests for SDK session storage in ``<work dir>/.kimix_cache``.

The SDK creates/resumes its sessions inside the work directory itself —
``<work dir>/.kimix_cache/<session id>`` — instead of the hashed share-dir
location under ``~/.kimi``, and anonymous sessions are still deleted from
there on destroy.
"""

from __future__ import annotations

from pathlib import Path
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

    class _FakeCLI:
        def __init__(self, session: CliSession) -> None:
            self.session = session

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
