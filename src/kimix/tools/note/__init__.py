"""Manage persistent notes for working memory."""

from pathlib import Path
from typing import Any, Literal

import anyio
from kimi_agent_sdk import CallableTool2, ToolError, ToolOk, ToolReturnValue
from kimi_cli.session import Session
from pydantic import BaseModel, Field
from kimi_cli.tools import SkipThisTool


class Params(BaseModel):
    content: str = Field(
        description="Note content.",
    )

import threading
_enable_note = threading.local()
class Note(CallableTool2):
    name: str = "Note"
    description: str = 'Append a note to a file.'
    params: type[Params] = Params

    def __init__(self, session: Session):
        super().__init__()
        self._session = session
        if getattr(_enable_note, 'value', None) != True:
            raise SkipThisTool()

    async def __call__(self, params: Params) -> ToolReturnValue:
        path: Path | None = self._session.custom_data.get('note_writing_path')
        if path is None:
            return ToolError(
                output="",
                message="Note tool invalid",
                brief="invalid tool.",
            )
        try:
            await anyio.to_thread.run_sync(lambda: path.parent.mkdir(parents=True, exist_ok=True))
            async with await anyio.open_file(path, 'a', encoding='utf-8') as f:
                await f.write(params.content)
            self._session.custom_data['note_called'] = True
            return ToolOk(output=f"Note appended to {path}")
        except Exception as exc:
            return ToolError(
                output="",
                message=str(exc),
                brief="Failed to append note",
            )
