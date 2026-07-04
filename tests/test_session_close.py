"""Tests for kimi_agent_sdk.Session cleanup behavior."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from kimi_agent_sdk._session import Session


@dataclass
class _FakeChatProvider:
    closed: bool = False
    raise_on_close: bool = False
    close_error_message: str = "boom"

    async def aclose(self) -> None:
        if self.raise_on_close:
            raise RuntimeError(self.close_error_message)
        self.closed = True


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
    closed: bool = False

    async def close(self) -> None:
        self.closed = True


@dataclass
class _FakePath:
    _exists: bool = False
    suffix: str = ""

    def exists(self) -> bool:
        return self._exists

    def unlink(self) -> None:
        pass

    def __truediv__(self, other: str) -> "_FakePath":
        return _FakePath()

    def with_suffix(self, suffix: str) -> "_FakePath":
        return _FakePath(suffix=suffix)


@dataclass
class _FakeWireFile:
    path: _FakePath = field(default_factory=_FakePath)


@dataclass
class _FakeCLISession:
    work_dir: Any = field(default_factory=lambda: type("W", (), {})())
    session_id: str = "test"
    context_file: _FakePath = field(default_factory=_FakePath)
    dir: _FakePath = field(default_factory=_FakePath)
    wire_file: _FakeWireFile = field(default_factory=_FakeWireFile)
    custom_data: dict[str, Any] = field(default_factory=dict)
    custom_config: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.session_id

    async def close_context_db(self) -> None:
        pass


@dataclass
class _FakeCLI:
    soul: _FakeSoul
    session: _FakeCLISession


def _make_session(chat_provider: _FakeChatProvider | None = None) -> Session:
    provider = chat_provider or _FakeChatProvider()
    runtime = _FakeRuntime(llm=_FakeLLM(chat_provider=provider))
    soul = _FakeSoul(agent=_FakeAgent(), _runtime=runtime)
    cli = _FakeCLI(soul=soul, session=_FakeCLISession())
    return Session(cli)


@pytest.mark.asyncio
async def test_close_calls_chat_provider_aclose() -> None:
    provider = _FakeChatProvider()
    session = _make_session(provider)
    await session.close()
    assert provider.closed


@pytest.mark.asyncio
async def test_close_swallows_event_loop_closed_error() -> None:
    provider = _FakeChatProvider(
        raise_on_close=True,
        close_error_message="Event loop is closed",
    )
    session = _make_session(provider)
    # Should not raise for the specific event-loop-closed cleanup error.
    await session.close()


@pytest.mark.asyncio
async def test_close_re_raises_other_chat_provider_aclose_errors() -> None:
    provider = _FakeChatProvider(raise_on_close=True)
    session = _make_session(provider)
    # RuntimeErrors other than "Event loop is closed" should propagate.
    with pytest.raises(RuntimeError, match="boom"):
        await session.close()


@pytest.mark.asyncio
async def test_clear_calls_chat_provider_aclose(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeChatProvider()
    session = _make_session(provider)
    async def noop() -> None:
        pass

    monkeypatch.setattr(session, "_cleanup_tools", noop)

    called = False

    async def fake_recreate(*args: Any, **kwargs: Any) -> _FakeCLI:
        nonlocal called
        called = True
        return _FakeCLI(
            soul=_FakeSoul(agent=_FakeAgent(), _runtime=_FakeRuntime(llm=None)),
            session=_FakeCLISession(),
        )

    import kimi_agent_sdk._session as session_mod

    monkeypatch.setattr(session_mod.CliSession, "create", classmethod(lambda cls, *a, **kw: fake_recreate(*a, **kw)))
    monkeypatch.setattr(session_mod.KimiCLI, "create", classmethod(lambda cls, *a, **kw: fake_recreate(*a, **kw)))

    await session.clear()
    assert provider.closed
    assert called


@pytest.mark.asyncio
async def test_clear_closes_soul(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeChatProvider()
    session = _make_session(provider)
    soul = session._cli.soul

    async def noop() -> None:
        pass

    monkeypatch.setattr(session, "_cleanup_tools", noop)

    async def fake_recreate(*args: Any, **kwargs: Any) -> _FakeCLI:
        return _FakeCLI(
            soul=_FakeSoul(agent=_FakeAgent(), _runtime=_FakeRuntime(llm=None)),
            session=_FakeCLISession(),
        )

    import kimi_agent_sdk._session as session_mod

    monkeypatch.setattr(session_mod.CliSession, "create", classmethod(lambda cls, *a, **kw: fake_recreate(*a, **kw)))
    monkeypatch.setattr(session_mod.KimiCLI, "create", classmethod(lambda cls, *a, **kw: fake_recreate(*a, **kw)))

    await session.clear()
    assert soul.closed


@pytest.mark.asyncio
async def test_rename_calls_chat_provider_aclose(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _FakeChatProvider()
    session = _make_session(provider)
    async def noop() -> None:
        pass

    monkeypatch.setattr(session, "_cleanup_tools", noop)

    called = False

    async def fake_recreate(*args: Any, **kwargs: Any) -> _FakeCLI:
        nonlocal called
        called = True
        return _FakeCLI(
            soul=_FakeSoul(agent=_FakeAgent(), _runtime=_FakeRuntime(llm=None)),
            session=_FakeCLISession(),
        )

    import kimi_agent_sdk._session as session_mod

    async def fake_rename(*args: Any, **kwargs: Any) -> _FakeCLISession:
        return _FakeCLISession(session_id=args[1] if len(args) > 1 else "new")

    monkeypatch.setattr(session_mod.CliSession, "rename", classmethod(lambda cls, *a, **kw: fake_rename(*a, **kw)))
    monkeypatch.setattr(session_mod.CliSession, "create", classmethod(lambda cls, *a, **kw: _FakeCLISession()))
    monkeypatch.setattr(session_mod.KimiCLI, "create", classmethod(lambda cls, *a, **kw: fake_recreate(*a, **kw)))
    async def fake_load_config(work_dir: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(session_mod, "_load_config_json", fake_load_config)

    await session.rename("new")
    assert provider.closed
    assert called
