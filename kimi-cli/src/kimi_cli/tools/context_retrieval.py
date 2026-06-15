from __future__ import annotations

from typing import override

from kosong.tooling import CallableTool2, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimi_cli.soul.history_index import HistoryIndex


class Params(BaseModel):
    query: str = Field(description="Natural-language query to search past conversation turns.")
    k: int = Field(default=3, ge=1, le=10, description="Number of top matching turns to return.")


class ContextRetrieval(CallableTool2[Params]):
    name: str = "ContextRetrieval"
    description: str = (
        "Search archived conversation history for past turns matching a query. "
        "Returns verbatim excerpts from user/assistant exchanges that were compacted or rotated "
        "out of the active context window. Use to recall decisions, file paths, or error messages "
        "no longer visible in the current conversation."
    )
    params: type[Params] = Params

    def __init__(self, history_index: HistoryIndex) -> None:
        super().__init__()
        self._history_index = history_index

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        results = self._history_index.search(params.query, top_k=params.k)
        if not results:
            return ToolOk(
                output="No matching past turns found.",
                message="No results",
            )

        lines: list[str] = [f"Retrieved {len(results)} past turn(s):"]
        for r in results:
            role = r["role"]
            text = r["text"]
            score = r.get("score", 0.0)
            compacted_marker = " [compacted]" if r.get("is_compacted") else ""
            lines.append(
                f"> **{role}**{compacted_marker} (relevance: {score:.2f})\n"
                f"> {text.replace(chr(10), chr(10) + '> ')}"
            )

        return ToolOk(
            output="\n\n".join(lines),
            message=f"Found {len(results)} turn(s)",
        )
