from __future__ import annotations

import asyncio
import builtins
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiofiles
import json
from kaos.path import KaosPath
from kosong.message import Message

from kimi_cli.file_mtine import FileMTime
from kimi_cli.metadata import WorkDirMeta, load_metadata, save_metadata
from kimi_cli.session_state import SessionState, load_session_state, save_session_state
from kimi_cli.soul.context_records import ExportedContext
from kosong.utils.jsonx import loads_relaxed
from kimi_cli.utils.logging import logger
from kimi_cli.utils.string import shorten
from kimi_cli.wire.file import WireFile
from kimi_cli.wire.types import TurnBegin
import secrets


@dataclass(slots=True, kw_only=True)
class Session:
    """A session of a work directory."""

    # static metadata
    id: str
    """The session ID."""
    work_dir: KaosPath
    """The absolute path of the work directory."""
    work_dir_meta: WorkDirMeta
    """The metadata of the work directory."""
    context_file: Path
    """The absolute path to the file storing the message history."""
    wire_file: WireFile
    """The wire message log file wrapper."""

    # session state
    state: SessionState
    """Persisted session state (approval settings, plan mode, workspace scope, etc.)."""

    # refreshable metadata
    title: str
    """The title of the session."""
    updated_at: float
    """The timestamp of the last update to the session."""

    custom_data: dict[str, Any]
    
    custom_config: dict[str, Any]

    file_mtime: FileMTime = field(default_factory=FileMTime)
    @property
    def dir(self) -> Path:
        """The absolute path of the session directory."""
        path = self.work_dir_meta.sessions_dir / self.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def subagents_dir(self) -> Path:
        """The absolute path of the subagent instances directory."""
        path = self.dir / "subagents"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def is_empty(self) -> bool:
        """Whether the session has any context history or a custom title."""
        if self.state.custom_title:
            return False
        if not self.wire_file.is_empty():
            return False
        try:
            with self.context_file.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    role = loads_relaxed(line).get("role")
                    if isinstance(role, str) and not role.startswith("_"):
                        return False
        except FileNotFoundError:
            return True
        except (OSError, ValueError, TypeError):
            logger.exception("Failed to read context file {file}:", file=self.context_file)
            return False
        return True

    def save_state(self) -> None:
        """Persist the session state to disk.

        Reloads externally-mutable fields (title, archive) from disk first
        to avoid overwriting concurrent changes made by the web API.
        """
        fresh = load_session_state(self.dir)
        self.state.custom_title = fresh.custom_title
        self.state.title_generated = fresh.title_generated
        self.state.title_generate_attempts = fresh.title_generate_attempts
        self.state.archived = fresh.archived
        self.state.archived_at = fresh.archived_at
        self.state.auto_archive_exempt = fresh.auto_archive_exempt
        save_session_state(self.state, self.dir)

    async def delete(self) -> None:
        """Delete the session directory."""
        session_dir = self.work_dir_meta.sessions_dir / self.id
        if not session_dir.exists():
            return
        await asyncio.to_thread(shutil.rmtree, session_dir, True)
        
    def delete_sync(self) -> None:
        """Delete the session directory."""
        session_dir = self.work_dir_meta.sessions_dir / self.id
        if not session_dir.exists():
            return
        shutil.rmtree(session_dir, True)

    async def refresh(self) -> None:
        self.title = "Untitled"
        self.updated_at = self.context_file.stat().st_mtime if self.context_file.exists() else 0.0

        if self.state.custom_title:
            self.title = self.state.custom_title
            return

        try:
            async for record in self.wire_file.iter_records():
                wire_msg = record.to_wire_message()
                if isinstance(wire_msg, TurnBegin):
                    self.title = shorten(
                        Message(role="user", content=wire_msg.user_input).extract_text(" "),
                        width=50,
                    )
                    return
        except Exception:
            logger.exception(
                "Failed to derive session title from wire file {file}:",
                file=self.wire_file.path,
            )

    async def export(self) -> ExportedContext:
        """Export all data from the session's context.jsonl file.

        Returns:
            ExportedContext: Structured representation of the context file.
        """
        from pydantic import ValidationError

        from kimi_cli.soul.context_records import (
            CheckpointRecord,
            ExportedContext,
            SystemPromptRecord,
            UsageRecord,
        )

        result = ExportedContext()

        if not self.context_file.exists() or self.context_file.stat().st_size == 0:
            return result

        async with aiofiles.open(self.context_file, encoding="utf-8", errors="replace") as f:
            async for line in f:
                if not line.strip():
                    continue
                try:
                    line_json = loads_relaxed(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(line_json, dict):
                    continue

                role = line_json.get("role")
                if not isinstance(role, str):
                    continue
                if role == "_system_prompt":
                    try:
                        record = SystemPromptRecord.model_validate(line_json)
                        result.system_prompt = record.content
                    except ValidationError:
                        continue
                elif role == "_usage":
                    try:
                        record = UsageRecord.model_validate(line_json)
                        result.usages.append(record.token_count)
                    except ValidationError:
                        continue
                elif role == "_checkpoint":
                    try:
                        record = CheckpointRecord.model_validate(line_json)
                        result.checkpoints.append(record.id)
                    except ValidationError:
                        continue
                else:
                    try:
                        message = Message.model_validate(line_json)
                        result.messages.append(message)
                    except ValidationError:
                        continue

        return result

    @staticmethod
    async def create(
        work_dir: KaosPath,
        session_id: str | None = None,
        _context_file: Path | None = None,
    ) -> Session:
        """Create a new session for a work directory."""
        work_dir = work_dir.canonical()
        logger.debug("Creating new session for work directory: {work_dir}", work_dir=work_dir)

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            work_dir_meta = metadata.new_work_dir_meta(work_dir)

        if session_id is None:
            session_id = uuid.uuid4().hex
        session_dir = work_dir_meta.sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        if _context_file is None:
            context_file = session_dir / "context.jsonl"
        else:
            logger.warning(
                "Using provided context file: {context_file}", context_file=_context_file
            )
            _context_file.parent.mkdir(parents=True, exist_ok=True)
            if _context_file.exists():
                assert _context_file.is_file()
            context_file = _context_file

        if context_file.exists():
            # truncate if exists
            logger.warning(
                "Context file already exists, truncating: {context_file}", context_file=context_file
            )
            context_file.unlink()
        context_file.touch()

        save_metadata(metadata)

        session = Session(
            id=session_id,
            work_dir=work_dir,
            work_dir_meta=work_dir_meta,
            context_file=context_file,
            wire_file=WireFile(path=session_dir / "wire.jsonl"),
            state=SessionState(),
            title="",
            updated_at=0.0,
            custom_data={},
            custom_config={},
        )
        await session.refresh()
        return session

    @staticmethod
    async def find(work_dir: KaosPath, session_id: str) -> Session | None:
        """Find a session by work directory and session ID."""
        work_dir = work_dir.canonical()
        logger.debug(
            "Finding session for work directory: {work_dir}, session ID: {session_id}",
            work_dir=work_dir,
            session_id=session_id,
        )

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            logger.debug("Work directory never been used")
            return None

        _migrate_session_context_file(work_dir_meta, session_id)

        session_dir = work_dir_meta.sessions_dir / session_id
        if not session_dir.is_dir():
            logger.debug("Session directory not found: {session_dir}", session_dir=session_dir)
            return None

        context_file = session_dir / "context.jsonl"
        if not context_file.exists():
            logger.debug(
                "Session context file not found: {context_file}", context_file=context_file
            )
            return None

        session = Session(
            id=session_id,
            work_dir=work_dir,
            work_dir_meta=work_dir_meta,
            context_file=context_file,
            wire_file=WireFile(path=session_dir / "wire.jsonl"),
            state=load_session_state(session_dir),
            title="",
            updated_at=0.0,
            custom_data={},
            custom_config={},
        )
        await session.refresh()
        return session

    @staticmethod
    async def list(work_dir: KaosPath) -> builtins.list[Session]:
        """List all sessions for a work directory."""
        work_dir = work_dir.canonical()
        logger.debug("Listing sessions for work directory: {work_dir}", work_dir=work_dir)

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            logger.debug("Work directory never been used")
            return []

        session_ids = {
            path.name if path.is_dir() else path.stem
            for path in work_dir_meta.sessions_dir.iterdir()
            if path.is_dir() or path.suffix == ".jsonl"
        }

        sessions: list[Session] = []
        for session_id in session_ids:
            _migrate_session_context_file(work_dir_meta, session_id)
            session_dir = work_dir_meta.sessions_dir / session_id
            if not session_dir.is_dir():
                logger.debug("Session directory not found: {session_dir}", session_dir=session_dir)
                continue
            context_file = session_dir / "context.jsonl"
            if not context_file.exists():
                logger.debug(
                    "Session context file not found: {context_file}", context_file=context_file
                )
                continue
            session = Session(
                id=session_id,
                work_dir=work_dir,
                work_dir_meta=work_dir_meta,
                context_file=context_file,
                wire_file=WireFile(path=session_dir / "wire.jsonl"),
                state=load_session_state(session_dir),
                title="",
                updated_at=0.0,
                custom_data={},
                custom_config={},
            )
            if session.is_empty():
                logger.debug(
                    "Session context file is empty: {context_file}", context_file=context_file
                )
                continue
            await session.refresh()
            sessions.append(session)
        sessions.sort(key=lambda session: session.updated_at, reverse=True)
        return sessions

    @classmethod
    async def list_all(cls) -> builtins.list[Session]:
        """List sessions across all known work directories."""
        all_sessions: list[Session] = []
        for wd in load_metadata().work_dirs:
            sessions = await cls.list(KaosPath.unsafe_from_local_path(Path(wd.path)))
            all_sessions.extend(sessions)
        all_sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return all_sessions

    @staticmethod
    async def continue_(work_dir: KaosPath) -> Session | None:
        """Get the last session for a work directory."""
        work_dir = work_dir.canonical()
        logger.debug("Continuing session for work directory: {work_dir}", work_dir=work_dir)

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            logger.debug("Work directory never been used")
            return None
        if work_dir_meta.last_session_id is None:
            logger.debug("Work directory never had a session")
            return None

        logger.debug(
            "Found last session for work directory: {session_id}",
            session_id=work_dir_meta.last_session_id,
        )
        return await Session.find(work_dir, work_dir_meta.last_session_id)

    @staticmethod
    async def rename(
        work_dir: KaosPath, session_id: str, new_session_id: str
    ) -> Session | None:
        """Rename a session to a new session ID.

        Args:
            work_dir: Working directory containing the session.
            session_id: The current session ID to rename.
            new_session_id: The new session ID.

        Returns:
            Session | None: The renamed session, or None if the session
                was not found or the new session ID already exists.
        """
        work_dir = work_dir.canonical()
        logger.debug(
            "Renaming session for work directory: {work_dir}, "
            "session ID: {session_id} to {new_session_id}",
            work_dir=work_dir,
            session_id=session_id,
            new_session_id=new_session_id,
        )

        metadata = load_metadata()
        work_dir_meta = metadata.get_work_dir_meta(work_dir)
        if work_dir_meta is None:
            logger.debug("Work directory never been used")
            return None

        old_session_dir = work_dir_meta.sessions_dir / session_id
        if not old_session_dir.is_dir():
            logger.debug(
                "Session directory not found: {session_dir}",
                session_dir=old_session_dir,
            )
            return None

        old_context_file = old_session_dir / "context.jsonl"
        if not old_context_file.exists():
            logger.debug(
                "Session context file not found: {context_file}",
                context_file=old_context_file,
            )
            return None

        new_session_dir = work_dir_meta.sessions_dir / new_session_id
        if new_session_dir.exists():
            logger.debug(
                "Target session directory already exists: {session_dir}",
                session_dir=new_session_dir,
            )
            return None

        old_session_dir.rename(new_session_dir)

        if work_dir_meta.last_session_id == session_id:
            work_dir_meta.last_session_id = new_session_id
            save_metadata(metadata)

        return await Session.find(work_dir, new_session_id)


def _migrate_session_context_file(work_dir_meta: WorkDirMeta, session_id: str) -> None:
    old_context_file = work_dir_meta.sessions_dir / f"{session_id}.jsonl"
    new_context_file = work_dir_meta.sessions_dir / session_id / "context.jsonl"
    if old_context_file.exists() and not new_context_file.exists():
        new_context_file.parent.mkdir(parents=True, exist_ok=True)
        old_context_file.rename(new_context_file)
        logger.info(
            "Migrated session context file from {old} to {new}",
            old=old_context_file,
            new=new_context_file,
        )
