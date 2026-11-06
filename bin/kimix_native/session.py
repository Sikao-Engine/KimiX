"""session — per-session part ring + session table (report A3 + C3).

`kimix serve` keeps a second full copy of every conversation: one
``MessagePart``/``MessageInfo``/``MessageWithParts`` dataclass graph per event,
with four ``Dict[str, Any]`` fields per part (1 949 859 B of mirror for 782 332 B
of source text, 2.49x) and a ``SessionManager._sessions`` dict that is never
evicted. This module owns those bytes natively:

* one part ring per session under an explicit per-session byte budget
  (drop-oldest, like the reference's "as many objects as the client pulled"
  becomes a cap);
* a total budget with LRU eviction over whole sessions;
* ``snapshot()`` renders the opencode-shaped ``MessageWithParts`` JSON in C++, so
  ``to_dict()`` disappears from the SSE hot path (the endpoint writes the
  returned bytes directly).

Gate: ``KIMIX_NATIVE_SESSION=0`` routes to :class:`_CompatStore`, whose JSON is
byte-identical to the native writer (``python/tests/test_session_store.py``
compares them).
"""

from __future__ import annotations

import orjson

from . import _native, use_native
from ._common import _dec, _enc

PART_KINDS = ("text", "reasoning", "tool", "step-start", "step-finish")

STATUS_IDLE = 0
STATUS_BUSY = 1
STATUS_RETRY = 2


def native_active() -> bool:
    return _native is not None and use_native("SESSION")


class Store:
    """Native session store (report A3 + C3)."""

    __slots__ = ("_s", "_closed", "_compat")

    def __init__(self, per_session_budget: int = 4 << 20, total_budget: int = 64 << 20):
        self._s = (
            _native.session.Store(per_session_budget, total_budget)
            if native_active()
            else None
        )
        self._compat = _CompatStore(per_session_budget, total_budget)
        self._closed = False

    # -- lifecycle ------------------------------------------------------
    def create(self, session_id: str) -> int:
        if self._s is not None:
            return int(self._s.create(session_id))
        return self._compat.create(session_id)

    def close(self, session: int) -> bool:
        if self._s is not None:
            return bool(self._s.close(session))
        return self._compat.close(session)

    def clear(self) -> None:
        if self._s is not None:
            self._s.clear()
            self._closed = True
        self._compat.clear()

    # -- writes ---------------------------------------------------------
    def append(self, session: int, kind: str, role: str = "assistant", **fields) -> bool:
        """Append one part.

        ``fields`` are bytes-valued ids/payloads (``session_id``, ``message_id``,
        ``part_id``, ``call_id``, ``tool``, ``text``, ``state_json``,
        ``prompt_json``, ``tokens_json``, ``time_json``, ``snapshot``,
        ``reason``, ``cost``, ``seq``).
        """
        if kind not in PART_KINDS:
            raise ValueError(f"unknown part kind: {kind!r}")
        if self._s is not None:
            return bool(self._s.append(session, kind, role, **fields))
        return self._compat.append(session, kind, role, **fields)

    def set_status(self, session: int, status: int) -> None:
        if self._s is not None:
            self._s.set_status(session, int(status))
        else:
            self._compat.set_status(session, int(status))

    def status(self, session: int) -> int:
        if self._s is not None:
            return int(self._s.status(session))
        return self._compat.status(session)

    def evict(self) -> int:
        if self._s is not None:
            return int(self._s.evict())
        return self._compat.evict()

    # -- reads ----------------------------------------------------------
    def snapshot(self, session: int, since_seq: int = 0) -> bytes:
        if self._s is not None:
            return bytes(self._s.snapshot(session, int(since_seq)))
        return self._compat.snapshot(session, int(since_seq))

    def part_json(self, session: int, part_id: bytes) -> bytes | None:
        if self._s is not None:
            return self._s.part_json(session, part_id)
        return self._compat.part_json(session, part_id)

    def sessions(self) -> int:
        return int(self._s.sessions()) if self._s is not None else self._compat.sessions()

    def part_count(self, session: int) -> int:
        if self._s is not None:
            return int(self._s.part_count(session))
        return self._compat.part_count(session)

    def dropped(self, session: int) -> int:
        if self._s is not None:
            return int(self._s.dropped(session))
        return self._compat.dropped(session)

    def generation(self, session: int) -> int:
        if self._s is not None:
            return int(self._s.generation(session))
        return self._compat.generation(session)

    def bytes_owned(self) -> int:
        return int(self._s.bytes_owned()) if self._s is not None else 0

    def bytes_live(self) -> int:
        return int(self._s.bytes_live()) if self._s is not None else 0

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> bool:
        self.clear()
        return False

    def __del__(self):
        try:
            if self._s is not None and not self._closed:
                self._s.clear()
                self._closed = True
        except Exception:  # pragma: no cover - shutdown path
            pass


class _CompatStore:
    """Pure-Python mirror of :class:`Store` (byte-identical snapshot JSON)."""

    __slots__ = ("_per_session", "_total", "_sessions", "_lru", "_counter")

    def __init__(self, per_session_budget: int = 4 << 20, total_budget: int = 64 << 20):
        self._per_session = per_session_budget
        self._total = total_budget
        self._sessions: dict[int, dict] = {}
        self._lru: dict[int, int] = {}
        self._counter = 0

    def create(self, session_id: str) -> int:
        for handle, entry in self._sessions.items():
            if entry["id"] == session_id:
                self._touch(handle)
                return handle
        handle = len(self._sessions) + 1
        self._sessions[handle] = {
            "id": session_id,
            "parts": [],
            "status": STATUS_IDLE,
            "next_seq": 1,
            "bytes": 0,
            "dropped": 0,
            "gen": 0,
        }
        self._touch(handle)
        return handle

    def _touch(self, handle: int) -> None:
        self._counter += 1
        self._lru[handle] = self._counter

    def close(self, handle: int) -> bool:
        if handle not in self._sessions:
            return False
        del self._sessions[handle]
        self._lru.pop(handle, None)
        return True

    def clear(self) -> None:
        self._sessions.clear()
        self._lru.clear()

    @staticmethod
    def _size(record: dict) -> int:
        return sum(
            len(record.get(key) or b"")
            for key in (
                "session_id", "message_id", "part_id", "call_id", "tool", "text",
                "state_json", "prompt_json", "tokens_json", "time_json",
                "snapshot", "reason",
            )
        )

    def append(self, handle: int, kind: str, role: str = "assistant", **fields) -> bool:
        entry = self._sessions.get(handle)
        if entry is None:
            return False
        record = {"kind": kind, "role": role}
        record.update(fields)
        size = self._size(record)
        if self._per_session and size > self._per_session:
            return False
        record["seq"] = int(fields.get("seq") or entry["next_seq"])
        entry["next_seq"] = record["seq"] + 1
        entry["parts"].append(record)
        entry["bytes"] += size
        entry["gen"] += 1
        self._touch(handle)
        while self._per_session and entry["bytes"] > self._per_session and entry["parts"]:
            dropped = entry["parts"].pop(0)
            entry["bytes"] -= self._size(dropped)
            entry["dropped"] += 1
        self.evict()
        return True

    def evict(self) -> int:
        if not self._total:
            return 0
        evicted = 0
        while sum(e["bytes"] for e in self._sessions.values()) > self._total:
            victim = min(self._sessions, key=lambda h: self._lru.get(h, 0), default=None)
            if victim is None:
                break
            self.close(victim)
            evicted += 1
        return evicted

    def set_status(self, handle: int, status: int) -> None:
        entry = self._sessions.get(handle)
        if entry is not None:
            entry["status"] = status
            entry["gen"] += 1
            self._touch(handle)

    def status(self, handle: int) -> int:
        entry = self._sessions.get(handle)
        return STATUS_IDLE if entry is None else entry["status"]

    @staticmethod
    def _text(value) -> str:
        if value is None:
            return ""
        return value if isinstance(value, str) else _dec(value)

    @classmethod
    def _json(cls, value):
        """A pre-serialized JSON object as the native writer embeds it."""
        if not value:
            return None
        if not isinstance(value, (bytes, bytearray, str)):
            return value
        try:
            return orjson.loads(value)
        except Exception:
            return cls._text(value)

    @classmethod
    def _part_dict(cls, record: dict) -> dict:
        kind = record["kind"]
        out = {
            "id": cls._text(record.get("part_id")),
            "sessionID": cls._text(record.get("session_id")),
            "messageID": cls._text(record.get("message_id")),
            "type": kind,
        }
        if kind in ("text", "reasoning"):
            out["text"] = cls._text(record.get("text"))
            if record.get("time_json"):
                out["time"] = cls._json(record["time_json"])
        elif kind == "tool":
            out["callID"] = cls._text(record.get("call_id"))
            out["tool"] = cls._text(record.get("tool"))
            state = cls._json(record.get("state_json"))
            out["state"] = {} if state is None else state
        elif kind == "step-start":
            out["snapshot"] = cls._text(record.get("snapshot"))
        elif kind == "step-finish":
            out["reason"] = cls._text(record.get("reason"))
            out["snapshot"] = cls._text(record.get("snapshot"))
            if record.get("cost"):
                out["cost"] = float(record["cost"])
            if record.get("prompt_json"):
                out["prompt"] = cls._json(record["prompt_json"])
            if record.get("tokens_json"):
                out["tokens"] = cls._json(record["tokens_json"])
        return out

    def snapshot(self, handle: int, since_seq: int = 0) -> bytes:
        entry = self._sessions.get(handle)
        if entry is None:
            raise ValueError("no such session")
        self._touch(handle)
        parts = [
            self._part_dict(record)
            for record in entry["parts"]
            if record["seq"] >= since_seq
        ]
        payload = {
            "sessionID": self._text(entry["id"]),
            "status": entry["status"],
            "seq": entry["parts"][-1]["seq"] if entry["parts"] else 0,
            "parts": parts,
        }
        return orjson.dumps(payload)

    def part_json(self, handle: int, part_id: bytes) -> bytes | None:
        entry = self._sessions.get(handle)
        if entry is None:
            return None
        for record in entry["parts"]:
            if self._text(record.get("part_id")) == self._text(part_id):
                return orjson.dumps(self._part_dict(record))
        return None

    def sessions(self) -> int:
        return len(self._sessions)

    def part_count(self, handle: int) -> int:
        entry = self._sessions.get(handle)
        return 0 if entry is None else len(entry["parts"])

    def dropped(self, handle: int) -> int:
        entry = self._sessions.get(handle)
        return 0 if entry is None else entry["dropped"]

    def generation(self, handle: int) -> int:
        entry = self._sessions.get(handle)
        return 0 if entry is None else entry["gen"]


def new_store(per_session_budget: int = 4 << 20, total_budget: int = 64 << 20):
    """The gated factory: native store when available, Python mirror otherwise."""
    return Store(per_session_budget, total_budget)


def _enc_id(value: str | bytes | None) -> bytes:
    if value is None:
        return b""
    return value if isinstance(value, bytes) else _enc(value)


def text_of(store: Store | _CompatStore, session: int, part_id: bytes) -> str | None:
    """Decode one rendered part (small convenience for the SSE path)."""
    payload = store.part_json(session, part_id)
    return None if payload is None else _dec(payload)
