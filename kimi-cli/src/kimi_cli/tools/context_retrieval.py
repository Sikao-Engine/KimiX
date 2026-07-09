from __future__ import annotations

from typing import override

from kosong.tooling import CallableTool2, ToolOk, ToolReturnValue
from pydantic import BaseModel, Field

from kimi_cli.soul.history_index import HistoryIndex


class Params(BaseModel):
    query: str = Field(default="", description="Natural-language query to search past conversation turns.")
    k: int = Field(default=3, ge=1, le=10, description="Number of top matching turns to return.")
    id: str | None = Field(default=None, description="Optional stable reference ID to retrieve a specific elided turn by ID.")


class ContextRetrieval(CallableTool2[Params]):
    name: str = "ContextRetrieval"
    description: str = (
        "Search archived conversation history for past turns matching a query. "
        "Returns verbatim excerpts from user/assistant exchanges that were compacted or rotated "
        "out of the active context window. Use to recall decisions, file paths, or error messages "
        "no longer visible in the current conversation. "
        "If an ``id`` is provided instead of a query, the exact turn with that reference ID is returned."
    )
    params: type[Params] = Params

    def __init__(self, history_index: HistoryIndex) -> None:
        super().__init__()
        self._history_index = history_index

    @override
    async def __call__(self, params: Params) -> ToolReturnValue:
        # If an explicit id is given, retrieve by reference
        if params.id is not None:
            turn = self._history_index.get_by_id(params.id)
            if turn is None:
                return ToolOk(
                    output=f"No turn found with id={params.id!r}.",
                    message="No results",
                )
            role = turn["role"]
            text = turn["text"]
            compacted_marker = " [compacted]" if turn.get("is_compacted") else ""
            return ToolOk(
                output=(
                    f"Retrieved turn id={params.id!r}:\n"
                    f"> **{role}**{compacted_marker}\n"
                    f"> {text.replace(chr(10), chr(10) + '> ')}"
                ),
                message=f"Found turn id={params.id!r}",
            )

        # Otherwise search by query
        if not params.query.strip():
            return ToolOk(
                output="No query provided. Pass a ``query`` string or an ``id``.",
                message="No query",
            )

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