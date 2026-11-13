"""``broadcast`` tool: publish a message to the other agents in this work dir.

The tool itself only *writes*: the message lands in the shared SQLite bus
(:mod:`kimi_cli.tools.broadcast.bus`) and every other agent process running in
the same work directory picks it up on its own poll tick and steers it into its
context (see ``KimiSoul._broadcast_poll_once``).
"""

from __future__ import annotations

from pathlib import Path

import anyio
from pydantic import BaseModel, Field

from kimi_cli.soul.agent import Runtime
from kimi_cli.tools.broadcast.bus import (
    MAX_CONTENT_CHARS,
    BroadcastBus,
    BroadcastError,
    remember_local_publish,
)
from kimi_cli.tools.prompt_common import accepts_alias_text
from kosong.tooling import CallableTool2, ToolError, ToolOk, ToolReturnValue

__all__ = ["Broadcast", "BroadcastParams"]


class BroadcastParams(BaseModel):
    """Parameters of the ``broadcast`` tool."""

    model_config = {"populate_by_name": True}

    content: str = Field(
        alias="text",  # common LLM variant (mirrors WriteFile / write_plan)
        min_length=1,
        max_length=MAX_CONTENT_CHARS,
        description=(
            "Message to publish to the other agent processes. Keep it self-contained: "
            "recipients only see this text, not your conversation. "
            + accepts_alias_text("content", "text")
        ),
    )


class Broadcast(CallableTool2):
    """Publish one message on the work directory's shared broadcast bus."""

    name: str = "broadcast"
    description: str = (
        "Publish a message to the other agent processes running in this work directory. "
        "The text is stored in the shared `.kimix_cache/_broadcast.db` bus and every other "
        "running agent in this directory receives it within a few seconds as an out-of-band "
        "steer message in its own context. Use it to share findings, claim files, or hand "
        "work to a sibling agent. Your own session does not re-read what you published."
    )
    params: type[BroadcastParams] = BroadcastParams

    def __init__(self, runtime: Runtime):
        super().__init__()
        self._runtime = runtime
        work_dir = Path(str(runtime.session.work_dir))
        self._bus = BroadcastBus(work_dir=work_dir)
        # Sharing the bus with the soul poller of the same process would echo
        # this agent's own messages back into its context, so the key is
        # remembered process-wide and skipped by the readers.
        self._work_dir = work_dir

    async def __call__(self, params: BroadcastParams) -> ToolReturnValue:
        content = params.content.strip()
        if not content:
            return ToolError(
                output="",
                message="Invalid: broadcast content is empty.",
                brief="invalid content",
            )
        try:
            # SQLite I/O must not block the event loop: a busy database can
            # wait up to the bus timeout, and writers fsync.
            message = await anyio.to_thread.run_sync(self._bus.append, content)
        except BroadcastError as exc:
            return ToolError(
                output="",
                message=f"Failed to broadcast: {exc}",
                brief="broadcast failed",
            )
        remember_local_publish(message.time, self._bus.path)
        return ToolOk(
            output=(
                f"Broadcast #{message.time} published ({len(content)} chars) to "
                f"{self._bus.path}. Other agents in this work directory will receive it "
                f"on their next poll (within a few seconds)."
            )
        )
