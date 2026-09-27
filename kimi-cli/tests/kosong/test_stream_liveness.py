"""Regression tests for the stream-stall watchdog producing a false
``APITimeoutError("Stream stalled: no data received for 60s")`` on healthy
streams.

Incident (error log ``20260927_031336_230_session_restart_...``):
``Step 6: APITimeoutError [connection recovery exhausted] — retries
exhausted, restarting session``.

Root cause: the per-part watchdog measured time between *complete parsed
chat chunks*, while the tolerant SSE decoder deliberately swallows keep-alive
traffic (SSE comment lines, empty ``data:`` events) without yielding anything.
A backend that holds the connection alive with keep-alives during a long
thinking/compaction window (observed with Moonshot/Kimi) therefore looks
"stalled" to the watchdog: the healthy stream is killed, every recovery retry
replays the same slow request and is killed again, the retry budget is
burned, and the whole session is restarted.

The watchdog must measure *network liveness* (bytes received), not
parsed-part cadence: keep-alives must re-arm it, while a genuinely silent
wire must still surface as ``APITimeoutError``.

These tests drive a scripted SSE response through the real ``AsyncStream`` +
``KimiStreamedMessage`` pipeline with a shortened watchdog timeout.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import openai
import pytest
from openai.types.chat import ChatCompletionChunk

from kosong.chat_provider import APITimeoutError, StreamedMessagePart
from kosong.contrib.chat_provider.common import BaseStreamedMessage
from kosong.message import TextPart

_DUMMY_REQUEST = httpx.Request("POST", "https://api.test/v1/chat/completions")

_KEEPALIVE_COMMENT = b": keep-alive\n\n"
_KEEPALIVE_EMPTY_DATA = b"data:\n\n"
_DONE = b"data: [DONE]\n\n"


def _sse_chunk(
    *, content: str | None = None, role: str | None = None, finish_reason: str | None = None
) -> str:
    """Build a chat.completion.chunk JSON payload for one SSE event."""
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    import orjson

    return orjson.dumps(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    ).decode()


class _ScriptedResponse(httpx.Response):
    """httpx.Response whose body bytes arrive according to a (delay, payload) script."""

    def __init__(self, script: list[tuple[float, bytes]]) -> None:
        super().__init__(200, request=_DUMMY_REQUEST)
        self._script = list(script)
        self.closed = False

    async def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        for delay, payload in self._script:
            if delay:
                await asyncio.sleep(delay)
            yield payload

    async def aclose(self) -> None:
        self.closed = True


def _build_streamed_message(
    script: list[tuple[float, bytes]],
) -> tuple[Any, _ScriptedResponse]:
    """Build a KimiStreamedMessage backed by a scripted SSE response."""
    from kosong.chat_provider.kimi import KimiStreamedMessage

    client = openai.AsyncOpenAI(api_key="test-key", base_url="https://api.test")
    response = _ScriptedResponse(script)
    stream = openai.AsyncStream(
        cast_to=ChatCompletionChunk,
        response=response,
        client=client,
    )
    return KimiStreamedMessage(stream), response


async def _collect_texts(message: Any) -> list[str]:
    parts = [part async for part in message]
    return [p.text for p in parts if isinstance(p, TextPart)]


class TestKeepAliveLiveness:
    """Keep-alive traffic must keep the stall watchdog re-armed."""

    @pytest.mark.asyncio
    async def test_comment_keepalives_prevent_false_stall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported incident: SSE comment keep-alives every 40ms while the
        model thinks for ~0.72s, then the first real chunk. With a 0.3s
        watchdog this stream used to be killed as 'stalled' even though the
        connection was perfectly healthy."""
        monkeypatch.setenv("KOSONG_STREAM_ITERATION_TIMEOUT", "0.3")
        monkeypatch.delenv("KOSONG_STREAM_IDLE_TIMEOUT", raising=False)
        body = (
            f"data: {_sse_chunk(role='assistant')}\n\ndata: {_sse_chunk(content='Hello')}\n\n"
        ).encode()
        script: list[tuple[float, bytes]] = [(0.04, _KEEPALIVE_COMMENT)] * 18
        script += [(0.0, body), (0.0, _DONE)]
        message, response = _build_streamed_message(script)

        texts = await _collect_texts(message)  # must NOT raise APITimeoutError

        assert texts == ["Hello"]
        assert response.closed, "stream must still be released after normal completion"

    @pytest.mark.asyncio
    async def test_empty_data_keepalives_prevent_false_stall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Moonshot-style empty ``data:`` keep-alive events must count as
        liveness too (they are the documented trigger of the tolerant-SSE
        path and of the original false stall)."""
        monkeypatch.setenv("KOSONG_STREAM_ITERATION_TIMEOUT", "0.3")
        monkeypatch.delenv("KOSONG_STREAM_IDLE_TIMEOUT", raising=False)
        body = f"data: {_sse_chunk(content='Hello')}\n\n".encode()
        script: list[tuple[float, bytes]] = [(0.04, _KEEPALIVE_EMPTY_DATA)] * 18
        script += [(0.0, body), (0.0, _DONE)]
        message, _ = _build_streamed_message(script)

        assert await _collect_texts(message) == ["Hello"]

    @pytest.mark.asyncio
    async def test_true_silence_still_raises_stall_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely silent wire must still surface as APITimeoutError, and
        the give-up path must release the underlying response."""
        monkeypatch.setenv("KOSONG_STREAM_ITERATION_TIMEOUT", "0.3")
        monkeypatch.delenv("KOSONG_STREAM_IDLE_TIMEOUT", raising=False)
        body = f"data: {_sse_chunk(content='late')}\n\n".encode()
        message, response = _build_streamed_message(
            [(0.8, body), (0.0, _DONE)]  # long silence, no keep-alives
        )

        with pytest.raises(APITimeoutError, match="no data received"):
            async for _ in message:
                pass
        assert response.closed, "give-up path must close the underlying response"

    @pytest.mark.asyncio
    async def test_stall_message_reports_measured_silence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When data flowed and then the wire went silent, the error must
        report the actual observed silence (>= the configured timeout), not
        merely restate the configuration."""
        monkeypatch.setenv("KOSONG_STREAM_ITERATION_TIMEOUT", "0.3")
        monkeypatch.delenv("KOSONG_STREAM_IDLE_TIMEOUT", raising=False)
        # Keep-alives at ~0.04/0.08/0.12s, then the wire goes silent; the next
        # event would only arrive at ~0.6s — too late.
        body = f"data: {_sse_chunk(content='x')}\n\n".encode()
        message, _ = _build_streamed_message(
            [(0.04, _KEEPALIVE_COMMENT)] * 3 + [(0.6, body), (0.0, _DONE)]
        )

        with pytest.raises(APITimeoutError) as exc_info:
            async for _ in message:
                pass
        match = re.search(r"no data received for ([0-9.]+)s", str(exc_info.value))
        assert match, f"unexpected error message: {exc_info.value}"
        assert float(match.group(1)) >= 0.3


class TestIdleTimeoutCap:
    """Optional cap for pathological streams that never produce parseable data."""

    @pytest.mark.asyncio
    async def test_idle_timeout_caps_liveness_without_parts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KOSONG_STREAM_ITERATION_TIMEOUT", "0.3")
        monkeypatch.setenv("KOSONG_STREAM_IDLE_TIMEOUT", "0.4")
        script: list[tuple[float, bytes]] = [(0.04, _KEEPALIVE_COMMENT)] * 25
        message, response = _build_streamed_message(script)

        with pytest.raises(APITimeoutError, match="no parseable data"):
            async for _ in message:
                pass
        assert response.closed

    def test_idle_timeout_env_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kosong.contrib.chat_provider.common import get_stream_idle_timeout

        monkeypatch.delenv("KOSONG_STREAM_IDLE_TIMEOUT", raising=False)
        assert get_stream_idle_timeout() == 0.0
        monkeypatch.setenv("KOSONG_STREAM_IDLE_TIMEOUT", "5")
        assert get_stream_idle_timeout() == 5.0
        monkeypatch.setenv("KOSONG_STREAM_IDLE_TIMEOUT", "-1")
        assert get_stream_idle_timeout() == 0.0
        monkeypatch.setenv("KOSONG_STREAM_IDLE_TIMEOUT", "junk")
        assert get_stream_idle_timeout() == 0.0


class TestWatchdogRearmInternals:
    """Direct tests for the liveness-aware timeout iterator."""

    @pytest.mark.asyncio
    async def test_rearms_while_watch_is_touched(self) -> None:
        from kosong.contrib.chat_provider.common import StallWatch, with_stream_timeout

        watch = StallWatch()

        async def slow_source() -> AsyncIterator[int]:
            yield 1
            for _ in range(5):
                await asyncio.sleep(0.1)
                watch.touch()
            yield 2

        wrapped = with_stream_timeout(slow_source(), timeout=0.35, watch=watch)
        assert [item async for item in wrapped] == [1, 2]

    @pytest.mark.asyncio
    async def test_raises_when_watch_reports_silence(self) -> None:
        from kosong.contrib.chat_provider.common import StallWatch, with_stream_timeout

        async def stalled_source() -> AsyncIterator[int]:
            yield 1
            await asyncio.sleep(3600)

        wrapped = with_stream_timeout(stalled_source(), timeout=0.05, watch=StallWatch())
        items: list[int] = []
        with pytest.raises(APITimeoutError, match="no data received"):
            async for item in wrapped:
                items.append(item)
        assert items == [1]

    @pytest.mark.asyncio
    async def test_watch_touched_on_every_byte_chunk(self) -> None:
        from kosong.chat_provider.openai_common import _iter_sse_events
        from kosong.contrib.chat_provider.common import StallWatch

        watch = StallWatch()
        response = _ScriptedResponse(
            [(0.0, b"data: a\n\n"), (0.0, b": ping\n\n"), (0.0, b"data: b\n\n")]
        )
        events = [event async for event in _iter_sse_events(response, watch=watch)]

        assert len(events) == 2
        assert watch.got_data
        assert watch.touch_count == 3
        assert watch.seconds_since_data() < 5.0


class TestBaseStreamedMessageWatch:
    """BaseStreamedMessage must pass a subclass-provided watch to the watchdog."""

    @pytest.mark.asyncio
    async def test_watch_passthrough_keeps_slow_parts_flowing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kosong.contrib.chat_provider.common import StallWatch

        monkeypatch.setenv("KOSONG_STREAM_ITERATION_TIMEOUT", "0.3")
        monkeypatch.delenv("KOSONG_STREAM_IDLE_TIMEOUT", raising=False)
        watch = StallWatch()

        class _WatchedStreamedMessage(BaseStreamedMessage):
            def __init__(self, iterator: AsyncIterator[StreamedMessagePart]) -> None:
                self._watch = watch
                self._iter = iterator

        async def slow_with_touches() -> AsyncIterator[StreamedMessagePart]:
            for i in range(3):
                await asyncio.sleep(0.15)
                watch.touch()  # keep-alive arrives 0.15s into each 0.4s gap
                await asyncio.sleep(0.25)
                yield TextPart(text=str(i))

        msg = _WatchedStreamedMessage(slow_with_touches())
        parts = [part async for part in msg]
        assert [p.text for p in parts] == ["0", "1", "2"]

    @pytest.mark.asyncio
    async def test_no_watch_keeps_legacy_behavior(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KOSONG_STREAM_ITERATION_TIMEOUT", "0.05")

        class _PlainStreamedMessage(BaseStreamedMessage):
            def __init__(self, iterator: AsyncIterator[StreamedMessagePart]) -> None:
                self._iter = iterator

        async def stalled() -> AsyncIterator[StreamedMessagePart]:
            yield TextPart(text="hello")
            await asyncio.sleep(3600)

        msg = _PlainStreamedMessage(stalled())
        parts: list[StreamedMessagePart] = []
        with pytest.raises(APITimeoutError):
            async for part in msg:
                parts.append(part)
        assert parts == [TextPart(text="hello")]
