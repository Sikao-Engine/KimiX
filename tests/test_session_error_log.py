"""Tests that the SDK auto-restart path records error-log snapshots.

When ``Session.prompt`` handles a restartable ``SessionRestartRequired`` it
must record the comprehensive session state into
``<work dir>/.kimix_cache/error_log/`` — once per restart attempt
(``session_restart``), once when the budget is exhausted
(``restart_exhausted``), and once when the error is not restartable at all
(``non_restartable``) — and surface the snapshot path on the wire.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from kaos.path import KaosPath
from kimi_cli.soul import SessionRestartRequired
from kimi_cli.soul.error_log import LATEST_POINTER_NAME
from kimi_cli.wire.types import TextPart
from kosong.chat_provider import APIStatusError, APITimeoutError

from kimi_agent_sdk._session import Session


@dataclass
class _FakeAgent:
    toolset: Any = None


@dataclass
class _FakeRuntime:
    llm: Any = None


@dataclass
class _FakeSoul:
    agent: _FakeAgent = field(default_factory=_FakeAgent)
    _runtime: _FakeRuntime = field(default_factory=_FakeRuntime)

    async def close(self) -> None:
        pass


@dataclass
class _FakeWireFile:
    path: Path


@dataclass
class _FakeCLISession:
    work_dir: KaosPath
    session_id: str
    context_file: Path
    dir: Path
    wire_file: _FakeWireFile
    custom_data: dict[str, Any] = field(default_factory=dict)
    custom_config: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.session_id

    async def close_context_db(self) -> None:
        pass


class _FakeCLI:
    """Fake CLI whose ``run`` fails according to a scripted error plan."""

    def __init__(
        self,
        session: _FakeCLISession,
        errors: list[BaseException | None],
    ) -> None:
        self.session = session
        self.soul = _FakeSoul()
        self._errors = list(errors)

    async def run(
        self,
        user_input: Any,
        cancel_event: Any,
        *,
        merge_wire_messages: bool = False,
    ) -> Any:
        error = self._errors.pop(0) if self._errors else None
        if error is not None:
            raise error
        yield TextPart(text="recovered")


def _make_session(tmp_path: Path, errors: list[BaseException | None]) -> Session:
    work_dir = KaosPath.unsafe_from_local_path(tmp_path)
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "wire.jsonl").write_text("", encoding="utf-8")
    cli_session = _FakeCLISession(
        work_dir=work_dir,
        session_id="s1",
        context_file=session_dir / "context.jsonl",
        dir=session_dir,
        wire_file=_FakeWireFile(session_dir / "wire.jsonl"),
    )
    session = Session(_FakeCLI(cli_session, errors))
    session._create_kwargs = {"yolo": True}
    return session

@pytest.mark.asyncio
async def test_prompt_restart_records_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every restart attempt and the exhausted budget must leave a snapshot."""
    import kimi_agent_sdk._session as session_mod

    timeout = APITimeoutError("Request timed out: 600s")

    def _restart_error() -> SessionRestartRequired:
        # Mirrors what the real KimiCLI.run raises when step retries exhaust.
        return SessionRestartRequired(
            "Step 1: APITimeoutError [connection recovery exhausted] "
            "— retries exhausted, restarting session",
            original_error=timeout,
        )

    work_dir = KaosPath.unsafe_from_local_path(tmp_path)
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "wire.jsonl").write_text("", encoding="utf-8")
    cli_session = _FakeCLISession(
        work_dir=work_dir,
        session_id="s1",
        context_file=session_dir / "context.jsonl",
        dir=session_dir,
        wire_file=_FakeWireFile(session_dir / "wire.jsonl"),
    )

    # The CLI fails twice (restart budget 1) and would recover on a third run.
    created: list[_FakeCLI] = []

    def _make_cli(cli_session: _FakeCLISession, **_kwargs: Any) -> _FakeCLI:
        cli = _FakeCLI(cli_session, errors=[_restart_error(), _restart_error(), None])
        created.append(cli)
        return cli

    cli = _make_cli(cli_session)
    session = Session(cli)
    session._create_kwargs = {"yolo": True}

    async def fake_find(
        work_dir: KaosPath, session_id: str, _sessions_dir: Any = None
    ) -> _FakeCLISession:
        return cli_session

    async def fake_create(cli_session: _FakeCLISession, **kwargs: Any) -> _FakeCLI:
        return _make_cli(cli_session, **kwargs)

    monkeypatch.setattr(
        session_mod.CliSession,
        "find",
        classmethod(lambda cls, *a, **kw: fake_find(*a, **kw)),
    )
    monkeypatch.setattr(
        session_mod.CliSession,
        "create",
        classmethod(lambda cls, *a, **kw: fake_create(a[1] if len(a) > 1 else None, **kw)),
    )
    monkeypatch.setattr(
        session_mod.KimiCLI,
        "create",
        classmethod(lambda cls, *a, **kw: fake_create(a[-1] if a else None, **kw)),
    )

    messages: list[Any] = []
    with pytest.raises(APITimeoutError):
        async for msg in session.prompt("hello", max_restarts=1):
            messages.append(msg)

    log_dir = tmp_path / ".kimix_cache" / "error_log"
    files = {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in log_dir.glob("*.json")
        if p.name != LATEST_POINTER_NAME
    }
    phases = sorted(snapshot["phase"] for snapshot in files.values())
    assert phases == ["restart_exhausted", "session_restart"]

    restart_snapshot = next(
        snapshot for snapshot in files.values() if snapshot["phase"] == "session_restart"
    )
    exhausted = next(
        snapshot for snapshot in files.values() if snapshot["phase"] == "restart_exhausted"
    )

    # Restart bookkeeping is captured from the SDK loop.
    assert restart_snapshot["restart"]["attempt"] == 1
    assert restart_snapshot["restart"]["max_restarts"] == 1
    assert restart_snapshot["restart"]["restartable"] is True
    assert exhausted["restart"]["attempt"] == 1
    assert exhausted["restart"]["max_restarts"] == 1

    # The error chain is preserved (restart wrapper + original timeout).
    for snapshot in (restart_snapshot, exhausted):
        assert snapshot["error"]["type"] == "SessionRestartRequired"
        assert snapshot["error"]["original_error"]["type"] == "APITimeoutError"
        assert "Request timed out" in snapshot["error"]["original_error"]["message"]

    # The session state references the failing session.
    for snapshot in files.values():
        assert snapshot["session"]["id"] == "s1"
        assert snapshot["session"]["work_dir"] == str(tmp_path)

    # The wire message names the recorded snapshot.
    texts = [m.text for m in messages if isinstance(m, TextPart)]
    assert any("Connection lost (APITimeoutError)" in t for t in texts)
    assert any("Restarting session (attempt 1/1)" in t for t in texts)
    snapshot_line = next((t for t in texts if "Debug snapshot:" in t), None)
    assert snapshot_line is not None
    named_path = snapshot_line.split("Debug snapshot:", 1)[1].strip()
    assert Path(named_path).exists()


@pytest.mark.asyncio
async def test_prompt_non_restartable_records_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic 400 errors surface immediately and still leave a snapshot."""
    bad_request = SessionRestartRequired(
        "Step 1: APIStatusError (status=400) — retries exhausted, restarting session",
        original_error=APIStatusError(400, "invalid request"),
    )
    session = _make_session(tmp_path, errors=[bad_request])

    messages: list[Any] = []
    with pytest.raises(APIStatusError):
        async for msg in session.prompt("hello", max_restarts=3):
            messages.append(msg)

    log_dir = tmp_path / ".kimix_cache" / "error_log"
    files = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in log_dir.glob("*.json")
        if p.name != LATEST_POINTER_NAME
    ]
    assert [snapshot["phase"] for snapshot in files] == ["non_restartable"]
    snapshot = files[0]
    assert snapshot["restart"]["restartable"] is False
    assert snapshot["error"]["original_error"]["status_code"] == 400

    # No "Connection lost" wire noise for deterministic errors.
    texts = [m.text for m in messages if isinstance(m, TextPart)]
    assert not any("Connection lost" in t for t in texts)
