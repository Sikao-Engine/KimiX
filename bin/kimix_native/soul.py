"""soul — arena-backed conversation state for the kimi-agent call sites.

Report candidates A1 (history store), A2 (prune elision blobs), B1 (provider
request builder), B3 (compaction decisions on the arena) and B4 (export
markdown on the packed bridge).

Every entry point takes a *message list* (``list[dict]`` with the kosong shape)
or a :class:`History` handle, packs it once into the KMB2 boundary form
(``python/kimix_native/_structure.py``), and calls the native kernel; the
pure-Python mirrors live in the same module as ``_compat_*`` and are used when
``KIMIX_NATIVE_SOUL=0`` (or when the compiled module is unavailable).

Why the packing lives here: the report's §2 rule is that a native entry point
takes BYTES + SPAN TABLES, never ``list[dict]``. One ``struct.pack_into`` per
record is ~60 KB for a 750-message history against ~1.2 MB of tuples/lists for
the legacy dict bridge, and it is what makes the arena kernels pay off.

Gates: ``KIMIX_NATIVE`` globally, ``KIMIX_NATIVE_SOUL`` for this module and
``KIMIX_NATIVE_REQUEST_<PROVIDER>`` per provider for :func:`build_request`.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

import orjson

from . import _native, _structure, use_native
from ._common import _dec, _enc

# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def native_active() -> bool:
    """True when the arena kernels are usable right now."""
    return _native is not None and use_native("SOUL")


def _request_gate(provider: str) -> bool:
    """Per-provider request-builder gate (``KIMIX_NATIVE_REQUEST_<P>``)."""
    import os

    if not native_active():
        return False
    key = f"KIMIX_NATIVE_REQUEST_{provider.upper()}"
    flag = os.environ.get(key, "1")
    return str(flag).lower() not in ("0", "false", "no", "")


def _pack(history) -> tuple[bytes, bytes]:
    """``(structure, history_bytes)`` for a message list or a History handle."""
    if isinstance(history, History):
        return history.packed()
    if isinstance(history, _CompatHistory):
        history = list(history)
    hist_bytes, structure = _structure.build(list(history))
    return structure, hist_bytes


def _messages_of(history) -> list[dict]:
    if isinstance(history, (History, _CompatHistory)):
        return history.to_messages()
    return list(history)


# ---------------------------------------------------------------------------
# History (A1)
# ---------------------------------------------------------------------------


class History(Sequence):
    """Native conversation-history arena (report A1).

    A ``Sequence`` of canonical message dicts whose bytes live in the native
    store: iteration materialises dicts in batches of ``BATCH`` and drops them
    immediately, so a 750-message history no longer keeps 3.5 MB of pydantic
    objects alive.

    Indices are live ordinals (Python list semantics). A mutation invalidates
    any span the previous call returned, which is why nothing here hands out a
    view: every accessor returns fresh dicts.
    """

    BATCH = 64
    __slots__ = ("_h", "_closed")

    def __init__(self, messages: Iterable[dict] | None = None, *, reserve_bytes: int = 0):
        self._h = _native.soul.History(reserve_bytes) if native_active() else None
        self._closed = False
        if messages:
            self.extend(messages)

    # -- writes ---------------------------------------------------------
    def append(self, message: dict) -> None:
        if self._h is None:
            raise RuntimeError("native soul.History is unavailable")
        structure, history = _pack([message])
        self._h.append(structure, history)

    def extend(self, messages: Iterable[dict]) -> None:
        if self._h is None:
            raise RuntimeError("native soul.History is unavailable")
        batch = list(messages)
        if not batch:
            return
        structure, history = _pack(batch)
        self._h.append(structure, history)

    def replace(self, index: int, message: dict) -> bool:
        if self._h is None:
            raise RuntimeError("native soul.History is unavailable")
        structure, history = _pack([message])
        return bool(self._h.replace(index, structure, history))

    def drop(self, index: int) -> bool:
        return bool(self._h.drop(index)) if self._h is not None else False

    def truncate(self, count: int) -> None:
        if self._h is not None:
            self._h.truncate(count)

    def clear(self) -> None:
        if self._h is not None:
            self._h.clear()

    def compact(self) -> int:
        return int(self._h.compact()) if self._h is not None else 0

    # -- reads ----------------------------------------------------------
    def __len__(self) -> int:
        return 0 if self._h is None else len(self._h)

    def pack_range(self, first: int = 0, count: int = -1) -> tuple[bytes, bytes]:
        """``(structure, history_bytes)`` for ``count`` messages from ``first``."""
        if self._h is None:
            return _pack([])
        structure, history = self._h.pack(first, count)
        return structure, history

    def packed(self) -> tuple[bytes, bytes]:
        return self.pack_range(0, -1)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                return [self[i] for i in range(start, stop, step)]
            return self.slice(start, stop)
        size = len(self)
        if index < 0:
            index += size
        if index < 0 or index >= size:
            raise IndexError("history index out of range")
        structure, history = self.pack_range(index, 1)
        messages = _structure.to_messages(structure, history)
        if not messages:
            raise IndexError("history index out of range")
        return messages[0]

    def slice(self, start: int, stop: int) -> list[dict]:
        size = len(self)
        start = max(0, min(int(start), size))
        stop = max(start, min(int(stop), size))
        if start == stop:
            return []
        structure, history = self.pack_range(start, stop - start)
        return _structure.to_messages(structure, history)

    def __iter__(self) -> Iterator[dict]:
        size = len(self)
        for first in range(0, size, self.BATCH):
            structure, history = self.pack_range(first, self.BATCH)
            yield from _structure.to_messages(structure, history)

    def to_messages(self) -> list[dict]:
        """Full materialisation (explicit; the batching path is ``__iter__``)."""
        structure, history = self.packed()
        return _structure.to_messages(structure, history)

    def text_at(self, index: int) -> str:
        if self._h is None:
            return ""
        return _dec(self._h.text_at(index))

    def generation(self) -> int:
        return int(self._h.generation()) if self._h is not None else 0

    def bytes_owned(self) -> int:
        return int(self._h.bytes_owned()) if self._h is not None else 0

    def bytes_live(self) -> int:
        return int(self._h.bytes_live()) if self._h is not None else 0

    # -- persistence ----------------------------------------------------
    def save(self) -> bytes:
        if self._h is None:
            return b""
        return bytes(self._h.save())

    def load(self, blob: bytes) -> bool:
        if self._h is None:
            return False
        return bool(self._h.load(blob))

    # -- lifetime -------------------------------------------------------
    def close(self) -> None:
        if self._h is not None and not self._closed:
            self._h.close()
            self._closed = True

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def __del__(self):
        # Warn only: never free during interpreter shutdown (the module may
        # already be torn down, and a raise here would be swallowed anyway).
        try:
            if self._h is not None and not self._closed:
                self._h.close()
                self._closed = True
        except Exception:  # pragma: no cover - shutdown path
            pass


class _CompatHistory(Sequence):
    """Pure-Python mirror of :class:`History` (bit-identical message dicts)."""

    BATCH = 64  # same batch size the native iterator uses
    __slots__ = ("_items",)

    def __init__(self, messages: Iterable[dict] | None = None, *, reserve_bytes: int = 0):
        del reserve_bytes
        self._items: list[dict] = [
            _structure.normalize_message(m) for m in (messages or [])
        ]

    def append(self, message: dict) -> None:
        self._items.append(_structure.normalize_message(message))

    def extend(self, messages: Iterable[dict]) -> None:
        for message in messages:
            self.append(message)

    def replace(self, index: int, message: dict) -> bool:
        if index < 0 or index >= len(self._items):
            return False
        self._items[index] = _structure.normalize_message(message)
        return True

    def drop(self, index: int) -> bool:
        if index < 0 or index >= len(self._items):
            return False
        del self._items[index]
        return True

    def truncate(self, count: int) -> None:
        del self._items[max(0, count):]

    def clear(self) -> None:
        self._items.clear()

    def compact(self) -> int:
        return 0

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [dict(m) for m in self._items[index]]
        return self._items[index]

    def __iter__(self):
        return iter(self._items)

    def slice(self, start: int, stop: int) -> list[dict]:
        return [dict(m) for m in self._items[start:stop]]

    def to_messages(self) -> list[dict]:
        return [dict(m) for m in self._items]

    def pack_range(self, first: int = 0, count: int = -1) -> tuple[bytes, bytes]:
        items = self._items if count < 0 else self._items[first:first + count]
        if count < 0:
            items = self._items[first:]
        hist_bytes, structure = _structure.build(items)
        return structure, hist_bytes

    def packed(self) -> tuple[bytes, bytes]:
        return self.pack_range(0, -1)

    def text_at(self, index: int) -> str:
        if index < 0 or index >= len(self._items):
            return ""
        return "\n".join(
            part["text"]
            for part in self._items[index]["content"]
            if part.get("type") == "text"
        )

    def generation(self) -> int:
        return 0

    def bytes_owned(self) -> int:
        return 0

    def bytes_live(self) -> int:
        return 0

    def save(self) -> bytes:
        return orjson.dumps(self._items)

    def load(self, blob: bytes) -> bool:
        try:
            self._items = [
                _structure.normalize_message(m) for m in orjson.loads(blob)
            ]
        except Exception:
            return False
        return True

    def close(self) -> None:
        self._items.clear()

    def __enter__(self) -> "_CompatHistory":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


def new_history(messages: Iterable[dict] | None = None, **kwargs) -> Sequence:
    """The gated factory: native arena when available, Python mirror otherwise."""
    if native_active():
        return History(messages, **kwargs)
    return _CompatHistory(messages, **kwargs)


# ---------------------------------------------------------------------------
# Elisions (A2)
# ---------------------------------------------------------------------------


class Elisions:
    """Native content-addressed blob store for pruned message text (A2)."""

    __slots__ = ("_e", "_closed", "_compat")

    def __init__(self):
        self._e = _native.soul.Elisions() if native_active() else None
        self._closed = False
        self._compat = _CompatElisions()

    def put(self, text: str | bytes) -> int:
        data = text if isinstance(text, bytes) else _enc(text)
        if self._e is not None:
            return int(self._e.put(data))
        return self._compat.put(data)

    def get(self, ref: int) -> bytes | None:
        if self._e is not None:
            return self._e.get(ref)
        return self._compat.get(ref)

    def text(self, ref: int) -> str | None:
        data = self.get(ref)
        return None if data is None else _dec(data)

    def has(self, ref: int) -> bool:
        return bool(self._e.has(ref)) if self._e is not None else self._compat.has(ref)

    def length(self, ref: int) -> int:
        return int(self._e.length(ref)) if self._e is not None else self._compat.length(ref)

    def count(self) -> int:
        return int(self._e.count()) if self._e is not None else self._compat.count()

    def dedup_hits(self) -> int:
        return (
            int(self._e.dedup_hits()) if self._e is not None
            else self._compat.dedup_hits()
        )

    def bytes_owned(self) -> int:
        return (
            int(self._e.bytes_owned()) if self._e is not None
            else self._compat.bytes_owned()
        )

    def stats(self) -> tuple[int, int, int]:
        if self._e is not None:
            return tuple(self._e.stats())  # type: ignore[return-value]
        return self._compat.stats()

    def save(self) -> bytes:
        return bytes(self._e.save()) if self._e is not None else self._compat.save()

    def load(self, blob: bytes) -> bool:
        if self._e is not None:
            return bool(self._e.load(blob))
        return self._compat.load(blob)

    def clear(self) -> None:
        if self._e is not None:
            self._e.clear()
        self._compat.clear()

    def close(self) -> None:
        if self._e is not None and not self._closed:
            self._e.close()
            self._closed = True

    def __enter__(self) -> "Elisions":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


class _CompatElisions:
    """Pure-Python mirror: dict of bytes keyed by a content hash."""

    __slots__ = ("_blobs", "_hits")

    def __init__(self):
        self._blobs: dict[int, bytes] = {}
        self._hits = 0

    _SEED = (1 << 61) - 1  # kimix::hash64_default_seed

    @staticmethod
    def _key(data: bytes) -> int:
        """The reference content key: XXH3-64 with kimix's default seed.

        Matches `kimix::hash64` exactly when ``xxhash`` is installed (the same
        package the other parity tests use); the blake2b fallback keeps the
        mirror deterministic without it, and refs are opaque handles either way.
        """
        try:
            import xxhash

            return xxhash.xxh3_64(data, seed=_CompatElisions._SEED).intdigest()
        except Exception:
            import hashlib

            return int.from_bytes(
                hashlib.blake2b(data, digest_size=8).digest(), "little"
            )

    def put(self, data: bytes | str) -> int:
        data = data if isinstance(data, bytes) else _enc(data)
        ref = self._key(data)
        while ref in self._blobs and self._blobs[ref] != data:
            ref = (ref + 1) & 0xFFFFFFFFFFFFFFFF
        if ref in self._blobs:
            self._hits += 1
        else:
            self._blobs[ref] = data
        return ref

    def get(self, ref: int) -> bytes | None:
        return self._blobs.get(ref)

    def has(self, ref: int) -> bool:
        return ref in self._blobs

    def length(self, ref: int) -> int:
        blob = self._blobs.get(ref)
        return 0 if blob is None else len(blob)

    def count(self) -> int:
        return len(self._blobs)

    def dedup_hits(self) -> int:
        return self._hits

    def bytes_owned(self) -> int:
        return sum(len(b) for b in self._blobs.values())

    def stats(self) -> tuple[int, int, int]:
        return (self.count(), self.dedup_hits(), self.bytes_owned())

    def save(self) -> bytes:
        payload = [
            {"ref": ref, "data": _dec(blob)} for ref, blob in self._blobs.items()
        ]
        return orjson.dumps(payload)

    def load(self, blob: bytes) -> bool:
        try:
            items = orjson.loads(blob)
        except Exception:
            return False
        for item in items:
            self._blobs[int(item["ref"])] = _enc(item["data"])
        return True

    def clear(self) -> None:
        self._blobs.clear()
        self._hits = 0


# ---------------------------------------------------------------------------
# pure decision kernels: native primary, Python mirror as _compat
# ---------------------------------------------------------------------------


def _text_of(message: dict, sep: str = "\n") -> str:
    return sep.join(
        part.get("text") or ""
        for part in message.get("content") or []
        if _structure.part_kind(part) == 0
    )


def _is_system_reminder(message: dict) -> bool:
    content = message.get("content") or []
    if message.get("role") != "user" or len(content) != 1:
        return False
    part = content[0]
    return _structure.part_kind(part) == 0 and (
        (part.get("text") or "").lstrip().startswith("<system-reminder>")
    )


def _is_notification(message: dict) -> bool:
    content = message.get("content") or []
    if message.get("role") != "user" or len(content) != 1:
        return False
    part = content[0]
    return _structure.part_kind(part) == 0 and (
        (part.get("text") or "").lstrip().startswith("<notification ")
    )


def _compat_normalize_history(history) -> list[dict]:
    """kimi_cli/soul/message.py::normalize_history (adjacent user merge)."""
    result: list[dict] = []
    for message in _messages_of(history):
        if (
            result
            and result[-1].get("role") == message.get("role") == "user"
            and not _is_notification(result[-1])
            and not _is_notification(message)
            and not _is_system_reminder(result[-1])
            and not _is_system_reminder(message)
        ):
            merged = _structure.normalize_message(result[-1])
            merged["content"] = list(merged["content"]) + list(
                _structure.normalize_message(message)["content"]
            )
            result[-1] = merged
        else:
            result.append(_structure.normalize_message(message))
    return result


def normalize_history(history) -> list[dict]:
    """Merge adjacent user messages (notification/reminder messages exempt)."""
    if native_active():
        structure, hist_bytes = _pack(history)
        out_structure, out_history = _native.soul.normalize_history(structure, hist_bytes)
        return _structure.to_messages(out_structure, out_history)
    return _compat_normalize_history(history)


def _compat_repair_tool_pairing(history) -> list[dict]:
    """kosong/history.py::repair_tool_message_pairing."""
    messages = [_structure.normalize_message(m) for m in _messages_of(history)]
    tools_by_id: dict[str, list[int]] = {}
    for index, message in enumerate(messages):
        if message.get("role") == "tool" and message.get("tool_call_id") is not None:
            tools_by_id.setdefault(message["tool_call_id"], []).append(index)
    if not tools_by_id and not any(m.get("tool_calls") for m in messages):
        return messages

    claimed: set[int] = set()
    output: list[dict] = []
    for message in messages:
        if message.get("role") == "tool":
            continue
        output.append(message)
        if not message.get("tool_calls"):
            continue
        for call in message["tool_calls"]:
            indices = tools_by_id.get(call.get("id", ""))
            tool_index = indices.pop(0) if indices else None
            if tool_index is None:
                name = call.get("function", {}).get("name", "")
                output.append({
                    "role": "tool",
                    "content": [{
                        "type": "text",
                        "text": (
                            f"[Tool result missing: the call to '{name}' was "
                            "interrupted before its result was recorded. Treat "
                            "the call as failed.]"
                        ),
                    }],
                    "tool_call_id": call.get("id", ""),
                })
                continue
            claimed.add(tool_index)
            output.append(messages[tool_index])
    return output


def repair_tool_pairing(history) -> list[dict]:
    """Repair assistant tool_call / tool-result pairing."""
    if native_active():
        structure, hist_bytes = _pack(history)
        out_structure, out_history = _native.soul.repair_tool_pairing(
            structure, hist_bytes
        )
        return _structure.to_messages(out_structure, out_history)
    return _compat_repair_tool_pairing(history)


def _compat_extract_text(history, sep: str = "\n") -> str:
    return sep.join(_text_of(m, sep) for m in _messages_of(history))


def extract_text(history, sep: str = "\n") -> str:
    """Join every text part of the history with ``sep``."""
    if native_active():
        structure, hist_bytes = _pack(history)
        return _dec(_native.soul.extract_text(structure, hist_bytes, _enc(sep)))
    return _compat_extract_text(history, sep)


_CJK_RANGES = (
    (0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0x20000, 0x2EBEF), (0xAC00, 0xD7AF),
    (0x3040, 0x309F), (0x30A0, 0x30FF), (0xFF00, 0xFFEF),
)


def _estimate_chars_tokens(text: str) -> int:
    """Mirror of the kernel's estimate_text_tokens (utils/tokens.py heuristic).

    Mostly-ASCII text is ~4 chars/token, CJK-detected text ~3, mixed/code ~3.5;
    the ratios are computed over the WHOLE part, exactly like the C++ kernel.
    """
    if not text:
        return 0
    total = len(text)
    ascii_cp = sum(1 for ch in text if ord(ch) < 128)
    cjk_cp = sum(1 for ch in text if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES))
    if total == 0:
        return 0
    if ascii_cp / total > 0.95:
        return max(1, total // 4)
    if cjk_cp / total > 0.15:
        return max(1, total // 3)
    return max(1, int(total / 3.5))


def _compat_count_tokens(history) -> int:
    total = 0
    for message in _messages_of(history):
        for part in message.get("content") or []:
            if _structure.part_kind(part) == 0:
                total += _estimate_chars_tokens(part.get("text") or "")
    return total


def count_tokens(history) -> int:
    """Heuristic token estimate over every text part (arena analogue of
    count_message_tokens)."""
    if native_active():
        structure, hist_bytes = _pack(history)
        return int(_native.soul.count_tokens(structure, hist_bytes))
    return _compat_count_tokens(history)


def _tool_call_delta(message: dict) -> int:
    if message.get("role") == "assistant":
        count = len(message.get("tool_calls") or [])
        count += sum(
            1 for part in message.get("content") or []
            if _structure.part_kind(part) == 2
        )
        return count
    if message.get("role") == "tool":
        return -1
    return 0


def _compat_balanced_cut_indices(history) -> tuple[list[int], bool, int]:
    """tool_pairing.balanced_cut_indices.

    The reference raises ValueError on an orphan tool result; the native port
    stops the fold there and reports ``(cuts_so_far, True, index)`` - the mirror
    does exactly the same so both sides return the same partial list.
    """
    messages = _messages_of(history)
    cuts = [0]
    running = 0
    for index, message in enumerate(messages):
        running += _tool_call_delta(message)
        if running < 0:
            return cuts, True, index
        if running == 0:
            cuts.append(index + 1)
    if cuts[-1] != len(messages):
        cuts.append(len(messages))
    return cuts, False, -1


def balanced_cut_indices(history) -> tuple[list[int], bool, int]:
    """tool_pairing.balanced_cut_indices -> (cuts, unbalanced, index)."""
    if native_active():
        structure, hist_bytes = _pack(history)
        packed, unbalanced, index = _native.soul.balanced_cut_indices(
            structure, hist_bytes
        )
        return _structure.unpack_u32(packed), bool(unbalanced), int(index)
    return _compat_balanced_cut_indices(history)


def _compat_nearest_balanced_cut_before(history, index: int) -> tuple[int, bool]:
    messages = _messages_of(history)
    size = len(messages)
    if index < 0:
        return 0, False
    if index > size:
        return size, False
    cuts, unbalanced, _bad = _compat_balanced_cut_indices(messages)
    if unbalanced:
        # The reference propagates the ValueError; the port reports the flag and
        # no cut, which is what the kernel does.
        return 0, True
    best = 0
    for cut in cuts:
        if cut <= index:
            best = cut
    return best, False


def nearest_balanced_cut_before(history, index: int) -> tuple[int, bool]:
    if native_active():
        structure, hist_bytes = _pack(history)
        cut, unbalanced = _native.soul.nearest_balanced_cut_before(
            structure, hist_bytes, int(index)
        )
        return int(cut), bool(unbalanced)
    return _compat_nearest_balanced_cut_before(history, index)


def _compat_should_auto_compact(token_count: int, cfg: dict | None = None) -> bool:
    """kimi_cli/soul/compaction.py::should_auto_compact (Python mirror)."""
    o = dict(cfg or {})
    ratio = float(o.get("trigger_ratio", 0.75))
    max_context = int(o.get("max_context_size", 128000))
    reserved = int(o.get("reserved_context_size", 8192))
    max_tokens = int(o.get("max_tokens", 0) or 0)
    buffer_tokens = int(o.get("tool_call_buffer_tokens", 0) or 0)
    margin = int(o.get("safety_margin_tokens", 1024))
    budget = reserved + max_tokens + buffer_tokens + margin
    threshold = min(ratio * max_context - budget, max_context - budget)
    return token_count > threshold


def should_auto_compact(token_count: int, cfg: dict | None = None) -> bool:
    """Compaction trigger (compaction.py::should_auto_compact)."""
    if native_active():
        return bool(_native.soul.should_auto_compact(int(token_count), dict(cfg or {})))
    return _compat_should_auto_compact(token_count, cfg)


# The compaction prompt/prepare trio keeps ONE native implementation of the
# prose-heavy assembly (src/builtin_tools/compact_tool.cpp). Its Python route is
# the legacy list[dict] binding (`runtime_py.builtin_tools.web`), which is the
# parity reference the plan names; with the compiled module absent there is no
# Python route and the caller keeps its own implementation (the sibling repo's).
def _legacy_web():
    if _native is None:
        raise RuntimeError(
            "soul compaction prompts need the native module; use the kimi-agent "
            "implementation when KIMIX_NATIVE=0"
        )
    return _native.builtin_tools.web


def resolve_preserve_split(history, preserve_depth: int, balanced_cuts: bool = True) -> dict:
    """SimpleCompaction.prepare's boundary math, on the arena."""
    if native_active():
        structure, hist_bytes = _pack(history)
        return dict(
            _native.soul.resolve_preserve_split(
                structure, hist_bytes, int(preserve_depth), bool(balanced_cuts)
            )
        )
    return dict(
        _legacy_web().resolve_preserve_split(
            _messages_of(history), int(preserve_depth), bool(balanced_cuts)
        )
    )


def build_compaction_prompt(
    history,
    options: dict | None = None,
    custom_instruction: str = "",
    prompt_compact: str = "",
    prompt_compact_cascade: str = "",
) -> tuple[str, int]:
    """Assemble the compaction instruction text -> (prompt_text, cascade_depth)."""
    if native_active():
        structure, hist_bytes = _pack(history)
        text, depth = _native.soul.build_compaction_prompt(
            structure, hist_bytes, dict(options or {}), custom_instruction,
            prompt_compact, prompt_compact_cascade,
        )
        return _dec(text), int(depth)
    out = _legacy_web().build_compaction_prompt(
        _messages_of(history), dict(options or {}), custom_instruction,
        prompt_compact, prompt_compact_cascade,
    )
    return out["prompt_text"], int(out["cascade_depth"])


def build_compact_message_text(history, prompt_text: str) -> str:
    if native_active():
        structure, hist_bytes = _pack(history)
        return _dec(
            _native.soul.build_compact_message_text(structure, hist_bytes, prompt_text)
        )
    return _legacy_web().build_compact_message_text(
        _messages_of(history), prompt_text
    )


def prepare_compaction_input(
    history,
    preserve_start_index: int,
    options: dict | None = None,
    custom_instruction: str = "",
    prompt_compact: str = "",
    prompt_compact_cascade: str = "",
) -> dict:
    """Slice + prompt + legacy compact message on the packed arena.

    Returns ``{"to_compact": list[dict], "to_preserve": list[dict],
    "compact_message_text": str, "prompt_text": str, "cascade_depth": int}`` -
    the same shape as the legacy dict binding.
    """
    if native_active():
        structure, hist_bytes = _pack(history)
        (c_struct, c_hist, p_struct, p_hist, compact_text, prompt_text,
         depth) = _native.soul.prepare_compaction_input(
            structure, hist_bytes, int(preserve_start_index), dict(options or {}),
            custom_instruction, prompt_compact, prompt_compact_cascade,
        )
        return {
            "to_compact": _structure.to_messages(c_struct, c_hist),
            "to_preserve": _structure.to_messages(p_struct, p_hist),
            "compact_message_text": _dec(compact_text),
            "prompt_text": _dec(prompt_text),
            "cascade_depth": int(depth),
        }
    return dict(
        _legacy_web().prepare_compaction_input(
            _messages_of(history), int(preserve_start_index), dict(options or {}),
            custom_instruction, prompt_compact, prompt_compact_cascade,
        )
    )


# ---------------------------------------------------------------------------
# B4: export markdown on the packed bridge
# ---------------------------------------------------------------------------


def build_export_markdown(history, opts: dict) -> bytes:
    """Full session export markdown (reference utils/export.py)."""
    if native_active():
        structure, hist_bytes = _pack(history)
        return bytes(
            _native.soul.build_export_markdown(structure, hist_bytes, dict(opts))
        )
    from . import tools as _tools

    return _tools._compat_build_export_markdown(_messages_of(history), opts)


# ---------------------------------------------------------------------------
# B1: the provider request builder
# ---------------------------------------------------------------------------


def _compat_build_request(history, options: dict) -> bytes:
    """Pure-Python mirror of soul.build_request (see request_builder.h).

    Kept byte-identical to the kernel: same key order, same optional-key rules.
    """
    provider = str(options.get("provider", "openai_legacy"))
    stream = bool(options.get("stream", True))
    max_tokens = int(options.get("max_tokens", 0) or 0)
    preserve_thinking = bool(options.get("preserve_thinking", False))
    reasoning_key = str(options.get("reasoning_key", ""))
    system_prompt = str(options.get("system_prompt", ""))
    tools_json = options.get("tools_json")
    tools = orjson.loads(tools_json) if tools_json else None

    messages = _compat_normalize_history(_compat_repair_tool_pairing(history))

    def visible(parts):
        return [p for p in parts if _structure.part_kind(p) != 1]

    def openai_content(message: dict):
        parts = message.get("content") or []
        seen = visible(parts)
        if not seen:
            return None if message.get("tool_calls") else ""
        if len(seen) == 1 and _structure.part_kind(seen[0]) == 0:
            return seen[0].get("text") or ""
        blocks = []
        for part in seen:
            if _structure.part_kind(part) == 0:
                blocks.append({"type": "text", "text": part.get("text") or ""})
            else:
                blocks.append({k: v for k, v in part.items() if v is not None})
        return blocks

    if provider in ("openai_legacy", "openai", "openai_chat", "kimi"):
        body: dict = {"model": str(options.get("model", "")), "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if max_tokens > 0:
            key = "max_completion_tokens" if provider == "kimi" else "max_tokens"
            body[key] = max_tokens
        if "temperature" in options:
            body["temperature"] = float(options["temperature"])
        out_messages = []
        for message in messages:
            entry: dict = {"role": message.get("role", "user")}
            content = openai_content(message)
            if content is not None or not message.get("tool_calls"):
                entry["content"] = content
            if message.get("tool_call_id") is not None:
                entry["tool_call_id"] = message["tool_call_id"]
            if message.get("tool_calls"):
                calls = []
                for call in message["tool_calls"]:
                    function = {"name": call["function"]["name"]}
                    if call["function"].get("arguments") is not None:
                        function["arguments"] = call["function"]["arguments"]
                    calls.append({"id": call["id"], "type": "function",
                                  "function": function})
                entry["tool_calls"] = calls
            if preserve_thinking and reasoning_key and message.get("role") == "assistant":
                thinking = "\n".join(
                    part.get("think") or ""
                    for part in message.get("content") or []
                    if _structure.part_kind(part) == 1
                )
                if thinking:
                    entry[reasoning_key] = thinking
            out_messages.append(entry)
        body["messages"] = out_messages
        if tools is not None:
            body["tools"] = tools
        if options.get("tool_choice"):
            body["tool_choice"] = orjson.loads(options["tool_choice"]) if str(
                options["tool_choice"]
            ).lstrip().startswith(("{", "[")) else options["tool_choice"]
        if options.get("reasoning_effort"):
            body["reasoning_effort"] = options["reasoning_effort"]
        return orjson.dumps(body)

    if provider == "anthropic":
        body = {"model": str(options.get("model", "")), "stream": stream}
        if max_tokens > 0:
            body["max_tokens"] = max_tokens
        if "temperature" in options:
            body["temperature"] = float(options["temperature"])
        system_parts = [system_prompt] if system_prompt else []
        out_messages = []
        for message in messages:
            role = message.get("role", "user")
            if role == "system":
                text = _text_of(message)
                if text:
                    system_parts.append(text)
                continue
            blocks = []
            if role == "tool":
                inner = [
                    {"type": "text", "text": p.get("text") or ""}
                    for p in message.get("content") or []
                    if _structure.part_kind(p) == 0
                ]
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": inner,
                })
            else:
                for part in message.get("content") or []:
                    kind = _structure.part_kind(part)
                    if kind == 0:
                        blocks.append({"type": "text", "text": part.get("text") or ""})
                    elif kind == 1 and preserve_thinking:
                        blocks.append({"type": "thinking",
                                       "thinking": part.get("think") or ""})
                    elif kind not in (1, 2):
                        blocks.append({k: v for k, v in part.items() if v is not None})
                for call in message.get("tool_calls") or []:
                    import json as _json

                    try:
                        payload = _json.loads(call["function"].get("arguments") or "{}")
                    except Exception:
                        payload = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": payload,
                    })
            out_messages.append({
                "role": "user" if role in ("tool", "user") else "assistant",
                "content": blocks,
            })
        if system_parts:
            body["system"] = "\n".join(system_parts)
        body["messages"] = out_messages
        if tools is not None:
            body["tools"] = tools
        return orjson.dumps(body)

    # openai_responses
    body = {"model": str(options.get("model", "")), "stream": stream}
    if system_prompt:
        body["instructions"] = system_prompt
    if max_tokens > 0:
        body["max_output_tokens"] = max_tokens
    if "temperature" in options:
        body["temperature"] = float(options["temperature"])
    items = []
    for message in messages:
        role = message.get("role", "user")
        if role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": message.get("tool_call_id", ""),
                "output": _text_of(message),
            })
            continue
        if role == "assistant" and message.get("tool_calls"):
            for call in message["tool_calls"]:
                items.append({
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"].get("arguments") or "{}",
                })
        text_type = "output_text" if role == "assistant" else "input_text"
        items.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": [
                {"type": text_type, "text": p.get("text") or ""}
                for p in message.get("content") or []
                if _structure.part_kind(p) == 0
            ],
        })
    body["input"] = items
    if tools is not None:
        body["tools"] = tools
    return orjson.dumps(body)


def build_request(history, options: dict) -> bytes:
    """Provider request body from the arena (report B1).

    Gated per provider: ``KIMIX_NATIVE_REQUEST_<PROVIDER>=0`` isolates one
    provider, and the shim keeps ``_compat_build_request`` as the comparison
    target (``python -m kimix_native.soul --diff-bodies``).
    """
    provider = str(options.get("provider", "openai_legacy"))
    if _request_gate(provider):
        structure, hist_bytes = _pack(history)
        return bytes(_native.soul.build_request(structure, hist_bytes, dict(options)))
    return _compat_build_request(history, options)


def _main(argv=None) -> int:
    """--diff-bodies: compare native vs _compat for a fixture directory."""
    import argparse
    import json
    import pathlib
    import sys

    parser = argparse.ArgumentParser(prog="kimix_native.soul")
    parser.add_argument(
        "--diff-bodies",
        metavar="DIR",
        help="directory of *.json fixtures: {'history': [...], 'options': {...}}",
    )
    args = parser.parse_args(argv)
    if not args.diff_bodies:
        parser.print_help()
        return 0
    failures = 0
    for path in sorted(pathlib.Path(args.diff_bodies).glob("*.json")):
        fixture = json.loads(path.read_text(encoding="utf-8"))
        native = build_request(fixture["history"], fixture["options"])
        mirror = _compat_build_request(fixture["history"], fixture["options"])
        if native != mirror:
            failures += 1
            print(f"MISMATCH {path.name}")
            print("  native:", native[:200])
            print("  compat:", mirror[:200])
        else:
            print(f"ok       {path.name}")
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(_main())
