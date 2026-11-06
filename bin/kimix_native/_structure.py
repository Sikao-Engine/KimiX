"""KMB2 — the packed message-structure codec shared by the soul kernels.

The native entry points take BYTES + SPAN TABLES, never ``list[dict]``: the
report measured a ``balanced_cut_indices`` call over the dict bridge at
4.66 ms / 435 KB peak against 0.43 ms / 76 KB in pure Python, i.e. moving a
loop to C++ over a per-item dict boundary makes GC memory *worse*.

This module is the pure-Python half of the packed ABI (the C++ half is
``src/runtime/soul/kmb2.{h,cpp}``):

* one text buffer holds every string of every message;
* one ``bytearray`` holds three fixed-width tables (messages / parts /
  tool calls) whose offsets point into that text buffer;
* the whole thing is TWO ``bytes`` objects, built with one ``struct.pack_into``
  per record (no per-item Python objects).

Layout (little-endian, every offset relative to the text buffer)::

    header (32 B)   u32 magic 'KMB2' | u32 version=1 | u32 msg_count |
                    u32 part_total | u32 call_total | u32 flags |
                    u32 reserved | u32 reserved
    message (32 B)  u8 role, u8 flags, u16 -, u32 part_first, u32 part_count,
                    u32 call_first, u32 call_count, i32 tcid_start,
                    i32 tcid_end, u32 -
    part (12 B)     i32 start, i32 end, u8 kind, u8 flags, u16 -
    call (24 B)     i32 id_s, i32 id_e, i32 name_s, i32 name_e,
                    i32 args_s (-1 = none), i32 args_e

Message flags: bit0 tool_call_id present, bit1 tool_calls present,
bit2 has a think part. Roles: 0 system, 1 user, 2 assistant, 3 tool.
Part kinds: 0 text, 1 think, 2 tool_call, 3 image, 4 audio, 5 file, 6 other.

The module is import-light (stdlib + orjson) so ``KIMIX_NATIVE_SOUL=0`` still
works without the compiled module: ``to_messages`` is the reference decoder the
native path is tested against.
"""

from __future__ import annotations

import struct

import orjson

from ._common import _compact, _dec, _enc

MAGIC = 0x32424D4B  # 'KMB2' little-endian
VERSION = 1
HEADER_BYTES = 32
MESSAGE_BYTES = 32
PART_BYTES = 12
CALL_BYTES = 24

FLAG_TOOL_CALL_ID = 1 << 0
FLAG_TOOL_CALLS = 1 << 1
FLAG_HAS_THINK = 1 << 2
FLAG_UTF8 = 1 << 0

ROLE_TO_INT = {"system": 0, "user": 1, "assistant": 2, "tool": 3}
INT_TO_ROLE = ("system", "user", "assistant", "tool")

# content-part "type" -> part_kind
PART_TYPE_KIND = {
    "text": 0,
    "think": 1,
    "tool_call": 2,
    "image_url": 3,
    "audio_url": 4,
    "video_url": 5,
    "file": 5,
}

_HEADER = struct.Struct("<IIIIIIII")
_MESSAGE = struct.Struct("<BBHIIIIiiI")
_PART = struct.Struct("<iiBBH")
_CALL = struct.Struct("<iiiiii")
_U32 = struct.Struct("<I")


def part_kind(part: dict) -> int:
    """Map a content part to its kind (the C++ ``part_kind`` enum)."""
    return PART_TYPE_KIND.get(part.get("type", ""), 6)


def part_payload(part: dict) -> bytes:
    """exclude_none part dump (what the kernels embed verbatim)."""
    return _compact({k: v for k, v in part.items() if v is not None})


def normalize_message(message: dict) -> dict:
    """Canonical dict form of one message.

    ``soul.History[i]`` and the pure-Python ``_CompatHistory`` both return this
    shape, so the two are comparable field by field. It is also exactly the
    information the arena stores.
    """
    out: dict = {"role": message.get("role", "user")}
    content = []
    for part in message.get("content") or []:
        kind = part_kind(part)
        if kind == 0:
            content.append({"type": "text", "text": part.get("text") or ""})
        elif kind == 1:
            content.append({"type": "think", "think": part.get("think") or ""})
        else:
            content.append({k: v for k, v in part.items() if v is not None})
    out["content"] = content
    calls = message.get("tool_calls")
    if calls is not None:
        normalized = []
        for call in calls:
            function = call.get("function", {}) or {}
            entry = {
                "id": call.get("id", ""),
                "function": {"name": function.get("name", "")},
            }
            if function.get("arguments") is not None:
                entry["function"]["arguments"] = function["arguments"]
            normalized.append(entry)
        out["tool_calls"] = normalized
    tool_call_id = message.get("tool_call_id")
    if tool_call_id is not None:
        out["tool_call_id"] = tool_call_id
    return out


def build(history) -> tuple[bytes, bytes]:
    """Encode a message list into ``(history_bytes, structure_bytes)``."""
    text = bytearray()
    messages: list[tuple] = []
    parts: list[tuple] = []
    calls: list[tuple] = []

    for message in history:
        role = ROLE_TO_INT.get(message.get("role"), 1)
        flags = 0

        tool_call_id = message.get("tool_call_id")
        if tool_call_id is None:
            tcid_start = tcid_end = -1
        else:
            data = _enc(tool_call_id)
            tcid_start = len(text)
            text += data
            tcid_end = len(text)
            flags |= FLAG_TOOL_CALL_ID

        call_first = len(calls)
        tool_calls = message.get("tool_calls")
        if tool_calls is not None:
            flags |= FLAG_TOOL_CALLS
            for call in tool_calls:
                function = call.get("function", {}) or {}
                call_id = _enc(call.get("id", ""))
                name = _enc(function.get("name", ""))
                arguments = function.get("arguments")
                id_s = len(text)
                text += call_id
                id_e = len(text)
                name_s = len(text)
                text += name
                name_e = len(text)
                if arguments is None:
                    args_s = args_e = -1
                else:
                    data = _enc(arguments)
                    args_s = len(text)
                    text += data
                    args_e = len(text)
                calls.append((id_s, id_e, name_s, name_e, args_s, args_e))
        call_count = len(calls) - call_first

        part_first = len(parts)
        for part in message.get("content") or []:
            kind = part_kind(part)
            if kind in (0, 1):
                payload = _enc(part.get("text") or part.get("think") or "")
            else:
                payload = part_payload(part)
            start = len(text)
            text += payload
            parts.append((start, len(text), kind, 0, 0))
            if kind == 1:
                flags |= FLAG_HAS_THINK
        part_count = len(parts) - part_first

        messages.append(
            (role, flags, 0, part_first, part_count, call_first, call_count,
             tcid_start, tcid_end, 0)
        )

    structure = bytearray(
        HEADER_BYTES
        + MESSAGE_BYTES * len(messages)
        + PART_BYTES * len(parts)
        + CALL_BYTES * len(calls)
    )
    _HEADER.pack_into(
        structure, 0, MAGIC, VERSION, len(messages), len(parts), len(calls),
        FLAG_UTF8, 0, 0,
    )
    offset = HEADER_BYTES
    for record in messages:
        _MESSAGE.pack_into(structure, offset, *record)
        offset += MESSAGE_BYTES
    for record in parts:
        _PART.pack_into(structure, offset, *record)
        offset += PART_BYTES
    for record in calls:
        _CALL.pack_into(structure, offset, *record)
        offset += CALL_BYTES
    return bytes(text), bytes(structure)


def message_count(structure: bytes) -> int:
    """Number of messages in a packed structure blob."""
    if len(structure) < HEADER_BYTES:
        return 0
    magic, _version, count, _parts, _calls, _flags, _r0, _r1 = _HEADER.unpack_from(
        structure, 0
    )
    if magic != MAGIC:
        return 0
    return count


def to_messages(structure: bytes, history: bytes) -> list[dict]:
    """Decode a packed pair back into the canonical message dicts."""
    if len(structure) < HEADER_BYTES:
        return []
    magic, _version, msg_count, part_total, call_total, _flags, _r0, _r1 = (
        _HEADER.unpack_from(structure, 0)
    )
    if magic != MAGIC:
        return []
    required = (
        HEADER_BYTES
        + MESSAGE_BYTES * msg_count
        + PART_BYTES * part_total
        + CALL_BYTES * call_total
    )
    if len(structure) < required:
        return []
    msg_off = HEADER_BYTES
    part_off = msg_off + MESSAGE_BYTES * msg_count
    call_off = part_off + PART_BYTES * part_total
    out: list[dict] = []
    for _ in range(msg_count):
        (role, flags, _r, part_first, part_count, call_first, call_count,
         tcid_start, tcid_end, _r2) = _MESSAGE.unpack_from(structure, msg_off)
        msg_off += MESSAGE_BYTES
        message: dict = {"role": INT_TO_ROLE[role] if role < 4 else "user"}
        content = []
        for index in range(part_first, part_first + part_count):
            start, end, kind, _pf, _pr = _PART.unpack_from(
                structure, part_off + PART_BYTES * index
            )
            payload = history[start:end]
            if kind == 0:
                content.append({"type": "text", "text": _dec(payload)})
            elif kind == 1:
                content.append({"type": "think", "think": _dec(payload)})
            else:
                content.append(orjson.loads(payload) if payload else {})
        message["content"] = content
        if call_count:
            calls = []
            for index in range(call_first, call_first + call_count):
                id_s, id_e, name_s, name_e, args_s, args_e = _CALL.unpack_from(
                    structure, call_off + CALL_BYTES * index
                )
                function: dict = {"name": _dec(history[name_s:name_e])}
                if args_s >= 0:
                    function["arguments"] = _dec(history[args_s:args_e])
                calls.append({"id": _dec(history[id_s:id_e]), "function": function})
            message["tool_calls"] = calls
        if flags & FLAG_TOOL_CALL_ID:
            message["tool_call_id"] = _dec(history[tcid_start:tcid_end])
        out.append(message)
    return out


def unpack_one(structure: bytes, history: bytes, index: int) -> dict | None:
    """Decode ONE message of a packed pair (the ``__getitem__`` fast path)."""
    if len(structure) < HEADER_BYTES:
        return None
    magic, _version, msg_count, _parts, _calls, _flags, _r0, _r1 = (
        _HEADER.unpack_from(structure, 0)
    )
    if magic != MAGIC or index < 0 or index >= msg_count:
        return None
    # Slice the requested record out and decode it with an adjusted table.
    msg_off = HEADER_BYTES + MESSAGE_BYTES * index
    record = _MESSAGE.unpack_from(structure, msg_off)
    (role, flags, _r, part_first, part_count, call_first, call_count,
     tcid_start, tcid_end, _r2) = record
    part_total = _HEADER.unpack_from(structure, 0)[3]
    call_total = _HEADER.unpack_from(structure, 0)[4]
    part_table = HEADER_BYTES + MESSAGE_BYTES * msg_count
    call_table = part_table + PART_BYTES * part_total
    # Rebuild a one-message blob that reuses the same (absolute) spans.
    single = bytearray(HEADER_BYTES + MESSAGE_BYTES)
    _HEADER.pack_into(single, 0, MAGIC, VERSION, 1, part_total, call_total,
                      _flags, 0, 0)
    _MESSAGE.pack_into(
        single, HEADER_BYTES, role, flags, 0, part_first, part_count,
        call_first, call_count, tcid_start, tcid_end, 0,
    )
    # to_messages() walks the part/call tables by absolute record index, so the
    # reconstructed header must keep the full tables: append them verbatim.
    single += structure[part_table:part_table + PART_BYTES * part_total]
    single += structure[call_table:call_table + CALL_BYTES * call_total]
    messages = to_messages(bytes(single), history)
    if not messages:
        return None
    return messages[0]


def pack_u32(values) -> bytes:
    """``u32 count | count * u32`` — the layout ``soul.pack_u32`` produces."""
    values = list(values)
    return _U32.pack(len(values)) + struct.pack(f"<{len(values)}I", *values)


def unpack_u32(blob: bytes) -> list[int]:
    """Decode the ``u32 count | count * u32`` layout."""
    if len(blob) < 4:
        return []
    (count,) = _U32.unpack_from(blob, 0)
    if len(blob) < 4 + 4 * count:
        return []
    return list(struct.unpack_from(f"<{count}I", blob, 4))
