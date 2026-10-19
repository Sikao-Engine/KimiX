"""History-level repair utilities shared by providers and the runtime.

These functions operate purely on :class:`~kosong.message.Message` sequences
and never mutate their inputs.
"""

from __future__ import annotations

from collections.abc import Sequence

from kosong.message import Message


def repair_tool_message_pairing(history: Sequence[Message]) -> Sequence[Message]:
    """Repair assistant tool_call / tool-result pairing for strict backends.

    Interrupting a step mid-flight (e.g. a steer/notification arriving while
    the step's context growth is being torn down, or a session restart) can
    persist histories like ``assistant(tool_calls) -> user -> tool`` or drop a
    tool result entirely.  Strict backends (Moonshot, OpenAI, Anthropic)
    reject such requests with a 400: "an assistant message with 'tool_calls'
    must be followed by tool messages responding to each 'tool_call_id'".

    This pass makes the history valid again:

    - tool results are moved to immediately follow the assistant message that
      requested them, ahead of any interleaved user/system messages;
    - assistant tool calls with no persisted result get a synthetic
      placeholder result noting the interruption;
    - orphan tool results (no matching call) and duplicate results for an
      already-satisfied call are dropped.

    Matching is by raw ``tool_call_id`` before any id normalization.
    Input messages are never mutated; when the history is already well-formed
    the original sequence object is returned unchanged.
    """
    # Index tool messages by call id (ordered, so duplicates can be detected).
    tools_by_id: dict[str, list[int]] = {}
    for index, message in enumerate(history):
        if message.role == "tool" and message.tool_call_id is not None:
            tools_by_id.setdefault(message.tool_call_id, []).append(index)

    if not tools_by_id and not any(m.tool_calls for m in history):
        return history

    claimed: set[int] = set()
    output: list[Message] = []
    for message in history:
        if message.role == "tool":
            # Tool results are emitted via their claiming assistant message;
            # any left unclaimed (orphans/duplicates) are dropped.
            continue
        output.append(message)
        if not message.tool_calls:
            continue
        for tool_call in message.tool_calls:
            indices = tools_by_id.get(tool_call.id)
            tool_index = indices.pop(0) if indices else None
            if tool_index is None:
                # Result was never persisted (interrupted step): synthesize a
                # placeholder so strict backends accept the history.
                output.append(
                    Message(
                        role="tool",
                        content=(
                            f"[Tool result missing: the call to "
                            f"'{tool_call.function.name}' was interrupted before "
                            f"its result was recorded. Treat the call as failed.]"
                        ),
                        tool_call_id=tool_call.id,
                    )
                )
                continue
            claimed.add(tool_index)
            output.append(history[tool_index])

    # No repair needed when the rebuilt sequence is identical (by identity)
    # to the input — messages are never mutated, only reordered/replaced.
    if len(output) == len(history) and all(o is h for o, h in zip(output, history)):
        return history
    return output
