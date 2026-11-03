"""Deterministic unit tests for the bytearray receive-buffer optimization in
``TCPServer._recv_all``, ``TCPClient._recv_all``, and
``TcpGroupServer._recv_all``.

Fake fragmented sockets are used instead of real network I/O so the tests are
fast and free of timing flakiness. The public contract under test:

* zero size returns ``b""`` without touching the socket;
* mid-message EOF (``b""``) returns ``None``;
* ``socket.timeout`` is retried until data arrives;
* a missing socket / unknown client returns ``None``;
* any other recv exception returns ``None``;
* the return value is exactly ``bytes`` with the full payload content,
  including for large payloads fragmented into many chunks.
"""
from __future__ import annotations

import socket
from typing import Callable, Optional

import pytest

from kimix.network.tcp_client import TCPClient
from kimix.network.tcp_group_server import TcpGroupServer
from kimix.network.tcp_server import TCPServer

# Pattern shared by the payload builders (deterministic, no RNG).
_PATTERN = bytes(range(256))


class FakeFragmentedSocket:
    """Deterministic fake socket yielding data in fixed-size chunks.

    Raises ``socket.timeout`` exactly ``timeouts`` times before serving data,
    then returns at most ``chunk_size`` bytes per call (never more than the
    requested ``bufsize``), and finally returns ``b""`` (EOF) once the payload
    is exhausted, mirroring real ``socket.recv`` semantics.
    """

    def __init__(self, payload: bytes, chunk_size: int = 1, timeouts: int = 0) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._payload = payload
        self._chunk_size = chunk_size
        self._remaining_timeouts = timeouts
        self._pos = 0
        self.recv_calls = 0

    def recv(self, bufsize: int) -> bytes:
        self.recv_calls += 1
        if self._remaining_timeouts > 0:
            self._remaining_timeouts -= 1
            raise socket.timeout("fake timeout")
        if self._pos >= len(self._payload):
            return b""  # EOF
        end = self._pos + min(self._chunk_size, bufsize)
        chunk = self._payload[self._pos:end]
        self._pos = end
        return chunk


class FakeErrorSocket:
    """Fake socket whose recv() always raises the given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def recv(self, bufsize: int) -> bytes:
        raise self._exc


def _bind_impl(kind: str) -> tuple[Callable[[int], Optional[bytes]], Callable[[object], None]]:
    """Return (recv_all, attach) for the named implementation."""
    if kind == "server":
        server = TCPServer()
        return server._recv_all, lambda fake: setattr(server, "_client_socket", fake)
    if kind == "client":
        client = TCPClient()
        return client._recv_all, lambda fake: setattr(client, "_socket", fake)
    assert kind == "group", kind
    group = TcpGroupServer()
    return (
        lambda size: group._recv_all(7, size),
        lambda fake: group._clients.__setitem__(7, fake),
    )


IMPLEMENTATIONS = ["server", "client", "group"]


@pytest.mark.parametrize("kind", IMPLEMENTATIONS)
class TestRecvAllBehavior:
    """Behavioral contract of every _recv_all implementation."""

    def test_multi_chunk_payload(self, kind: str) -> None:
        payload = b"hello fragmented tcp world"
        recv_all, attach = _bind_impl(kind)
        fake = FakeFragmentedSocket(payload, chunk_size=3)
        attach(fake)

        result = recv_all(len(payload))

        assert type(result) is bytes
        assert result == payload
        assert fake.recv_calls > 1  # actually exercised the fragmenting path

    def test_single_chunk_payload(self, kind: str) -> None:
        payload = b"abc"
        recv_all, attach = _bind_impl(kind)
        attach(FakeFragmentedSocket(payload, chunk_size=1024))

        result = recv_all(len(payload))

        assert type(result) is bytes
        assert result == payload

    def test_zero_size_returns_empty_bytes_without_recv(self, kind: str) -> None:
        recv_all, attach = _bind_impl(kind)
        fake = FakeFragmentedSocket(b"should not be read")
        attach(fake)

        result = recv_all(0)

        assert result == b""
        assert type(result) is bytes
        assert fake.recv_calls == 0

    def test_eof_mid_message_returns_none(self, kind: str) -> None:
        recv_all, attach = _bind_impl(kind)
        fake = FakeFragmentedSocket(b"short", chunk_size=2)
        attach(fake)

        result = recv_all(10)  # request more than the payload holds

        assert result is None
        assert fake.recv_calls == 4  # 2 data chunks + EOF

    def test_immediate_eof_returns_none(self, kind: str) -> None:
        recv_all, attach = _bind_impl(kind)
        attach(FakeFragmentedSocket(b""))

        assert recv_all(4) is None

    def test_timeout_then_data_retries(self, kind: str) -> None:
        payload = b"retry me"
        recv_all, attach = _bind_impl(kind)
        fake = FakeFragmentedSocket(payload, chunk_size=2, timeouts=3)
        attach(fake)

        result = recv_all(len(payload))

        assert result == payload
        assert type(result) is bytes
        assert fake.recv_calls == 3 + 4  # 3 timeouts + 4 data chunks

    def test_timeout_at_eof_still_returns_none(self, kind: str) -> None:
        recv_all, attach = _bind_impl(kind)
        fake = FakeFragmentedSocket(b"x", chunk_size=1, timeouts=2)
        attach(fake)

        assert recv_all(5) is None  # 2 timeouts + 1 byte + EOF

    def test_generic_recv_exception_returns_none(self, kind: str) -> None:
        recv_all, attach = _bind_impl(kind)
        attach(FakeErrorSocket(ConnectionResetError("reset")))

        assert recv_all(8) is None

    def test_missing_socket_returns_none(self, kind: str) -> None:
        recv_all, _ = _bind_impl(kind)  # never attach a fake socket

        assert recv_all(4) is None

    def test_returns_bytes_not_bytearray(self, kind: str) -> None:
        """The bytearray is an internal accumulator only; callers must get bytes."""
        recv_all, attach = _bind_impl(kind)
        attach(FakeFragmentedSocket(b"xyz", chunk_size=1))

        result = recv_all(3)

        assert type(result) is bytes
        assert not isinstance(result, bytearray)

    def test_utf8_payload_decodes_identically(self, kind: str) -> None:
        """Returned bytes keep the decode behavior used by the receive loops."""
        payload = "héllo wörld — ünïcodé".encode("utf-8")
        recv_all, attach = _bind_impl(kind)
        attach(FakeFragmentedSocket(payload, chunk_size=5))

        result = recv_all(len(payload))

        assert type(result) is bytes
        assert result.decode("utf-8") == "héllo wörld — ünïcodé"


@pytest.mark.parametrize("kind", IMPLEMENTATIONS)
def test_large_fragmented_payload(kind: str) -> None:
    """1 MiB in ~17k tiny chunks: many appends, still exact bytes content.

    Deterministic fake socket keeps this fast while exercising the exact
    fragmentation pattern that made bytes += quadratic.
    """
    payload = _PATTERN * 4096  # 1 MiB
    recv_all, attach = _bind_impl(kind)
    fake = FakeFragmentedSocket(payload, chunk_size=61)

    attach(fake)
    result = recv_all(len(payload))

    assert type(result) is bytes
    assert result == payload
    assert fake.recv_calls == -(-len(payload) // 61)  # ceil(1MiB / 61)
