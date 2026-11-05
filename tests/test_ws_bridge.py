"""Functional tests for the WebSocket -> TCP bridge in ``kimix.network.rpc_server``.

The bridge is the only place where a *thread* (the TCP reader) hands frames to an
*event loop* (the WebSocket sender).  It used to buffer those frames in an
unbounded ``asyncio.Queue``; the queue is now bounded and every mutation happens
on the loop thread, so these tests pin both the delivery path and the overflow
behaviour.  There was no coverage here before, which is how a
``run_coroutine_threadsafe``-a-plain-function bug could ship.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from kimix.network import rpc_server


class _FakeWebSocket:
    """Minimal stand-in for a websockets server connection."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        self._send_gate: asyncio.Event | None = None

    def block_sends(self) -> asyncio.Event:
        self._send_gate = asyncio.Event()
        return self._send_gate

    async def send(self, message: str) -> None:
        if self._send_gate is not None:
            await self._send_gate.wait()
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait("__stop__")

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        item = await self._incoming.get()
        if item == "__stop__":
            raise StopAsyncIteration
        return item


class _FakeTCPClient:
    """Records callbacks so the test can drive them from another thread."""

    instances: list[_FakeTCPClient] = []

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sent: list[str] = []
        self.connected = False
        self._on_message = None
        self._on_disconnect = None
        _FakeTCPClient.instances.append(self)

    def on_message(self, cb) -> None:  # noqa: ANN001
        self._on_message = cb

    def on_disconnect(self, cb) -> None:  # noqa: ANN001
        self._on_disconnect = cb

    def connect(self, blocking: bool = False) -> bool:
        self.connected = True
        return True

    def send(self, message: str) -> None:
        self.sent.append(message)

    def disconnect(self) -> None:
        self.connected = False

    # helpers used by the tests
    def emit(self, message: str) -> None:
        assert self._on_message is not None
        self._on_message(message)

    def drop(self) -> None:
        assert self._on_disconnect is not None
        self._on_disconnect()


@pytest.fixture
def bridge(monkeypatch: pytest.MonkeyPatch):
    _FakeTCPClient.instances.clear()
    monkeypatch.setattr(rpc_server, "TCPClient", _FakeTCPClient)
    server = rpc_server.JSONRPCServer(host="127.0.0.1", port=0)
    return server


async def _cancel(task: "asyncio.Task[None]") -> None:
    """Cancel *task* and swallow whatever it raises while unwinding."""
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass


async def _wait_until(predicate, *, timeout: float = 5.0) -> None:  # noqa: ANN001
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


async def test_bridge_forwards_frames_to_the_websocket(bridge) -> None:
    ws = _FakeWebSocket()
    task = asyncio.create_task(bridge._ws_handler(ws))
    try:
        await _wait_until(lambda: _FakeTCPClient.instances)
        tcp = _FakeTCPClient.instances[0]
        await _wait_until(lambda: tcp.connected)

        for i in range(20):
            tcp.emit(f"frame-{i}")
        await _wait_until(lambda: len(ws.sent) == 20)
        assert ws.sent == [f"frame-{i}" for i in range(20)]
    finally:
        await ws.close()
        await _cancel(task)


async def test_bridge_callbacks_cross_threads_without_errors(bridge) -> None:
    """The producer runs on another thread, like the real TCP reader."""
    ws = _FakeWebSocket()
    errors: list[BaseException] = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, ctx: errors.append(ctx.get("exception")))

    task = asyncio.create_task(bridge._ws_handler(ws))
    try:
        await _wait_until(lambda: _FakeTCPClient.instances)
        tcp = _FakeTCPClient.instances[0]
        await _wait_until(lambda: tcp.connected)

        def producer() -> None:
            for i in range(100):
                tcp.emit(f"t-{i}")

        thread = threading.Thread(target=producer)
        thread.start()
        while thread.is_alive():
            await asyncio.sleep(0.005)
        thread.join()
        await _wait_until(lambda: len(ws.sent) == 100)
        assert ws.sent[0] == "t-0" and ws.sent[-1] == "t-99"
        assert not errors
    finally:
        await ws.close()
        await _cancel(task)


async def test_bridge_overflow_closes_the_connection_instead_of_growing(bridge) -> None:
    ws = _FakeWebSocket()
    gate = ws.block_sends()  # the forwarder is stuck -> the queue must fill up
    task = asyncio.create_task(bridge._ws_handler(ws))
    try:
        await _wait_until(lambda: _FakeTCPClient.instances)
        tcp = _FakeTCPClient.instances[0]
        await _wait_until(lambda: tcp.connected)

        limit = rpc_server._WS_SEND_QUEUE_MAX
        for i in range(limit + 50):
            tcp.emit(f"frame-{i}")
            await asyncio.sleep(0)

        await _wait_until(lambda: ws.closed)
        gate.set()

        # Everything the client actually received is a prefix-ordered subset:
        # frames are never reordered, and the overflow drops the *backlog*.
        await _wait_until(lambda: task.done() or len(ws.sent) >= 1)
        assert len(ws.sent) <= limit + 1
        received = [f for f in ws.sent if f.startswith("frame-")]
        assert received == received[: len(received)]
        assert [int(f.split("-")[1]) for f in received] == list(range(len(received)))
    finally:
        gate.set()
        await ws.close()
        await _cancel(task)


async def test_bridge_disconnect_terminates_the_stream(bridge) -> None:
    ws = _FakeWebSocket()
    task = asyncio.create_task(bridge._ws_handler(ws))
    try:
        await _wait_until(lambda: _FakeTCPClient.instances)
        tcp = _FakeTCPClient.instances[0]
        await _wait_until(lambda: tcp.connected)

        tcp.emit("hello")
        await _wait_until(lambda: ws.sent == ["hello"])

        tcp.drop()  # server-side disconnect
        await _wait_until(lambda: ws.closed)
        await asyncio.wait_for(task, timeout=5)
    finally:
        await _cancel(task)


def test_send_queue_is_bounded_by_construction() -> None:
    """A bounded queue is the whole point: assert the constant is sane."""
    assert 0 < rpc_server._WS_SEND_QUEUE_MAX <= 4096
