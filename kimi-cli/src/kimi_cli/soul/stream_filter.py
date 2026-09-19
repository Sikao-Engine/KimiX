"""Empty-content-block stream filter (shared, hardened).

Some OpenAI-compatible backends (e.g. scnet/Qwen in thinking mode) interleave
empty ``reasoning_content`` / text / tool-call-argument deltas between real
deltas. An empty block landing mid-stream breaks
:func:`kosong._generate.generate`'s single-``pending_part`` merge chain (an
empty part cannot merge into the pending ``ToolCall``/``TextPart``), so it
force-flushes the pending part early — truncating tool-call arguments or
dropping them entirely (→ ``{}``), and fragmenting text. It also makes the CLI
print a spurious ``[Think]`` banner per empty delta.

This module hosts the filter so **every** LLM call site in the soul can share
it — the main agent step (``kimisoul._step``), context compaction
(``compaction.SimpleCompaction.compact``) and side questions
(``btw.execute_side_question``). ``kimisoul`` re-exports the private names for
backward compatibility; other modules import from here directly to avoid the
``kimisoul → compaction`` import cycle.

Robustness hardening over the original ``kimisoul``-local version:

* ``_EmptyPartFilteredStreamedMessage`` delegates *unknown* attributes to the
  wrapped stream via ``__getattr__`` — provider streams may expose extra
  fields (``finish_reason``, provider-specific metadata) that consumers
  might read; the wrapper must be attribute-transparent like
  ``_EmptyPartFilteredChatProvider``.
* ``_make_empty_part_filtering_callback`` detects async callables beyond bare
  coroutine functions: ``functools.partial`` of a coroutine function and
  callable objects with ``async def __call__``. Misclassification would make
  ``kosong.utils.aio.callback`` skip awaiting the returned coroutine,
  silently dropping wire messages (and emitting RuntimeWarnings).
"""
from __future__ import annotations

import functools
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any


def _is_empty_content_block(part: Any) -> bool:
    """True for a streamed message part that carries **no visible content**.

    Covers *every* empty string-bearing block, so the soul is robust to
    backends that emit empty placeholders between real deltas (not just
    ``ThinkPart``):

    * ``ThinkPart``  — empty ``think`` **and** no ``encrypted`` payload.
    * ``TextPart``   — empty ``text``.
    * ``ToolCallPart`` — empty/``None`` ``arguments_part``.

    ``ToolCall`` headers are *never* treated as empty even when their
    ``arguments`` is ``None``/``""``: that is the standard OpenAI shape for
    the first streamed chunk of a tool call (name present, arguments stream
    in afterwards via ``ToolCallPart``). Dropping such a header would delete
    the whole tool call.

    Encrypted think parts (opaque provider signatures) are never filtered —
    they must pass through.
    """
    from kosong.message import TextPart as _TextPart
    from kosong.message import ThinkPart as _ThinkPart
    from kosong.message import ToolCallPart as _ToolCallPart

    if isinstance(part, _ThinkPart):
        return not part.think and not part.encrypted
    if isinstance(part, _TextPart):
        return part.text == ""
    if isinstance(part, _ToolCallPart):
        return not part.arguments_part
    return False


async def _aiter_without_empty_parts(
    stream: Any,
) -> AsyncIterator[Any]:
    """Yield every streamed part except empty content blocks.

    See :func:`_is_empty_content_block` for what counts as empty and why
    those parts are dropped.
    """
    async for part in stream:
        if _is_empty_content_block(part):
            continue
        yield part


class _EmptyPartFilteredStreamedMessage:
    """Adapter hiding empty content blocks from a provider stream.

    Wraps a kosong ``StreamedMessage`` (duck-typed: ``__aiter__`` + ``id`` /
    ``usage`` properties) so the empty-part filter runs *inside* the async
    iteration — i.e. before :func:`kosong._generate.generate` merges the
    stream. That is what fixes the tool-call argument corruption, which a
    display-path-only filter (``on_message_part``) cannot reach.

    Any *other* attribute access is delegated to the wrapped stream via
    ``__getattr__`` so provider-specific extras (``finish_reason``, custom
    metadata) keep working through the wrapper.
    """

    def __init__(self, original: Any) -> None:
        self._original = original

    def __aiter__(self) -> AsyncIterator[Any]:
        return _aiter_without_empty_parts(self._original)

    @property
    def id(self) -> str | None:
        return self._original.id

    @property
    def usage(self) -> Any:
        return self._original.usage

    def __getattr__(self, name: str) -> Any:
        # Only fires for names not found normally (id/usage/_original resolve
        # without recursion); everything else delegates to the wrapped stream.
        return getattr(object.__getattribute__(self, "_original"), name)


class _EmptyPartFilteredChatProvider:
    """Delegating provider that hides empty content blocks from its stream.

    Forwards every attribute to the wrapped provider (``model_name``,
    ``thinking_effort``, private ``_generation_kwargs`` the retry path
    mutates, ``with_thinking`` / ``with_generation_kwargs`` results, …) via
    :meth:`__getattr__`, overriding only :meth:`generate` to wrap the
    returned stream in :class:`_EmptyPartFilteredStreamedMessage`. The
    provider's *back-pass* serialization (``_convert_message``) is
    unaffected: it keys off the message ``content`` list, which never
    carries the filtered-out empty stream deltas.
    """

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        # __getattr__ only fires for names not found normally, so _inner and
        # generate resolve without recursion; everything else delegates.
        return getattr(object.__getattribute__(self, "_inner"), name)

    async def generate(self, system_prompt: str, tools: Any, history: Any) -> Any:
        inner = object.__getattribute__(self, "_inner")
        stream = await inner.generate(system_prompt, tools, history)
        return _EmptyPartFilteredStreamedMessage(stream)


def _is_async_callable(fn: Any) -> bool:
    """True when *fn* must be awaited: a coroutine function, a
    ``functools.partial`` wrapping one, or an object with an async
    ``__call__``."""
    if inspect.iscoroutinefunction(fn):
        return True
    # functools.partial (possibly nested) wrapping a coroutine function.
    while isinstance(fn, functools.partial):
        fn = fn.func
        if inspect.iscoroutinefunction(fn):
            return True
    # Callable object with ``async def __call__`` (check on the *type*, since
    # inspect.iscoroutinefunction of a bound method works, but a plain
    # instance attribute would not be found).
    call = getattr(fn, "__call__", None)
    if call is not None and inspect.iscoroutinefunction(call):
        return True
    return False


def _make_empty_part_filtering_callback(
    on_message_part: Callable[[Any], Any] | None,
) -> Callable[[Any], Any] | None:
    """Wrap ``on_message_part`` to drop empty content blocks before forwarding.

    The stream-level filter already removes empty parts, so this is a
    belt-and-suspenders guard ensuring the *wire* never sees an empty
    ``[Think]``/text block even if the stream wrapper is bypassed (e.g. a
    future refactor routes a different provider). Preserves sync/async
    callback shape transparently — including ``functools.partial`` of a
    coroutine function and callable objects with ``async def __call__``,
    which ``inspect.iscoroutinefunction`` alone misses.
    """
    if on_message_part is None:
        return None

    def _forward(part: Any) -> Any:
        if _is_empty_content_block(part):
            return None
        result = on_message_part(part)
        return result

    if _is_async_callable(on_message_part):

        async def _afwd(part: Any) -> Any:
            if _is_empty_content_block(part):
                return None
            result = on_message_part(part)
            if inspect.isawaitable(result):
                return await result
            return result

        return _afwd
    return _forward


__all__ = [
    "_EmptyPartFilteredChatProvider",
    "_EmptyPartFilteredStreamedMessage",
    "_aiter_without_empty_parts",
    "_is_async_callable",
    "_is_empty_content_block",
    "_make_empty_part_filtering_callback",
]
