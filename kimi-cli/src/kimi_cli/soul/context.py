from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import orjson
from kosong.message import Message
from pydantic import ValidationError

from kimi_cli.soul.compaction import estimate_text_tokens
from kimi_cli.soul.context_records import ExportedContext
from kimi_cli.soul.jsonl_io import JsonlAppender, iter_jsonl_records
from kimi_cli.soul.message import system
from kimi_cli.utils.logging import logger


# ---------------------------------------------------------------------------
# ContextStorage protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ContextStorage(Protocol):
    """Durable, ordered storage for messages, prompts, checkpoints and usage."""

    async def initialize(self) -> None: ...
    async def close(self) -> None: ...
    async def get_system_prompt(self) -> str | None: ...
    async def set_system_prompt(self, content: str) -> None: ...
    async def append_messages(self, messages: Sequence[Message]) -> None: ...
    async def get_messages(
        self, *, after_rowid: int = 0, limit: int | None = None
    ) -> list[Message]: ...
    async def get_message_count(self) -> int: ...
    async def has_visible_messages(self) -> bool: ...
    async def create_checkpoint(self, checkpoint_id: int) -> int: ...
    async def get_latest_checkpoint_id(self) -> int: ...
    async def revert_to_checkpoint(self, checkpoint_id: int) -> None: ...
    async def record_usage(self, token_count: int) -> None: ...
    async def get_latest_usage(self) -> int | None: ...
    async def clear(self) -> None: ...
    async def export(self) -> ExportedContext: ...

    async def restore_full(self) -> tuple[str | None, list[Message], int | None, int, list[Message]]:
        """Return prompt, messages, usage, next checkpoint and pending messages."""
        ...


# ---------------------------------------------------------------------------
# Shared append-only replay state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContextMessageEntry:
    message: Message
    created_at: float


@dataclass(slots=True)
class UsageSnapshot:
    token_count: int
    message_count: int


@dataclass(slots=True)
class CheckpointState:
    """The active state immediately before a checkpoint record."""

    message_count: int
    usage_count: int
    system_prompt: str | None
    checkpoint_ids: tuple[int, ...]
    next_checkpoint_id: int


@dataclass(slots=True)
class ContextReplayState:
    system_prompt: str | None = None
    entries: list[ContextMessageEntry] = field(default_factory=list)
    usages: list[UsageSnapshot] = field(default_factory=list)
    checkpoints: dict[int, CheckpointState] = field(default_factory=dict)
    next_checkpoint_id: int = 0

    @property
    def messages(self) -> list[Message]:
        return [entry.message for entry in self.entries]

    @property
    def pending_message_start(self) -> int:
        if not self.usages:
            return 0
        # A corrupt future count must not produce a negative slice or discard
        # every pending message.  Valid writes always store an in-range count.
        return min(max(self.usages[-1].message_count, 0), len(self.entries))

    @property
    def pending_messages(self) -> list[Message]:
        return [entry.message for entry in self.entries[self.pending_message_start :]]

    @property
    def latest_usage(self) -> int | None:
        return self.usages[-1].token_count if self.usages else None

    @property
    def latest_checkpoint_id(self) -> int:
        return max(self.checkpoints, default=-1)

    def reset(self) -> None:
        self.system_prompt = None
        self.entries.clear()
        self.usages.clear()
        self.checkpoints.clear()
        self.next_checkpoint_id = 0

    def revert(self, checkpoint_id: int) -> bool:
        checkpoint = self.checkpoints.get(checkpoint_id)
        if checkpoint is None:
            return False
        self.entries = self.entries[: checkpoint.message_count]
        self.usages = self.usages[: checkpoint.usage_count]
        self.system_prompt = checkpoint.system_prompt
        self.checkpoints = {
            active_id: self.checkpoints[active_id] for active_id in checkpoint.checkpoint_ids
        }
        self.next_checkpoint_id = checkpoint.next_checkpoint_id
        return True

    def apply(self, record: dict[str, Any], *, line_number: int, fallback_timestamp: float) -> None:
        """Apply one valid object record, logging and skipping malformed state."""
        role = record.get("role")
        if not isinstance(role, str):
            logger.warning("Skipping context record without a role at line {line}", line=line_number)
            return

        if role == "_system_prompt":
            content = record.get("content")
            if isinstance(content, str):
                self.system_prompt = content
            else:
                logger.warning("Skipping invalid system prompt at line {line}", line=line_number)
            return

        if role == "_usage":
            token_count = record.get("token_count")
            message_count = record.get("message_count", len(self.entries))
            if (
                not isinstance(token_count, int)
                or isinstance(token_count, bool)
                or not isinstance(message_count, int)
                or isinstance(message_count, bool)
                or message_count < 0
            ):
                logger.warning("Skipping invalid usage record at line {line}", line=line_number)
                return
            self.usages.append(UsageSnapshot(token_count, message_count))
            return

        if role == "_checkpoint":
            checkpoint_id = record.get("id")
            message_count = record.get("message_count", len(self.entries))
            if (
                not isinstance(checkpoint_id, int)
                or isinstance(checkpoint_id, bool)
                or not isinstance(message_count, int)
                or isinstance(message_count, bool)
                or message_count < 0
            ):
                logger.warning("Skipping invalid checkpoint at line {line}", line=line_number)
                return
            self.checkpoints[checkpoint_id] = CheckpointState(
                message_count=message_count,
                usage_count=len(self.usages),
                system_prompt=self.system_prompt,
                checkpoint_ids=tuple(self.checkpoints),
                next_checkpoint_id=self.next_checkpoint_id,
            )
            self.next_checkpoint_id = max(self.next_checkpoint_id, checkpoint_id + 1)
            return

        if role == "_reset":
            self.reset()
            return

        if role == "_revert":
            checkpoint_id = record.get("id")
            if (
                not isinstance(checkpoint_id, int)
                or isinstance(checkpoint_id, bool)
                or not self.revert(checkpoint_id)
            ):
                logger.warning(
                    "Skipping revert to unknown checkpoint {id} at line {line}",
                    id=checkpoint_id,
                    line=line_number,
                )
            return

        if role.startswith("_"):
            # Unknown control records are forward-compatible and never become
            # visible messages.
            return

        created_at = record.get("created_at", fallback_timestamp)
        if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
            created_at = fallback_timestamp
        message_record = dict(record)
        message_record.pop("created_at", None)
        content = message_record.get("content")
        if isinstance(content, list):
            message_record["content"] = [
                {**part, "type": "text"}
                if isinstance(part, dict) and "text" in part and "type" not in part
                else part
                for part in content
            ]
        try:
            message = Message.model_validate(message_record)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "Skipping invalid context message at line {line}: {error}",
                line=line_number,
                error=exc,
            )
            return
        self.entries.append(ContextMessageEntry(message, float(created_at)))

    def export(self) -> ExportedContext:
        return ExportedContext(
            system_prompt=self.system_prompt,
            messages=self.messages,
            checkpoints=list(self.checkpoints),
            usages=[usage.token_count for usage in self.usages],
        )


def replay_context(path: Path) -> ContextReplayState:
    """Replay the active context view without constructing runtime storage."""
    state = ContextReplayState()
    if not path.exists():
        return state
    try:
        fallback_timestamp = path.stat().st_mtime
    except OSError:
        fallback_timestamp = 0.0
    for line_number, record in iter_jsonl_records(path):
        state.apply(
            record,
            line_number=line_number,
            fallback_timestamp=fallback_timestamp,
        )
    return state


def message_record(message: Message, *, created_at: float | None = None) -> dict[str, Any]:
    """Return the raw message-shaped dense-storage mapping with its timestamp."""
    record = message.model_dump(mode="json", exclude_none=True)
    record["created_at"] = time.time() if created_at is None else created_at
    return record


# ---------------------------------------------------------------------------
# JsonlContextStorage — sole runtime context backend
# ---------------------------------------------------------------------------


class JsonlContextStorage:
    """Append-only JSONL context with one cached replay state.

    Blocking file work runs through ``asyncio.to_thread``.  State changes and
    appends are serialized by a thread lock, and the retained binary handle has
    a real synchronous close for SDK and Windows cleanup.
    """

    def __init__(self, file_path: Path) -> None:
        self._file_path = file_path
        self._appender = JsonlAppender(file_path)
        self._lock = threading.Lock()
        self._state = ContextReplayState()
        self._loaded = False

    @property
    def storage_path(self) -> Path:
        return self._file_path

    def _initialize_locked(self) -> None:
        self._appender.open()
        if not self._loaded:
            self._state = replay_context(self._file_path)
            self._loaded = True

    def _initialize_sync(self) -> None:
        with self._lock:
            self._initialize_locked()

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    async def close(self) -> None:
        await asyncio.to_thread(self._appender.close)

    def close_sync(self) -> None:
        self._appender.close()

    async def get_system_prompt(self) -> str | None:
        return self._state.system_prompt

    async def set_system_prompt(self, content: str) -> None:
        def write() -> None:
            with self._lock:
                self._initialize_locked()
                self._appender.append({"role": "_system_prompt", "content": content})
                self._state.system_prompt = content

        await asyncio.to_thread(write)

    async def append_messages(self, messages: Sequence[Message]) -> None:
        if not messages:
            return

        def write() -> None:
            with self._lock:
                self._initialize_locked()
                now = time.time()
                records = [message_record(message, created_at=now) for message in messages]
                self._appender.append_many(records)
                self._state.entries.extend(
                    ContextMessageEntry(message, float(record["created_at"]))
                    for message, record in zip(messages, records, strict=True)
                )

        await asyncio.to_thread(write)

    async def get_messages(
        self, *, after_rowid: int = 0, limit: int | None = None
    ) -> list[Message]:
        start = max(after_rowid, 0)
        stop = None if limit is None else start + max(limit, 0)
        return [entry.message for entry in self._state.entries[start:stop]]

    async def get_messages_with_meta(
        self, *, after_rowid: int = 0, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return logical rowids and dense message JSON for server consumers."""
        start = max(after_rowid, 0)
        stop = None if limit is None else start + max(limit, 0)
        result: list[dict[str, Any]] = []
        for index, entry in enumerate(self._state.entries[start:stop], start=start + 1):
            result.append(
                {
                    "rowid": index,
                    "role": entry.message.role,
                    "content": orjson.dumps(
                        entry.message.model_dump(mode="json", exclude_none=True)
                    ).decode("utf-8"),
                    "created_at": entry.created_at,
                }
            )
        return result

    async def get_message_count(self) -> int:
        return len(self._state.entries)

    async def has_visible_messages(self) -> bool:
        return bool(self._state.entries)

    async def create_checkpoint(self, checkpoint_id: int) -> int:
        def write() -> None:
            with self._lock:
                self._initialize_locked()
                record = {
                    "role": "_checkpoint",
                    "id": checkpoint_id,
                    "message_count": len(self._state.entries),
                }
                self._appender.append(record)
                self._state.apply(
                    record,
                    line_number=0,
                    fallback_timestamp=time.time(),
                )

        await asyncio.to_thread(write)
        return len(self._state.entries)

    async def get_latest_checkpoint_id(self) -> int:
        return self._state.latest_checkpoint_id

    async def revert_to_checkpoint(self, checkpoint_id: int) -> None:
        def write() -> None:
            with self._lock:
                self._initialize_locked()
                if checkpoint_id not in self._state.checkpoints:
                    raise ValueError(f"Checkpoint {checkpoint_id} not found")
                record = {"role": "_revert", "id": checkpoint_id}
                self._appender.append(record)
                restored = self._state.revert(checkpoint_id)
                assert restored

        await asyncio.to_thread(write)

    async def record_usage(self, token_count: int) -> None:
        def write() -> None:
            with self._lock:
                self._initialize_locked()
                self._appender.append(
                    {
                        "role": "_usage",
                        "token_count": token_count,
                        "message_count": len(self._state.entries),
                    }
                )
                self._state.usages.append(
                    UsageSnapshot(token_count, len(self._state.entries))
                )

        await asyncio.to_thread(write)

    async def get_latest_usage(self) -> int | None:
        return self._state.latest_usage

    async def clear(self) -> None:
        def write() -> None:
            with self._lock:
                self._initialize_locked()
                self._appender.append({"role": "_reset"})
                self._state.reset()

        await asyncio.to_thread(write)

    async def export(self) -> ExportedContext:
        return self._state.export()

    async def restore_full(self) -> tuple[str | None, list[Message], int | None, int, list[Message]]:
        return (
            self._state.system_prompt,
            self._state.messages,
            self._state.latest_usage,
            self._state.next_checkpoint_id,
            self._state.pending_messages,
        )


# ---------------------------------------------------------------------------
# Context — the main orchestrator
# ---------------------------------------------------------------------------


class Context:
    """Conversation context manager over append-only JSONL storage."""

    def __init__(
        self,
        file_backend: Path | None = None,
        on_append: Callable[[Sequence[Message]], None] | None = None,
        model_name: str | None = None,
        storage: ContextStorage | None = None,
    ):
        if storage is not None:
            self._storage = storage
        elif file_backend is not None:
            self._storage = self._auto_create_storage(file_backend)
        else:
            raise ValueError("Either file_backend or storage must be provided")

        self._history: list[Message] = []
        self._token_count: int = 0
        self._pending_token_estimate: int = 0
        self._next_checkpoint_id: int = 0
        self._system_prompt: str | None = None
        self._on_append = on_append
        self._model_name = model_name

    @staticmethod
    def _auto_create_storage(path: Path) -> ContextStorage:
        if path.suffix == ".db":
            raise ValueError(
                f"Legacy SQLite context {path} must be migrated to context.jsonl "
                "before runtime use"
            )
        return JsonlContextStorage(path)

    @classmethod
    def from_file_backend(cls, path: Path, **kwargs: Any) -> Context:
        """Create a JSONL context; legacy ``.db`` paths require migration first."""
        return cls(file_backend=path, **kwargs)

    @property
    def storage(self) -> ContextStorage:
        return self._storage

    @property
    def history(self) -> Sequence[Message]:
        return self._history

    @property
    def token_count(self) -> int:
        return self._token_count

    @property
    def token_count_with_pending(self) -> int:
        return self._token_count + self._pending_token_estimate

    @property
    def n_checkpoints(self) -> int:
        return self._next_checkpoint_id

    @property
    def system_prompt(self) -> str | None:
        return self._system_prompt

    @property
    def file_backend(self) -> Path:
        if hasattr(self._storage, "storage_path"):
            return self._storage.storage_path
        return Path(".")

    @property
    def model_name(self) -> str | None:
        return self._model_name

    @model_name.setter
    def model_name(self, value: str | None) -> None:
        self._model_name = value

    async def restore(self) -> bool:
        """Restore the active view; return False for an empty/missing context."""
        logger.debug("Restoring context from storage: {storage}", storage=self.file_backend)
        if self._history:
            logger.error("The context storage is already modified")
            raise RuntimeError("The context storage is already modified")

        await self._storage.initialize()
        system_prompt, messages, latest_usage, next_checkpoint_id, pending = (
            await self._storage.restore_full()
        )
        if system_prompt is not None:
            self._system_prompt = system_prompt
        self._history.extend(messages)
        if latest_usage is not None:
            self._token_count = latest_usage
        if next_checkpoint_id > 0:
            self._next_checkpoint_id = next_checkpoint_id

        from kimi_cli.soul.message import strip_system_reminders

        strip_system_reminders(self._history)
        self._repair_tool_pairing_inplace()
        self._pending_token_estimate = estimate_text_tokens(pending, model=self._model_name)
        return len(self._history) > 0 or self._system_prompt is not None

    def _repair_tool_pairing_inplace(self) -> None:
        """Repair restored in-memory pairing without rewriting persisted bytes."""
        from kosong.history import repair_tool_message_pairing

        repaired = repair_tool_message_pairing(self._history)
        if repaired is not self._history:
            logger.warning(
                "Repaired tool-call pairing in restored context ({before} -> {after} messages)",
                before=len(self._history),
                after=len(repaired),
            )
            self._history = list(repaired)

    async def write_system_prompt(self, prompt: str) -> None:
        await self._storage.set_system_prompt(prompt)
        self._system_prompt = prompt

    async def append_message(self, message: Message | Sequence[Message]) -> None:
        logger.debug("Appending message(s) to context: {message}", message=message)
        messages = [message] if isinstance(message, Message) else message
        self._history.extend(messages)
        self._pending_token_estimate += estimate_text_tokens(messages, model=self._model_name)
        if self._on_append is not None:
            self._on_append(messages)
        await self._storage.append_messages(messages)

    async def replace_history(self, messages: Sequence[Message]) -> None:
        """Append a reset and the replacement active view; old bytes remain."""
        logger.debug("Replacing context history with {count} messages", count=len(messages))
        await self._storage.clear()
        if self._system_prompt is not None:
            await self._storage.set_system_prompt(self._system_prompt)
        if messages:
            await self._storage.append_messages(messages)
        self._history = list(messages)
        new_token_estimate = estimate_text_tokens(messages, model=self._model_name)
        if new_token_estimate < self._token_count:
            self._token_count = new_token_estimate
        self._pending_token_estimate = 0
        self._next_checkpoint_id = 0

    async def checkpoint(self, add_user_message: bool) -> None:
        checkpoint_id = self._next_checkpoint_id
        self._next_checkpoint_id += 1
        logger.debug("Checkpointing, ID: {id}", id=checkpoint_id)
        await self._storage.create_checkpoint(checkpoint_id)
        if add_user_message:
            await self.append_message(
                Message(role="user", content=[system(f"CHECKPOINT {checkpoint_id}")])
            )

    async def revert_to(self, checkpoint_id: int) -> None:
        logger.debug("Reverting checkpoint, ID: {id}", id=checkpoint_id)
        if checkpoint_id >= self._next_checkpoint_id:
            logger.error("Checkpoint {checkpoint_id} does not exist", checkpoint_id=checkpoint_id)
            raise ValueError(f"Checkpoint {checkpoint_id} does not exist")
        await self._storage.revert_to_checkpoint(checkpoint_id)

        self._history.clear()
        self._token_count = 0
        self._next_checkpoint_id = 0
        self._system_prompt = None
        system_prompt, messages, latest_usage, next_checkpoint_id, pending = (
            await self._storage.restore_full()
        )
        if system_prompt is not None:
            self._system_prompt = system_prompt
        self._history.extend(messages)
        if latest_usage is not None:
            self._token_count = latest_usage
        if next_checkpoint_id > 0:
            self._next_checkpoint_id = next_checkpoint_id
        self._repair_tool_pairing_inplace()
        self._pending_token_estimate = estimate_text_tokens(pending, model=self._model_name)

    async def update_token_count(self, token_count: int) -> None:
        logger.debug("Updating token count in context: {token_count}", token_count=token_count)
        self._token_count = token_count
        self._pending_token_estimate = 0
        await self._storage.record_usage(token_count)

    async def close(self) -> None:
        logger.debug("Closing context storage")
        await self._storage.close()

    def close_sync(self) -> None:
        """Synchronously flush and release the JSONL handle, best-effort."""
        close_sync = getattr(self._storage, "close_sync", None)
        if close_sync is not None:
            try:
                close_sync()
            except Exception:
                logger.debug("Failed to synchronously close context storage")

    async def clear(self) -> None:
        logger.debug("Clearing context")
        await self._storage.clear()
        self._history.clear()
        self._token_count = 0
        self._pending_token_estimate = 0
        self._next_checkpoint_id = 0
        self._system_prompt = None
