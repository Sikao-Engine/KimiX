"""Tests that deterministic API errors (e.g. 400) fail fast instead of
triggering phantom "Connection lost" session restarts.

Regression context: a poisoned tool_call.arguments ('{}{}') made the scnet/Qwen
gateway return HTTP 400 on every request.  The restart loop re-sent the same
history three times, producing three identical 400s and a misleading
"Connection lost ... restart limit reached" before the real error surfaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from kaos.path import KaosPath

import kimi_agent_sdk._session as session_mod
from kimi_cli.soul import SessionRestartRequired
from kimi_cli.wire.types import TextPart
from kosong.chat_provider import APIStatusError

from kimi_agent_sdk._session import Session


class _FakeChatProvider:
    async def aclose(self) -> None:
        pass


@dataclass
class _FakeLLM:
    chat_provider: _FakeChatProvider


@dataclass
class _FakeRuntime:
    llm: _FakeLLM | None


@dataclass
class _FakeAgent:
    toolset: Any = None


@dataclass
class _FakeSoul:
    agent: _FakeAgent
    _runtime: _FakeRuntime

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
    """Fake CLI that always fails with the configured original_error."""

    original_error: BaseException | None = None
    calls: int = 0

    def __init__(self, session: _FakeCLISession) -> None:
        self.session = session
        self.soul = _FakeSoul(
            agent=_FakeAgent(),
            _runtime=_FakeRuntime(llm=_FakeLLM(chat_provider=_FakeChatProvider())),
        )

    async def run(
        self,
        user_input: Any,
        cancel_event: Any,
        *,
        merge_wire_messages: bool = False,
    ) -> Any:
        _FakeCLI.calls += 1
        raise SessionRestartRequired("simulated failure", original_error=_FakeCLI.original_error)
        yield  # pragma: no cover - makes this an async generator


def _patch_restart(monkeypatch: pytest.MonkeyPatch, cli_session: _FakeCLISession) -> None:
    """Make Session._restart() reload the same fake session/CLI."""

    async def fake_find(
        work_dir: KaosPath, session_id: str, _sessions_dir: Any = None
    ) -> _FakeCLISession:
        return cli_session

    async def fake_create(cli_session: _FakeCLISession, **kwargs: Any) -> _FakeCLI:
        return _FakeCLI(cli_session)

    monkeypatch.setattr(
        session_mod.CliSession,
        "find",
        classmethod(lambda cls, *a, **kw: fake_find(*a, **kw)),
    )
    monkeypatch.setattr(
        session_mod.KimiCLI,
        "create",
        classmethod(lambda cls, *a, **kw: fake_create(a[0] if a else None, **kw)),
    )


def _make_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    work_dir = KaosPath.unsafe_from_local_path(tmp_path)
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    context_file = session_dir / "context.jsonl"
    context_file.write_text('{"role": "user", "content": "previous turn"}\n', encoding="utf-8")
    (session_dir / "state.json").write_text("{}", encoding="utf-8")
    cli_session = _FakeCLISession(
        work_dir=work_dir,
        session_id="s1",
        context_file=context_file,
        dir=session_dir,
        wire_file=_FakeWireFile(session_dir / "wire.jsonl"),
    )
    _patch_restart(monkeypatch, cli_session)
    session = Session(_FakeCLI(cli_session))
    session._create_kwargs = {"yolo": True}
    return session


@pytest.mark.asyncio
async def test_non_retryable_status_fails_fast_without_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 400 (deterministic client error) must surface immediately, not burn
    the restart budget on identical retries."""
    _FakeCLI.calls = 0
    _FakeCLI.original_error = APIStatusError(400, "invalid request")
    session = _make_session(tmp_path, monkeypatch)

    texts: list[str] = []
    with pytest.raises(APIStatusError) as exc_info:
        async for msg in session.prompt("hello", max_restarts=3):
            if isinstance(msg, TextPart):
                texts.append(msg.text)

    assert exc_info.value.status_code == 400
    assert _FakeCLI.calls == 1, "must not retry a deterministic 400"
    assert not any("Connection lost" in t for t in texts)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
async def test_retryable_status_still_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Transient statuses must keep the auto-restart behavior until the budget
    is exhausted."""
    _FakeCLI.calls = 0
    _FakeCLI.original_error = APIStatusError(status, "transient")
    session = _make_session(tmp_path, monkeypatch)

    texts: list[str] = []
    with pytest.raises(APIStatusError) as exc_info:
        async for msg in session.prompt("hello", max_restarts=2):
            if isinstance(msg, TextPart):
                texts.append(msg.text)

    assert exc_info.value.status_code == status
    assert _FakeCLI.calls == 3, "transient errors should exhaust the restart budget"
    assert sum("Connection lost" in t for t in texts) == 2


@pytest.mark.asyncio
async def test_restart_without_original_error_keeps_restarting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SessionRestartRequired without an attached API error (e.g. recovery
    exhaustion) keeps the traditional restart behavior."""
    _FakeCLI.calls = 0
    _FakeCLI.original_error = None
    session = _make_session(tmp_path, monkeypatch)

    texts: list[str] = []
    with pytest.raises(SessionRestartRequired):
        async for msg in session.prompt("hello", max_restarts=1):
            if isinstance(msg, TextPart):
                texts.append(msg.text)

    assert _FakeCLI.calls == 2
    assert any("Connection lost" in t for t in texts)
