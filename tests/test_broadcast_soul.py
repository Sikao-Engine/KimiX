"""Soul-side tests for the cross-process ``broadcast`` channel.

``KimiSoul`` keeps a read cursor (``_broadcast_last_time``) into the shared
``.kimix_cache/_broadcast.db`` bus, initialized when the session is created.  A
background poller -- throttled to one read -> re-format -> steer cycle every
three seconds, never per turn -- steers what *other* processes published into
the running context, bounded by the model's context window.

These probes pin:
  * the cursor is created at session start and only moves forward;
  * external messages become steer messages, our own publishes do not;
  * the injected block respects the context budget (truncation), or the batch is
    deferred when there is no room;
  * an unconsumed steer rewinds the cursor instead of losing the message;
  * the poller is throttled, root-only, cancellable, and failure-isolated;
  * a session start prunes messages older than the retention window.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
from kaos.path import KaosPath
from kimi_cli.auth.oauth import OAuthManager
from kimi_cli.background import BackgroundTaskManager
from kimi_cli.config import get_default_config
from kimi_cli.llm import ALL_MODEL_CAPABILITIES, LLM
from kimi_cli.metadata import WorkDirMeta
from kimi_cli.notifications import NotificationManager
from kimi_cli.session import Session
from kimi_cli.session_state import SessionState
from kimi_cli.soul import run_soul
from kimi_cli.soul.agent import Agent, BuiltinSystemPromptArgs, LaborMarket, Runtime
from kimi_cli.soul.approval import Approval
from kimi_cli.soul.context import Context
from kimi_cli.soul.denwarenji import DenwaRenji
from kimi_cli.soul.kimisoul import (
    _BROADCAST_POLL_INTERVAL_S,
    _STEER_QUEUE_MAX,
    KimiSoul,
)
from kimi_cli.tools.broadcast import Broadcast, BroadcastParams
from kimi_cli.tools.broadcast.bus import (
    BroadcastBus,
    BroadcastError,
    _forget_local_publishes,
    broadcast_db_path,
    now_ms,
)
from kimi_cli.tools.broadcast.format import ELLIPSIS
from kimi_cli.utils.aioqueue import QueueShutDown
from kimi_cli.utils.environment import Environment
from kimi_cli.utils.tokens import count_tokens
from kimi_cli.wire import Wire
from kimi_cli.wire.file import WireFile
from kimi_cli.wire.types import TextPart
from kosong.message import ContentPart
from kosong.tooling.empty import EmptyToolset

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """Chat provider that replays texts, optionally slowly (for live pollers)."""

    def __init__(self, texts: list[str], *, delay: float = 0.0) -> None:
        self._texts = list(texts) or ["ok"]
        self._calls = 0
        self._delay = delay

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def model_name(self) -> str:
        return "scripted"

    @property
    def thinking_effort(self) -> None:
        return None

    def with_thinking(self, effort: Any) -> _ScriptedProvider:
        return self

    async def generate(
        self, system_prompt: str, tools: list[Any], history: list[Any]
    ) -> _ScriptedMessage:
        text = self._texts[min(self._calls, len(self._texts) - 1)]
        self._calls += 1
        return _ScriptedMessage([TextPart(text=text)], delay=self._delay)


class _ScriptedMessage:
    """Minimal streamed message yielding one text part per chunk of words."""

    def __init__(self, parts: list[ContentPart], *, delay: float) -> None:
        self._parts = list(parts)
        self._delay = delay

    def __aiter__(self) -> _ScriptedMessage:
        return self

    async def __anext__(self) -> ContentPart:
        if not self._parts:
            raise StopAsyncIteration
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._parts.pop(0)

    @property
    def id(self) -> str | None:
        return "scripted"

    @property
    def usage(self) -> None:
        return None


def _make_runtime(
    tmp_path: Path,
    *,
    provider: Any = None,
    max_context_size: int = 100_000,
    role: str = "root",
) -> Runtime:
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    share_dir = tmp_path / "share"
    share_dir.mkdir(parents=True, exist_ok=True)

    work_kaos = KaosPath.unsafe_from_local_path(work_dir)
    config = get_default_config()
    llm = LLM(
        chat_provider=provider or _ScriptedProvider(["done"]),
        max_context_size=max_context_size,
        capabilities=ALL_MODEL_CAPABILITIES,
    )
    builtin_args = BuiltinSystemPromptArgs(
        KIMI_WORK_DIR=work_kaos,
        KIMI_SKILLS="No skills found.",
        KIMI_OS="Windows",
    )
    session = Session(
        id="broadcast-test",
        work_dir=work_kaos,
        work_dir_meta=WorkDirMeta(path=str(work_kaos), kaos="local"),
        context_file=share_dir / "context.jsonl",
        wire_file=WireFile(path=share_dir / "wire.jsonl"),
        state=SessionState(),
        title="Broadcast Test",
        updated_at=0.0,
        custom_data={},
        custom_config={},
    )
    notifications = NotificationManager(share_dir / "notifications", config.notifications)
    return Runtime(
        config=config,
        llm=llm,
        builtin_args=builtin_args,
        denwa_renji=DenwaRenji(),
        session=session,
        approval=Approval(yolo=True),
        labor_market=LaborMarket(),
        environment=Environment(
            os_kind="Windows",
            os_arch="x86_64",
            os_version="1.0",
            shell_name="pwsh",
            shell_path=KaosPath(r"C:\Program Files\Git\bin\bash.exe"),
        ),
        notifications=notifications,
        background_tasks=BackgroundTaskManager(
            session, config.background, notifications=notifications
        ),
        skills={},
        oauth=OAuthManager(config),
        additional_dirs=[],
        skills_dirs=[],
        role=role,  # type: ignore[arg-type]
    )


def _make_soul(tmp_path: Path, runtime: Runtime) -> KimiSoul:
    agent = Agent(
        name="Broadcast Test Agent",
        system_prompt="Test prompt.",
        toolset=EmptyToolset(),
        runtime=runtime,
    )
    return KimiSoul(agent, context=Context(file_backend=tmp_path / "context.jsonl"))


def _peer_bus(work_dir: Path) -> BroadcastBus:
    """A bus standing in for another agent process in the same work dir."""
    return BroadcastBus(work_dir=work_dir)


def _drain_steers(soul: KimiSoul) -> list[Any]:
    drained = []
    while not soul._steer_queue.empty():
        drained.append(soul._steer_queue.get_nowait())
    return drained


@pytest.fixture(autouse=True)
def _clean_local_publishes():
    """Keep the process-wide own-publish registry from leaking between tests."""
    _forget_local_publishes()
    yield
    _forget_local_publishes()


@pytest.fixture
def soul(tmp_path: Path) -> KimiSoul:
    return _make_soul(tmp_path, _make_runtime(tmp_path))


# ---------------------------------------------------------------------------
# Read cursor
# ---------------------------------------------------------------------------


def test_read_cursor_starts_at_session_creation(tmp_path: Path) -> None:
    started = now_ms()
    soul = _make_soul(tmp_path, _make_runtime(tmp_path))
    finished = now_ms()
    assert started <= soul._broadcast_last_time <= finished


def test_poll_default_interval_is_three_seconds(soul: KimiSoul) -> None:
    assert soul._broadcast_poll_interval == 3.0
    assert _BROADCAST_POLL_INTERVAL_S == 3.0


def test_bus_lives_in_the_session_work_dir(soul: KimiSoul, tmp_path: Path) -> None:
    assert soul._broadcast_bus.path == broadcast_db_path(tmp_path / "work")


# ---------------------------------------------------------------------------
# Delivering external messages
# ---------------------------------------------------------------------------


async def test_poll_steers_external_messages(soul: KimiSoul, tmp_path: Path) -> None:
    peer = _peer_bus(tmp_path / "work")
    try:
        published = peer.append("peer says hello")
    finally:
        peer.close()

    assert await soul._broadcast_poll_once() == 1
    steered = _drain_steers(soul)
    assert len(steered) == 1
    assert "peer says hello" in steered[0]
    assert "broadcast" in steered[0]
    # Cursor advanced past the delivered key, so a second tick is a no-op.
    assert soul._broadcast_last_time > published.time
    assert await soul._broadcast_poll_once() == 0
    assert soul._broadcast_delivered == 1
    assert soul._broadcast_polls == 2


async def test_poll_batches_everything_since_the_cursor(soul: KimiSoul, tmp_path: Path) -> None:
    peer = _peer_bus(tmp_path / "work")
    try:
        for index in range(3):
            peer.append(f"message-{index}")
    finally:
        peer.close()

    assert await soul._broadcast_poll_once() == 3
    steered = _drain_steers(soul)
    assert len(steered) == 1  # one steer message per poll batch
    for index in range(3):
        assert f"message-{index}" in steered[0]


async def test_poll_ignores_own_publishes(soul: KimiSoul, tmp_path: Path) -> None:
    tool = Broadcast(soul.runtime)
    assert tool.name == "broadcast"
    result = await tool(BroadcastParams(content="my own note"))
    assert result.is_error is False, result

    assert await soul._broadcast_poll_once() == 0
    assert _drain_steers(soul) == []
    # The cursor still moves past our own key, otherwise it would be re-read.
    assert soul._broadcast_last_time > 0
    with BroadcastBus(work_dir=tmp_path / "work") as peer:
        assert len(peer.read_since(0)) == 1


async def test_poll_keeps_foreign_messages_when_mixed_with_own(
    soul: KimiSoul, tmp_path: Path
) -> None:
    tool = Broadcast(soul.runtime)
    await tool(BroadcastParams(content="own note"))
    peer = _peer_bus(tmp_path / "work")
    try:
        peer.append("foreign note")
    finally:
        peer.close()

    assert await soul._broadcast_poll_once() == 1
    steered = _drain_steers(soul)
    assert "foreign note" in steered[0]
    assert "own note" not in steered[0]


# ---------------------------------------------------------------------------
# Context-window safety
# ---------------------------------------------------------------------------


async def test_injection_is_truncated_to_the_context_window(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, max_context_size=4_000)
    soul = _make_soul(tmp_path, runtime)
    budget = soul._broadcast_token_budget()
    assert 0 < budget < 4_000

    peer = _peer_bus(tmp_path / "work")
    try:
        peer.append("Z" * 20_000)
    finally:
        peer.close()

    assert await soul._broadcast_poll_once() == 1
    steered = _drain_steers(soul)
    assert len(steered) == 1
    assert count_tokens(steered[0]) <= budget
    assert ELLIPSIS in steered[0]


async def test_poll_defers_when_the_context_has_no_room(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, max_context_size=1_024)
    soul = _make_soul(tmp_path, runtime)
    assert soul._broadcast_token_budget() == 0

    peer = _peer_bus(tmp_path / "work")
    try:
        published = peer.append("cannot fit yet")
    finally:
        peer.close()

    cursor_before = soul._broadcast_last_time
    assert await soul._broadcast_poll_once() == 0
    assert _drain_steers(soul) == []
    # Deferred, not dropped: the cursor stays put so a later tick retries.
    assert soul._broadcast_last_time == cursor_before
    assert soul._broadcast_last_time <= published.time


async def test_poll_defers_when_the_steer_queue_is_full(
    soul: KimiSoul, tmp_path: Path
) -> None:
    """A full ring drops the *oldest* steer, so a broadcast must not be enqueued."""
    peer = _peer_bus(tmp_path / "work")
    try:
        published = peer.append("queue full probe")
    finally:
        peer.close()

    for _ in range(_STEER_QUEUE_MAX):
        soul._steer_queue.put_nowait("user steer")
    cursor_before = soul._broadcast_last_time

    assert await soul._broadcast_poll_once() == 0
    # Deferred, not lost: cursor untouched and nothing was silently evicted.
    assert soul._broadcast_last_time == cursor_before
    assert soul._steer_queue.qsize() == _STEER_QUEUE_MAX

    soul._steer_queue.get_nowait()  # the loop consumed one
    assert await soul._broadcast_poll_once() == 1
    assert "queue full probe" in _drain_steers(soul)[-1]
    assert soul._broadcast_last_time > published.time


# ---------------------------------------------------------------------------
# Cursor rewind (a steer that the loop never consumed)
# ---------------------------------------------------------------------------


async def test_cursor_rewinds_when_the_steer_was_not_consumed(
    soul: KimiSoul, tmp_path: Path
) -> None:
    peer = _peer_bus(tmp_path / "work")
    try:
        peer.append("late message")
    finally:
        peer.close()

    assert await soul._broadcast_poll_once() == 1
    advanced = soul._broadcast_last_time
    # The turn ended before the agent loop drained the steer queue.
    soul._restore_broadcast_cursor_if_unconsumed()
    assert soul._broadcast_last_time < advanced

    _drain_steers(soul)
    assert await soul._broadcast_poll_once() == 1
    assert "late message" in _drain_steers(soul)[0]


async def test_cursor_is_kept_when_the_steer_was_consumed(soul: KimiSoul, tmp_path: Path) -> None:
    peer = _peer_bus(tmp_path / "work")
    try:
        peer.append("consumed message")
    finally:
        peer.close()

    assert await soul._broadcast_poll_once() == 1
    _drain_steers(soul)  # the loop consumed it
    advanced = soul._broadcast_last_time
    soul._restore_broadcast_cursor_if_unconsumed()
    assert soul._broadcast_last_time == advanced

# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


async def test_poll_survives_a_broken_bus(soul: KimiSoul, monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any):
        raise BroadcastError("database is locked by another process")

    monkeypatch.setattr(BroadcastBus, "read_since", _boom)
    assert await soul._broadcast_poll_once() == 0
    assert _drain_steers(soul) == []


async def test_poll_survives_an_unexpected_error(
    soul: KimiSoul, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: Any, **kwargs: Any):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(BroadcastBus, "read_since", _boom)
    assert await soul._broadcast_poll_once() == 0


async def test_poll_survives_a_corrupt_database(soul: KimiSoul) -> None:
    soul._broadcast_bus.path.parent.mkdir(parents=True, exist_ok=True)
    soul._broadcast_bus.close()
    soul._broadcast_bus.path.write_bytes(b"not a database" * 64)
    # The bus quarantines the broken file; the poll reports "nothing" instead
    # of raising, and the next publish recreates the database.
    assert await soul._broadcast_poll_once() == 0
    peer = _peer_bus(soul._broadcast_bus.path.parent.parent)
    try:
        peer.append("after corruption")
    finally:
        peer.close()
    assert await soul._broadcast_poll_once() == 1


# ---------------------------------------------------------------------------
# Poller task: throttling, root-only, cancellation
# ---------------------------------------------------------------------------


async def test_poller_ticks_are_throttled_by_the_interval(soul: KimiSoul) -> None:
    ticks: list[float] = []

    async def _tick() -> int:
        ticks.append(time.monotonic())
        return 0

    soul._broadcast_poll_interval = 0.05
    soul._broadcast_poll_once = _tick  # type: ignore[method-assign]
    soul._start_broadcast_poller()
    await asyncio.sleep(0.3)
    await soul._stop_broadcast_poller()

    assert len(ticks) >= 2, f"poller only ticked {len(ticks)} time(s)"
    gaps = [later - earlier for earlier, later in zip(ticks, ticks[1:])]
    assert all(gap >= 0.04 for gap in gaps), gaps


async def test_poller_reads_a_backlog_right_away_on_a_fresh_turn(soul: KimiSoul) -> None:
    """The throttle is anchored to the previous poll, not to the loop start."""
    soul._broadcast_poll_interval = 3.0
    soul._broadcast_last_poll_at = 0.0  # nothing polled in this soul yet
    ticks: list[float] = []

    async def _tick() -> int:
        ticks.append(time.monotonic())
        return 0

    soul._broadcast_poll_once = _tick  # type: ignore[method-assign]
    started = time.monotonic()
    soul._start_broadcast_poller()
    await asyncio.sleep(0.05)
    await soul._stop_broadcast_poller()

    assert ticks, "a turn starting after an idle stretch must not wait a full interval"
    assert ticks[0] - started < 1.0


async def test_poller_honours_the_throttle_after_a_recent_poll(soul: KimiSoul) -> None:
    soul._broadcast_poll_interval = 0.2
    ticks: list[float] = []

    async def _tick() -> int:
        ticks.append(time.monotonic())
        return 0

    soul._broadcast_poll_once = _tick  # type: ignore[method-assign]
    started = time.monotonic()
    soul._broadcast_last_poll_at = started  # we just polled
    soul._start_broadcast_poller()
    await asyncio.sleep(0.1)
    assert ticks == [], "the poller must stay quiet inside the interval window"
    await asyncio.sleep(0.2)
    await soul._stop_broadcast_poller()

    assert ticks, "the poller must resume once the interval elapsed"
    assert ticks[0] - started >= 0.15


async def test_poller_is_not_started_twice(soul: KimiSoul) -> None:
    soul._broadcast_poll_interval = 0.05
    soul._start_broadcast_poller()
    first = soul._broadcast_task
    soul._start_broadcast_poller()
    assert soul._broadcast_task is first
    await soul._stop_broadcast_poller()
    assert soul._broadcast_task is None


async def test_poller_is_not_started_for_subagents(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, role="subagent")
    soul = _make_soul(tmp_path, runtime)
    soul._start_broadcast_poller()
    assert soul._broadcast_task is None


async def test_poller_stop_is_safe_without_a_task(soul: KimiSoul) -> None:
    await soul._stop_broadcast_poller()
    assert soul._broadcast_task is None


# ---------------------------------------------------------------------------
# Retention at session start
# ---------------------------------------------------------------------------


def test_session_start_prunes_messages_older_than_seven_days(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    preserved_bus = BroadcastBus(work_dir=work_dir)
    day_ms = 86_400_000
    try:
        preserved_bus.append("8 days old", timestamp=now_ms() - 8 * day_ms)
        preserved_bus.append("2 days old", timestamp=now_ms() - 2 * day_ms)
    finally:
        preserved_bus.close()

    soul = _make_soul(tmp_path, _make_runtime(tmp_path))
    assert soul._broadcast_pruned == 1
    with BroadcastBus(work_dir=work_dir) as bus:
        remaining = [row.content for row in bus.read_since(0)]
    assert remaining == ["2 days old"]


def test_session_start_without_database_is_a_noop(tmp_path: Path) -> None:
    soul = _make_soul(tmp_path, _make_runtime(tmp_path))
    assert soul._broadcast_pruned == 0
    assert not soul._broadcast_bus.path.exists()


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------


async def test_tool_publishes_to_the_shared_bus(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    tool = Broadcast(runtime)
    result = await tool(BroadcastParams(content="hello peers"))
    assert result.is_error is False, result
    output = getattr(result, "output", "")
    assert ".kimix_cache" in output and "_broadcast.db" in output

    with BroadcastBus(work_dir=tmp_path / "work") as bus:
        rows = bus.read_since(0)
    assert [row.content for row in rows] == ["hello peers"]


async def test_tool_accepts_the_text_alias(tmp_path: Path) -> None:
    tool = Broadcast(_make_runtime(tmp_path))
    params = BroadcastParams.model_validate({"text": "aliased"})
    assert params.content == "aliased"
    assert (await tool(params)).is_error is False


async def test_tool_rejects_blank_content(tmp_path: Path) -> None:
    tool = Broadcast(_make_runtime(tmp_path))
    result = await tool(BroadcastParams(content="   "))
    assert result.is_error is True


async def test_tool_reports_a_broken_database_as_tool_error(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path)
    # ``.kimix_cache`` is a file, so no database can ever be created there.
    (tmp_path / "work" / ".kimix_cache").write_text("not a directory")
    tool = Broadcast(runtime)
    result = await tool(BroadcastParams(content="doomed"))
    assert result.is_error is True
    assert "broadcast" in str(getattr(result, "message", "")).lower()


# ---------------------------------------------------------------------------
# End-to-end: a real turn picks up a peer message
# ---------------------------------------------------------------------------


async def test_running_turn_picks_up_a_peer_broadcast(tmp_path: Path) -> None:
    provider = _ScriptedProvider(["working on it", "done"], delay=0.25)
    runtime = _make_runtime(tmp_path, provider=provider)
    soul = _make_soul(tmp_path, runtime)
    soul._broadcast_poll_interval = 0.05

    peer = _peer_bus(tmp_path / "work")
    try:
        peer.append("please also check the docs")
    finally:
        peer.close()

    async def ui_loop(wire: Wire) -> None:
        wire_ui = wire.ui_side(merge=False)
        while True:
            try:
                await wire_ui.receive()
            except QueueShutDown:
                return

    await run_soul(soul, "do the thing", ui_loop, asyncio.Event())

    user_messages = [
        "".join(part.text for part in message.content if isinstance(part, TextPart))
        for message in soul.context.history
        if message.role == "user"
    ]
    broadcast_messages = [text for text in user_messages if "[broadcast]" in text]
    assert broadcast_messages, user_messages
    assert "please also check the docs" in broadcast_messages[0]
    assert soul._broadcast_delivered >= 1
    assert soul._broadcast_task is None


async def test_poller_steer_is_never_swallowed_by_turn_init_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poller must start *after* ``_agent_loop`` discards stale steers.

    The anchored throttle makes the poller's first tick immediate.  A poller
    started earlier (e.g. from ``run``) could therefore steer a batch while the
    turn is still initialising; turn init then drops it as a stale steer even
    though the read cursor already moved past it — the messages would be lost.

    The poll is stubbed with a synchronous steer so the ordering is exercised
    deterministically instead of racing the real off-thread database read.
    """
    provider = _ScriptedProvider(["working", "done"], delay=0.1)
    runtime = _make_runtime(tmp_path, provider=provider)
    soul = _make_soul(tmp_path, runtime)
    soul._broadcast_poll_interval = 3.0
    delivered: list[str] = []

    async def _fake_poll() -> int:
        soul._broadcast_polls += 1
        text = "synchronously delivered batch"
        soul.steer(text)
        delivered.append(text)
        return 1

    monkeypatch.setattr(soul, "_broadcast_poll_once", _fake_poll)

    async def ui_loop(wire: Wire) -> None:
        wire_ui = wire.ui_side(merge=False)
        while True:
            try:
                await wire_ui.receive()
            except QueueShutDown:
                return

    await run_soul(soul, "do the work", ui_loop, asyncio.Event())

    assert delivered, "the stubbed poll never ran"
    user_texts = [
        "".join(part.text for part in message.content if isinstance(part, TextPart))
        for message in soul.context.history
        if message.role == "user"
    ]
    assert any("synchronously delivered batch" in text for text in user_texts), user_texts


async def test_running_turn_is_not_broken_by_an_unusable_bus(tmp_path: Path) -> None:
    runtime = _make_runtime(tmp_path, provider=_ScriptedProvider(["fine"]))
    soul = _make_soul(tmp_path, runtime)
    soul._broadcast_poll_interval = 0.01
    # A directory where the database file should be makes every bus call fail.
    soul._broadcast_bus.path.parent.mkdir(parents=True, exist_ok=True)
    soul._broadcast_bus.path.mkdir()
    soul._broadcast_bus.close()

    async def ui_loop(wire: Wire) -> None:
        wire_ui = wire.ui_side(merge=False)
        while True:
            try:
                await wire_ui.receive()
            except QueueShutDown:
                return

    await run_soul(soul, "still works", ui_loop, asyncio.Event())

    assert any(
        message.role == "assistant"
        and any(isinstance(part, TextPart) and "fine" in part.text for part in message.content)
        for message in soul.context.history
    )


_PEER_WORKER = textwrap.dedent(
    """
    import sys, time
    from kimi_cli.tools.broadcast.bus import BroadcastBus

    db_path, delay, text = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    time.sleep(delay)
    bus = BroadcastBus(db_path=db_path)
    try:
        bus.append(text)
    finally:
        bus.close()
    """
)


async def test_peer_process_broadcast_reaches_a_running_turn(tmp_path: Path) -> None:
    """The real cross-process path: another OS process publishes, we steer it."""
    provider = _ScriptedProvider(["first", "second"], delay=0.3)
    runtime = _make_runtime(tmp_path, provider=provider)
    soul = _make_soul(tmp_path, runtime)
    soul._broadcast_poll_interval = 0.05
    db_path = soul._broadcast_bus.path

    worker = subprocess.Popen(
        [sys.executable, "-c", _PEER_WORKER, str(db_path), "0", "peer process says hi"],
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = worker.communicate(timeout=120)
    assert worker.returncode == 0, stderr.decode("utf-8", "replace") or stdout.decode(
        "utf-8", "replace"
    )
    # The row is committed by another process before the turn starts, so only
    # the poller's read cursor (not a shared object) can deliver it.
    with BroadcastBus(db_path=db_path) as check:
        assert [row.content for row in check.read_since(0)] == ["peer process says hi"]

    async def ui_loop(wire: Wire) -> None:
        wire_ui = wire.ui_side(merge=False)
        while True:
            try:
                await wire_ui.receive()
            except QueueShutDown:
                return

    await run_soul(soul, "work while a peer talks", ui_loop, asyncio.Event())

    user_texts = [
        "".join(part.text for part in message.content if isinstance(part, TextPart))
        for message in soul.context.history
        if message.role == "user"
    ]
    assert any("peer process says hi" in text for text in user_texts), user_texts
