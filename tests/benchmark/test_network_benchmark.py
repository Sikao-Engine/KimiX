"""Performance benchmarks for kimix.network — TCP client/server.

All timings are assert-based so the file doubles as a regression test.

Note: Network benchmarks require localhost TCP and should be skipped in CI
with ``@pytest.mark.skipif``.
"""

from __future__ import annotations

import socket
import time
from typing import Any

import pytest

from kimix.network.tcp_client import TCPClient
from kimix.network.tcp_server import TCPServer

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not hasattr(socket, "AF_INET"),
        reason="Network benchmarks require TCP (not available in this environment)",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# TCP client connect/disconnect benchmarks
# ---------------------------------------------------------------------------


class TestTCPClientConnectBenchmark:
    """Benchmarks for TCP client connect/disconnect."""

    def test_connect_disconnect_cycle(self) -> None:
        """Localhost connect/disconnect cycle."""
        port = _find_free_port()
        server = TCPServer(host="127.0.0.1", port=port)
        server.start(blocking=False)
        time.sleep(0.1)  # Wait for server to start

        client = TCPClient(host="127.0.0.1", port=port)

        start = time.perf_counter()
        for _ in range(200):
            if client.connect():
                time.sleep(0.001)
                client.disconnect()
        elapsed = time.perf_counter() - start
        assert elapsed < 30.0

        # Cleanup
        server.stop()


# ---------------------------------------------------------------------------
# TCP send/recv benchmarks
# ---------------------------------------------------------------------------


class TestTCPSendRecvBenchmark:
    """Benchmarks for TCP send/receive."""

    def test_send_recv_small_messages(self) -> None:
        """Send/receive 100-byte messages."""
        port = _find_free_port()
        received: list[str] = []
        server = TCPServer(host="127.0.0.1", port=port)

        def on_message(msg: str) -> None:
            received.append(msg)

        server.on_message(on_message)
        server.start(blocking=False)
        time.sleep(0.1)

        client = TCPClient(host="127.0.0.1", port=port)
        assert client.connect()
        time.sleep(0.05)

        small_msg = "x" * 100

        start = time.perf_counter()
        for _ in range(1_000):
            client.send(small_msg)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

        client.disconnect()
        server.stop()

    def test_send_recv_large_messages(self) -> None:
        """Send/receive 100KB messages."""
        port = _find_free_port()
        received: list[str] = []
        server = TCPServer(host="127.0.0.1", port=port)

        def on_message(msg: str) -> None:
            received.append(msg)

        server.on_message(on_message)
        server.start(blocking=False)
        time.sleep(0.1)

        client = TCPClient(host="127.0.0.1", port=port)
        assert client.connect()
        time.sleep(0.05)

        large_msg = "x" * 102_400  # ~100KB

        start = time.perf_counter()
        for _ in range(500):
            client.send(large_msg)
        elapsed = time.perf_counter() - start
        assert elapsed < 30.0

        client.disconnect()
        server.stop()

    def test_send_bytes_throughput(self) -> None:
        """Raw byte send throughput."""
        port = _find_free_port()
        received_bytes: list[bytes] = []
        server = TCPServer(host="127.0.0.1", port=port)

        def on_raw(data: bytes) -> str | None:
            received_bytes.append(data)
            return None

        server.on_raw_data(on_raw)
        server.start(blocking=False)
        time.sleep(0.1)

        client = TCPClient(host="127.0.0.1", port=port)
        assert client.connect()
        time.sleep(0.05)

        data = b"x" * 1024

        start = time.perf_counter()
        for _ in range(1_000):
            client.send_bytes(data)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0

        client.disconnect()
        server.stop()
