"""Tests for the SDK session ``_done`` flag lifecycle in ``Session.prompt``.

The flag tracks the last LLM stream lifecycle:
- ``False`` initially and while a stream is running,
- ``True`` only when the remote server finished the stream normally,
- ``False`` after cancel / restart / error,
- reset to ``False`` by ``clear()``.

No network access: ``session._cli.run`` is stubbed as an async generator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pytest
from kaos.path import KaosPath
from kimi_cli.soul import RunCancelled
from kimi_cli.wire.types import TextPart

from kimi_agent_sdk._session import Session

# An async generator script: receives the cancel event, yields messages,
# and may raise to simulate failures.
RunScript = Callable[[Any], Any]


class _FakeChatProvider:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
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


class _ScriptedCLI:
    """Fake CLI whose ``run`` behavior is supplied per-test as a script."""

    def __init__(self, session: _FakeCLISession, script: RunScript) -> None:
        self.session = session
        self.soul = _FakeSoul(
            agent=_FakeAgent(),
            _runtime=_FakeRuntime(llm=_FakeLLM(chat_provider=_FakeChatProvider())),
        )
        self._script = script

    def run(
        self,
        user_input: Any,
        cancel_event: Any,
        *,
        merge_wire_messages: bool = False,
    ) -> Any:
        return self._script(cancel_event)


def _make_session(tmp_path: Path, script: RunScript) -> Session:
    session_dir = tmp_path / "sessions" / "s1"
    session_dir.mkdir(parents=True)
    context_file = session_dir / "context.jsonl"
    context_file.write_text('{"role": "user", "content": "previous turn"}\n', encoding="utf-8")
    (session_dir / "state.json").write_text("{}", encoding="utf-8")
    cli_session = _FakeCLISession(
        work_dir=KaosPath.unsafe_from_local_path(tmp_path),
        session_id="s1",
        context_file=context_file,
        dir=session_dir,
        wire_file=_FakeWireFile(session_dir / "wire.jsonl"),
        custom_data={},
        custom_config={},
    )
    session = Session(_ScriptedCLI(cli_session, script))
    session._create_kwargs = {"yolo": True}
    return session


async def _drain(gen: Any) -> list[Any]:
    return [msg async for msg in gen]


def test_done_flag_false_initially(tmp_path: Path) -> None:
    async def _script(cancel_event: Any) -> Any:
        yield TextPart(text="ok")
        return

    session = _make_session(tmp_path, _script)
    assert session._done is False


async def test_done_flag_true_after_normal_stream_finish(tmp_path: Path) -> None:
    async def _script(cancel_event: Any) -> Any:
        yield TextPart(text="done")

    session = _make_session(tmp_path, _script)
    messages = await _drain(session.prompt("hi"))
    assert any(isinstance(m, TextPart) for m in messages)
    assert session._done is True


async def test_done_flag_false_after_cancel(tmp_path: Path) -> None:
    import asyncio

    started = asyncio.Event()

    async def _script(cancel_event: Any) -> Any:
        started.set()
        yield TextPart(text="partial")
        await cancel_event.wait()
        raise RunCancelled("cancelled")

    session = _make_session(tmp_path, _script)
    gen = session.prompt("hi")
    # Pull the first message so the stream is inside the async-for body,
    # then cancel; RunCancelled must propagate and leave the flag False.
    await gen.__anext__()
    session.cancel()
    with pytest.raises(RunCancelled):
        await _drain(gen)
    assert session._done is False


async def test_done_flag_reset_at_stream_start(tmp_path: Path) -> None:
    observed: list[bool] = []

    async def _first(cancel_event: Any) -> Any:
        yield TextPart(text="first")

    async def _second(cancel_event: Any) -> Any:
        observed.append(session._done)
        yield TextPart(text="second")

    session = _make_session(tmp_path, _first)
    await _drain(session.prompt("hi"))
    assert session._done is True  # previous run finished normally

    session._cli._script = _second  # type: ignore[attr-defined]
    await _drain(session.prompt("again"))
    # The flag was cleared when the second stream started, even though the
    # first stream had finished normally.
    assert observed == [False]
    assert session._done is True


async def test_done_flag_false_on_error(tmp_path: Path) -> None:
    async def _script(cancel_event: Any) -> Any:
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator

    session = _make_session(tmp_path, _script)
    with pytest.raises(RuntimeError, match="boom"):
        await _drain(session.prompt("hi"))
    assert session._done is False


async def test_clear_resets_done_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _script(cancel_event: Any) -> Any:
        yield TextPart(text="done")

    session = _make_session(tmp_path, _script)
    await _drain(session.prompt("hi"))
    assert session._done is True

    import kimi_agent_sdk._session as session_mod

    async def _noop_cleanup() -> None:
        return None

    async def fake_create_cli_session(*args: Any, **kwargs: Any) -> _FakeCLISession:
        return session._cli.session

    async def fake_create_cli(cli_session: _FakeCLISession, **kwargs: Any) -> _ScriptedCLI:
        return _ScriptedCLI(cli_session, _script)

    monkeypatch.setattr(session, "_cleanup_tools", _noop_cleanup)
    monkeypatch.setattr(session, "_close_chat_provider", _noop_cleanup)
    monkeypatch.setattr(
        session_mod.CliSession,
        "create",
        classmethod(lambda cls, *a, **kw: fake_create_cli_session(*a, **kw)),
    )
    monkeypatch.setattr(
        session_mod.KimiCLI,
        "create",
        classmethod(lambda cls, *a, **kw: fake_create_cli(a[0] if a else None, **kw)),
    )

    await session.clear()
    assert session._done is False
