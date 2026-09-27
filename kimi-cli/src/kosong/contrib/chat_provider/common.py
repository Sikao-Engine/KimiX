from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Literal, TypeVar

import regex as re

from kosong.chat_provider import APITimeoutError, StreamedMessagePart, TokenUsage
from kosong.message import Message

if TYPE_CHECKING:
    from kosong.message import ToolCall

type ToolMessageConversion = Literal["extract_text"]


def validate_tool_call_arguments(tool_calls: list[ToolCall]) -> list[str]:
    """Validate and sanitize tool call arguments. Returns error messages.

    Each tool call's arguments are strictly parsed as JSON — ``loads_relaxed``
    is intentionally NOT used here because ``json_repair`` accepts inputs like
    ``'{}{}'`` (duplicated gateway chunks) that strict backends reject with a
    400 when echoed back in history.  Invalid JSON is first repaired to the
    first complete JSON value, otherwise reset to ``"{}"``; non-object values
    are reset to ``"{}"``.  All repairs are reported via the returned error
    list so the model can see what happened.
    """
    import orjson

    from kosong.utils.jsonx import sanitize_tool_arguments

    errors: list[str] = []
    for tc in tool_calls:
        original = tc.function.arguments
        if not original:
            # Missing/empty arguments: silently fill so the backend always
            # receives a parseable JSON object.  Some backends reject an
            # absent ``arguments`` key outright.
            tc.function.arguments = "{}"
            continue
        sanitized = sanitize_tool_arguments(original)
        if sanitized != original:
            tc.function.arguments = sanitized
            errors.append(
                f"Error: Tool call '{tc.function.name}' had invalid JSON arguments; "
                f"reset to '{sanitized}'."
            )
        try:
            parsed = orjson.loads(tc.function.arguments)
        except orjson.JSONDecodeError:  # pragma: no cover - sanitize guarantees validity
            continue
        if not isinstance(parsed, dict):
            errors.append(
                f"Error: Tool call '{tc.function.name}' arguments must be a JSON object, "
                f"got {type(parsed).__name__}."
            )
            tc.function.arguments = "{}"
    return errors


def check_tool_call_id(tool_call_id: str | None, message_content: str) -> str | None:
    """Return an error message if *tool_call_id* is missing, otherwise ``None``.

    This is a shared helper for the ``tool_call_id is None`` guard used across
    multiple providers.
    """
    if tool_call_id is None:
        return f"Error: Tool message is missing `tool_call_id`. Content: {message_content}"
    return None


_EMPTY_TOOL_CALL_ID = "tool_call"
_TOOL_CALL_ID_SAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
_TOOL_CALL_ID_MAX_LENGTH = 64


def _sanitize_tool_call_id(tool_call_id: str) -> str:
    """Replace characters strict backends reject and truncate to the id budget."""
    sanitized = _TOOL_CALL_ID_SAFE_CHARS.sub("_", tool_call_id)
    return sanitized[:_TOOL_CALL_ID_MAX_LENGTH]


def _make_unique_tool_call_id(normalized: str, used: set[str]) -> str:
    base = normalized if normalized else _EMPTY_TOOL_CALL_ID
    candidate = base[:_TOOL_CALL_ID_MAX_LENGTH]
    if candidate not in used:
        return candidate
    index = 2
    while True:
        suffix = f"_{index}"
        candidate = base[: _TOOL_CALL_ID_MAX_LENGTH - len(suffix)] + suffix
        if candidate not in used:
            return candidate
        index += 1


def normalize_tool_call_ids(history: Sequence[Message]) -> Sequence[Message]:
    """Rewrite invalid historical tool-call ids to a safe, portable shape.

    Histories persisted from other providers (or older sessions) can contain
    tool-call ids with characters strict backends reject (e.g. Moonshot 400s
    on ``Read:9``) or ids longer than 64 characters; sending them verbatim
    fails the whole request. Ids are sanitized to ``[a-zA-Z0-9_-]``,
    truncated, and made unique with ``_2``/``_3``... suffixes; assistant
    ``tool_calls`` entries and their matching ``tool`` messages are rewritten
    consistently.

    Providers whose own backends generate well-formed ids (OpenAI, Anthropic)
    apply the same normalization defensively so cross-provider histories can
    be replayed anywhere. Input messages are never mutated; when every id is
    already valid the original sequence object is returned unchanged.
    """
    raw_ids: list[str] = []
    seen: set[str] = set()
    for message in history:
        for tool_call in message.tool_calls or []:
            if tool_call.id not in seen:
                seen.add(tool_call.id)
                raw_ids.append(tool_call.id)
        if message.tool_call_id is not None and message.tool_call_id not in seen:
            seen.add(message.tool_call_id)
            raw_ids.append(message.tool_call_id)
    if not raw_ids:
        return history

    # Ids that already satisfy the contract keep their value (first pass), so
    # only genuinely invalid ids are rewritten (second pass).
    mapped: dict[str, str] = {}
    used: set[str] = set()
    for raw_id in raw_ids:
        normalized = _sanitize_tool_call_id(raw_id)
        if normalized == raw_id and normalized:
            mapped[raw_id] = normalized
            used.add(normalized)
    for raw_id in raw_ids:
        if raw_id in mapped:
            continue
        unique = _make_unique_tool_call_id(_sanitize_tool_call_id(raw_id), used)
        mapped[raw_id] = unique
        used.add(unique)

    if all(mapped[raw_id] == raw_id for raw_id in raw_ids):
        return history

    normalized_messages: list[Message] = []
    for message in history:
        changed = False
        new_tool_calls = message.tool_calls
        if message.tool_calls:
            new_tool_calls = []
            for tool_call in message.tool_calls:
                mapped_id = mapped[tool_call.id]
                if mapped_id == tool_call.id:
                    new_tool_calls.append(tool_call)
                else:
                    changed = True
                    new_tool_calls.append(tool_call.model_copy(update={"id": mapped_id}))
        new_tool_call_id = (
            mapped[message.tool_call_id]
            if message.tool_call_id is not None
            else message.tool_call_id
        )
        if new_tool_call_id != message.tool_call_id:
            changed = True
        if not changed:
            normalized_messages.append(message)
        else:
            normalized_messages.append(
                message.model_copy(
                    update={"tool_calls": new_tool_calls, "tool_call_id": new_tool_call_id}
                )
            )
    return normalized_messages


T = TypeVar("T")

_STREAM_ITERATION_TIMEOUT_DEFAULT = 60.0
_STREAM_ITERATION_TIMEOUT_ENV = "KOSONG_STREAM_ITERATION_TIMEOUT"

_STREAM_IDLE_TIMEOUT_ENV = "KOSONG_STREAM_IDLE_TIMEOUT"


def get_stream_iteration_timeout() -> float:
    """Return the per-chunk timeout for chat provider streaming iterators.

    Reads ``KOSONG_STREAM_ITERATION_TIMEOUT`` from the environment; falls back
    to a 60-second default. Unparseable values are ignored.
    """
    raw = os.environ.get(_STREAM_ITERATION_TIMEOUT_ENV)
    if raw is None:
        return _STREAM_ITERATION_TIMEOUT_DEFAULT
    try:
        return float(raw)
    except ValueError:
        return _STREAM_ITERATION_TIMEOUT_DEFAULT


def get_stream_idle_timeout() -> float:
    """Return the cap on time without a parseable chunk while data still flows.

    Reads ``KOSONG_STREAM_IDLE_TIMEOUT`` from the environment. ``0`` (the
    default) disables the cap, giving the stall watchdog pure liveness
    semantics: a stream is only killed when the network is truly silent.
    Unparseable or negative values are treated as disabled.
    """
    raw = os.environ.get(_STREAM_IDLE_TIMEOUT_ENV)
    if raw is None:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if value > 0 else 0.0


class StallWatch:
    """Tracks when the network last delivered data for a streaming response.

    The stream-stall watchdog treats a stream as healthy while *any* bytes
    are arriving — SSE comment keep-alives, empty ``data:`` events and
    partial decodes all count. The transport/decode loop calls :meth:`touch`
    on every read so the watchdog can distinguish a dead connection from a
    slow-but-alive backend (e.g. backends that hold the stream open with
    keep-alive traffic during long reasoning windows).
    """

    __slots__ = ("_last_data_ts", "touch_count")

    def __init__(self) -> None:
        self._last_data_ts: float = time.monotonic()
        self.touch_count: int = 0

    def touch(self) -> None:
        """Record that data just arrived from the network."""
        self._last_data_ts = time.monotonic()
        self.touch_count += 1

    @property
    def got_data(self) -> bool:
        """Whether any data has arrived since the watch was created."""
        return self.touch_count > 0

    def seconds_since_data(self) -> float:
        """Seconds elapsed since the network last delivered data."""
        return time.monotonic() - self._last_data_ts


class _StreamTimeoutIterator(AsyncIterator[T]):
    """Wrap an async iterator so a stalled stream surfaces as APITimeoutError.

    Each next item is awaited with a per-attempt timeout. When a *watch* is
    supplied, a per-attempt timeout does not immediately abort the stream: as
    long as the network is still delivering data (SSE keep-alive comments,
    empty ``data:`` events — anything at all), the wait is re-armed instead.
    Only a wire that has been silent for at least *timeout* seconds — or a
    stream without a watch — raises :class:`APITimeoutError`.

    Re-arming must not cancel the wrapped iterator's in-flight ``__anext__``:
    cancelling an async generator's ``__anext__`` terminates the generator for
    good (the next ``__anext__`` would raise ``StopAsyncIteration``). The
    in-flight call is therefore held in a task that survives timeouts (via
    :func:`asyncio.shield`) and is cancelled only when the watchdog gives up
    or the consuming task itself is cancelled.
    """

    __slots__ = (
        "_idle_timeout",
        "_iterator",
        "_last_item_ts",
        "_pending",
        "_timeout",
        "_watch",
    )

    def __init__(
        self,
        iterator: AsyncIterator[T],
        timeout: float,
        *,
        watch: StallWatch | None = None,
        idle_timeout: float = 0.0,
    ) -> None:
        self._iterator = iterator
        self._timeout = timeout
        self._watch = watch
        self._idle_timeout = idle_timeout if idle_timeout and idle_timeout > 0 else 0.0
        self._pending: asyncio.Future[T] | None = None
        self._last_item_ts: float = time.monotonic()

    def __aiter__(self) -> AsyncIterator[T]:
        return self

    async def __anext__(self) -> T:
        while True:
            if self._pending is None:
                self._pending = asyncio.ensure_future(self._iterator.__anext__())
            try:
                item = await asyncio.wait_for(
                    asyncio.shield(self._pending), timeout=self._timeout
                )
            except TimeoutError:
                now = time.monotonic()
                if (
                    self._watch is not None
                    and self._watch.seconds_since_data() < self._timeout
                ):
                    # The wire is alive (keep-alives arriving): the lag is in
                    # producing the next *complete* chunk, not in the network.
                    # Re-arm instead of killing a healthy stream.
                    if (
                        self._idle_timeout
                        and now - self._last_item_ts >= self._idle_timeout
                    ):
                        await self._give_up()
                        raise APITimeoutError(
                            "Stream idle: no parseable data for "
                            f"{now - self._last_item_ts:.3g}s "
                            "(connection still alive)"
                        ) from None
                    continue
                await self._give_up()
                silence = (
                    self._watch.seconds_since_data() if self._watch else self._timeout
                )
                raise APITimeoutError(
                    f"Stream stalled: no data received for {silence:.3g}s"
                ) from None
            except asyncio.CancelledError:
                # This task was cancelled from outside (user interrupt,
                # shutdown): unwind the wrapped generator exactly like the
                # pre-watch implementation did on timeout.
                if self._pending is not None and not self._pending.done():
                    await self._give_up()
                else:
                    self._pending = None
                raise
            except BaseException:
                # The wrapped iterator itself failed (or is exhausted); its
                # exception/result was just consumed via the propagation.
                self._pending = None
                raise
            self._pending = None
            self._last_item_ts = time.monotonic()
            return item

    async def _give_up(self) -> None:
        """Cancel the in-flight ``__anext__`` and wait for the wrapped
        generator to unwind, so its cleanup (e.g. closing the HTTP response)
        completes before the failure is surfaced to the caller — matching the
        pre-watch ``asyncio.wait_for`` behavior."""
        pending, self._pending = self._pending, None
        if pending is None or pending.done():
            return
        pending.cancel()
        try:
            await pending
        except BaseException:
            # CancelledError from the cancelled task, or an exception raised
            # by the generator's cleanup — both are expected here.
            pass


def with_stream_timeout[T](
    iterator: AsyncIterator[T],
    timeout: float | None = None,
    *,
    watch: StallWatch | None = None,
    idle_timeout: float | None = None,
) -> AsyncIterator[T]:
    """Wrap *iterator* with a stall watchdog.

    Args:
        iterator: The async iterator to wrap.
        timeout: Stall timeout in seconds. If ``None``, uses
            :func:`get_stream_iteration_timeout`.
        watch: Optional liveness signal updated by the transport layer on
            every received byte. When provided, a timeout only aborts the
            stream if the network has also been silent for *timeout* seconds;
            healthy-but-slow streams (keep-alive traffic without complete
            chunks) are re-armed instead of killed.
        idle_timeout: Optional cap on the time without a parseable item even
            while the connection is alive. ``0``/``None`` disables the cap
            (see :func:`get_stream_idle_timeout`).
    """
    if timeout is None:
        timeout = get_stream_iteration_timeout()
    if idle_timeout is None:
        idle_timeout = get_stream_idle_timeout()
    return _StreamTimeoutIterator(
        iterator, timeout=timeout, watch=watch, idle_timeout=idle_timeout
    )


class BaseStreamedMessage:
    """Mixin / base class for provider-specific streamed messages.

    Provides the common ``__aiter__`` / ``__anext__`` / ``id`` boilerplate.
    Subclasses must set ``self._iter`` in ``__init__``.

    The underlying iterator is wrapped with a stall watchdog so that a stalled
    network stream (e.g. the remote server stops sending SSE chunks) is
    surfaced as :class:`~kosong.chat_provider.APITimeoutError` instead of
    hanging the caller forever. Subclasses may set ``self._watch`` to a
    :class:`StallWatch` fed by their transport layer; when present, the
    watchdog re-arms while any data (keep-alives included) arrives and only
    kills the stream when the wire is truly silent.
    """

    _iter: AsyncIterator[StreamedMessagePart]
    _id: str | None = None
    _timeout_iter: AsyncIterator[StreamedMessagePart] | None = None
    _watch: StallWatch | None = None

    def __aiter__(self) -> AsyncIterator[StreamedMessagePart]:
        if self._timeout_iter is None:
            self._timeout_iter = with_stream_timeout(
                self._iter,
                timeout=get_stream_iteration_timeout(),
                watch=self._watch,
                idle_timeout=get_stream_idle_timeout(),
            )
        return self._timeout_iter

    async def __anext__(self) -> StreamedMessagePart:
        return await self.__aiter__().__anext__()

    @property
    def id(self) -> str | None:
        return self._id

    @staticmethod
    def _build_token_usage(
        *,
        input_other: int,
        output: int,
        input_cache_read: int = 0,
        input_cache_creation: int = 0,
    ) -> TokenUsage:
        """Canonical factory for ``TokenUsage``.

        Subclass ``usage`` properties call this with provider-specific
        extraction logic so that edge-case handling (None-vs-0, negation,
        etc.) stays consistent across providers.
        """
        return TokenUsage(
            input_other=input_other,
            output=output,
            input_cache_read=input_cache_read,
            input_cache_creation=input_cache_creation,
        )
