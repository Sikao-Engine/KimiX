"""Persist and retrieve structured work steps to survive context compaction.

Steps are stored under `.kimix_cache/steps/{session_id}.json` so they remain
accessible after `/compact` wipes the conversation history.
"""

from __future__ import annotations

import orjson
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, override
from kimi_cli.session import Session

from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.reason import ToolCallReason
from kimi_cli.utils.io import atomic_json_write


class Params(BaseModel):
    action: Literal["save", "load"] = Field(
        description='Action to perform: "save" to record a step, "load" to retrieve history.'
    )
    step: str | None = Field(
        default=None,
        description="Required for save: description of what was done. Optional for load: filter history to entries whose step text contains this value.",
    )
    result: str | None = Field(
        default=None,
        description="Optional for save: outcome summary (success/failure/output).",
    )
    files: str | list[str] | None = Field(
        default=None,
        description="Optional: for save, list of files involved in this step. For load, search ToolCallReason for these files.",
    )
    brief: str | None = Field(
        default=None,
        description="Optional for save: short title used for quick indexing after compaction.",
    )


class StepMemory(CallableTool2[Params]):
    name: str = "StepMemory"
    description: str = (
        "Persist and retrieve structured work steps. "
        "Call action='save' after completing each key step. "
        "Call action='load' after context compaction to recover full history. "
        "When loading, pass 'step' to filter history by step text, or 'files' to search tool call reasons."
    )
    params: type[Params] = Params

    _MAX_ENTRIES: int = 200
    _lock = threading.Lock()

    def __init__(self, runtime: Runtime, session: Session) -> None:
        super().__init__()
        self._runtime = runtime
        self._session = session
        if "tool_call_reason" not in session.custom_data:
            session.custom_data["tool_call_reason"] = ToolCallReason()

    def _storage_path(self) -> Path:
        session = self._runtime.session
        path = session.dir / "steps" / f"{session.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _load_steps(self) -> tuple[list[dict[str, Any]], str | None]:
        path = self._storage_path()
        if not path.exists():
            return [], None
        try:
            data = orjson.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data, None
        except (orjson.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
        display_path = str(path).replace("\\", "/")
        return [], f"Corrupted step memory file, using empty history: {display_path}"

    def _save_steps(self, steps: list[dict[str, Any]]) -> None:
        path = self._storage_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(steps, path)

    def _maybe_compact(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(steps) <= self._MAX_ENTRIES:
            return steps

        # Compact the oldest half, preserving brief for indexing.
        split = len(steps) // 2
        compacted: list[dict[str, Any]] = []
        for s in steps[:split]:
            compacted.append(
                {
                    "seq": s.get("seq"),
                    "time": s.get("time"),
                    "brief": s.get("brief"),
                    "step": "[compacted] " + (s.get("step", ""))[:100],
                    "result": "[compacted]",
                    "files": [],
                }
            )
        return compacted + steps[split:]

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        if params.action == "save":
            return await self._save(params)
        return await self._load(params)

    async def _save(self, params: Params) -> ToolReturnValue:
        if not params.step:
            return ToolError(
                message="Field 'step' is required when action='save'.",
                brief="Missing step description",
            )

        with self._lock:
            steps, warning = self._load_steps()
            seq = steps[-1].get("seq", 0) + 1 if steps else 1
            entry: dict[str, Any] = {
                "seq": seq,
                "time": datetime.now(timezone.utc).isoformat(),
                "brief": params.brief or params.step[:50],
                "step": params.step,
                "result": params.result or "",
                "files": ([params.files] if isinstance(params.files, str) else params.files) or [],
            }
            steps.append(entry)
            steps = self._maybe_compact(steps)
            self._save_steps(steps)

        brief_display = params.brief or params.step[:50]
        return ToolOk(
            output=f"Step #{seq} saved: {brief_display}",
            message=f"{warning}; step recorded" if warning else "Step recorded",
            brief=f"Saved step #{seq}: {brief_display}",
        )

    async def _load(self, params: Params) -> ToolReturnValue:
        with self._lock:
            steps, warning = self._load_steps()

        if params.step:
            steps = [s for s in steps if params.step in s.get("step", "")]

        parts: list[str] = []

        if steps:
            lines: list[str] = [f"Step history ({len(steps)} entries):"]
            for s in steps:
                seq = s.get("seq", "?")
                time = s.get("time", "?")
                brief = s.get("brief", "")
                step = s.get("step", "")
                result = s.get("result", "")
                files = s.get("files", [])
                files_str = f" | files: {', '.join(files)}" if files else ""
                lines.append(
                    f"#{seq} [{time}] {brief}\n"
                    f"  step: {step}\n"
                    f"  result: {result}{files_str}"
                )
            parts.append("\n\n".join(lines))

        files_list = [params.files] if isinstance(params.files, str) else params.files
        if files_list:
            tcr: ToolCallReason = self._session.custom_data.get(
                "tool_call_reason", ToolCallReason()
            )
            tool_reasons = tcr.formatted_print(files_list)
            if tool_reasons:
                parts.append(f"Tool call reasons for files:\n\n{tool_reasons}")

        if not parts:
            return ToolOk(
                output="No step history found.",
                message=warning or "Empty history",
                brief="No step history found",
            )

        msg_parts: list[str] = []
        if steps:
            msg_parts.append(f"Loaded {len(steps)} steps")
        if files_list:
            msg_parts.append(f"queried {len(files_list)} files")
        message = "; ".join(msg_parts) if msg_parts else (warning or "Done")

        return ToolOk(
            output="\n\n".join(parts),
            message=message,
            brief=message,
        )
