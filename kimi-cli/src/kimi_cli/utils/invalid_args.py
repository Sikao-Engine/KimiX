from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import aiofiles
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class InvalidArgRecord(BaseModel):
    """A record of an invalid argument tool call stored in `invalid_arguments.jsonl`."""

    model_config = ConfigDict(strict=True)

    role: Literal["_invalid_arg"]
    """Literal tag to distinguish from other context.jsonl record types."""

    timestamp: float
    """Unix epoch seconds (from time.time())."""

    session_id: str
    """The active session ID."""

    tool_name: str
    """Name of the tool that received bad arguments."""

    tool_call_id: str
    """The tool call ID (from ToolCall.id)."""

    arguments: str
    """The raw JSON arguments string received from the LLM."""

    error_type: Literal["parse_error", "validate_error"]
    """Type of failure: JSON parse error or schema validation error."""

    error_message: str
    """The human-readable error message returned to the LLM."""

    turn_id: str | None = None
    """Current turn ID (optional, for traceability)."""

    step_no: int | None = None
    """Current step number (optional)."""


class InvalidArgsRecorder:
    """Appends invalid-argument tool-call records to a Markdown log.

    The target file lives under ``<work-dir>/.kimix_cache/log/`` and is named
    ``invalid_arguments.md``.
    """

    def __init__(self, work_dir: Path) -> None:
        """Initialise the recorder with the path to the work directory.

        Args:
            work_dir: Absolute path to the work directory.  The target output
                file is derived as ``<work-dir>/.kimix_cache/log/invalid_arguments.md``.
        """
        self._work_dir = work_dir

    # ── public API ──────────────────────────────────────────────────────────

    @property
    def target_path(self) -> Path:
        """Absolute path of the ``invalid_arguments.md`` output file."""
        return self._work_dir / ".kimix_cache" / "log" / "invalid_arguments.md"

    async def record(self, record: InvalidArgRecord) -> None:
        """Append *record* to the target Markdown file.

        The parent directory is created if it does not already exist.
        Exceptions other than :exc:`FileNotFoundError` are logged but not
        propagated so that a recording failure does not interrupt the main loop.
        """
        target = self.target_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            entry = self._format_record(record)
            async with aiofiles.open(target, "a", encoding="utf-8") as f:
                await f.write(entry)
        except FileNotFoundError:
            logger.warning(
                "Cannot write invalid-arguments record: parent directory of %s "
                "does not exist (work directory may have been deleted)",
                target,
            )
        except OSError:
            logger.exception("Failed to write invalid-arguments record to %s", target)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _format_record(self, record: InvalidArgRecord) -> str:
        """Render a single record as a Markdown section."""
        timestamp = datetime.fromtimestamp(record.timestamp, tz=timezone.utc)

        lines: list[str] = [
            f"## Invalid argument — {record.tool_name} ({record.error_type})",
            "",
            f"- **Timestamp (UTC):** {timestamp.isoformat()}",
            f"- **Session ID:** {record.session_id}",
            f"- **Tool Call ID:** {record.tool_call_id}",
        ]
        if record.turn_id is not None:
            lines.append(f"- **Turn ID:** {record.turn_id}")
        if record.step_no is not None:
            lines.append(f"- **Step No:** {record.step_no}")

        lines.extend(
            [
                "",
                "### Arguments",
                "",
                "```json",
                record.arguments,
                "```",
                "",
                "### Error message",
                "",
                record.error_message,
                "",
                "---",
                "",
            ]
        )

        # If this is the first entry, prepend a document title.
        if not self.target_path.exists() or self.target_path.stat().st_size == 0:
            lines.insert(0, "# Invalid arguments log")
            lines.insert(1, "")

        return "\n".join(lines)
