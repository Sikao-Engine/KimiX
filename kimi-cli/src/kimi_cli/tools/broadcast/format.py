"""Render broadcast messages into one steer-safe text block for the model.

The soul injects the rendered block as a single follow-up user message, so it
has to stay small no matter how much other agents published: the text is capped
by a token budget derived from the model's context window (and by a per-message
cap).  When the batch does not fit, the *newest* messages win and the older
ones are cut: they are summarised as "N earlier message(s) omitted" so the model
knows something preceded this block.  Cutting (rather than deferring) is what
keeps the block strictly bounded — a deferred tail would be re-read on every
tick, and since the renderer always prefers the newest messages it would starve
forever instead of ever shrinking.

Only the *whole batch* case is deferred: if not even the header fits into the
budget, :func:`format_broadcast_batch` returns ``None`` and the soul keeps its
read cursor where it is, retrying after compaction has freed context space.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence

from kimi_cli.tools.broadcast.bus import BroadcastMessage

#: Header that frames the block as an out-of-band notice rather than a prompt.
BATCH_HEADER = (
    "[broadcast] {count} message(s) from other agent processes in this work directory "
    "(out-of-band notice, not a user request; answer with the `broadcast` tool if needed):"
)
#: Appended when older messages in the batch did not fit the token budget.
OMITTED_NOTE = "[broadcast] ... {count} earlier message(s) omitted (token budget)"
#: Marker used when a single message is cut short.
ELLIPSIS = "…"
#: Tokens reserved for the truncation marker / omitted note.
TRUNCATION_RESERVE_TOKENS = 32
#: Hard token cap for a single message inside a batch.
PER_MESSAGE_MAX_TOKENS = 2000


def format_timestamp(ms: int) -> str:
    """Render a millisecond epoch key as a local ``YYYY-MM-DD HH:MM:SS`` stamp."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def truncate_to_tokens(
    text: str,
    max_tokens: int,
    count_tokens: Callable[[str], int],
    *,
    marker: str = ELLIPSIS,
) -> str:
    """Return the longest prefix of *text* that fits into *max_tokens*.

    The cut point is found with a binary search over characters, so an
    arbitrarily long message costs ``O(len(text) * log len(text))`` token
    estimates instead of a linear scan.
    """
    if max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = text[:middle] + marker
        if count_tokens(candidate) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    if low <= 0:
        return marker if count_tokens(marker) <= max_tokens else ""
    return text[:low].rstrip() + marker


def format_broadcast_batch(
    messages: Sequence[BroadcastMessage],
    *,
    token_budget: int,
    count_tokens: Callable[[str], int],
    per_message_max_tokens: int = PER_MESSAGE_MAX_TOKENS,
) -> str | None:
    """Render *messages* (oldest first) into one block within *token_budget*.

    Returns ``None`` when nothing fits — the caller then keeps its read cursor
    where it is and retries after the context has room again.
    """
    if not messages or token_budget <= 0:
        return None
    budget = token_budget - TRUNCATION_RESERVE_TOKENS
    header = BATCH_HEADER.format(count=len(messages))
    used = count_tokens(header) + 1
    if used >= budget:
        return None

    lines: list[str] = []
    omitted = 0
    # Newest first so the freshest coordination data survives a tight budget.
    for message in reversed(messages):
        line = f"[{format_timestamp(message.time)}] {message.content.strip()}"
        # ``- 1`` accounts for the newline that joins the line to the block, so
        # an exactly-fitting truncated line is accepted instead of dropped.
        room = max(1, budget - used - 1)
        # Fast path: a line no longer than the remaining budget cannot exceed it
        # in tokens (every tokenization of N chars is <= N tokens), so skip the
        # truncation search -- two estimates per line become one.
        if len(line) > room:
            line = truncate_to_tokens(line, min(per_message_max_tokens, room), count_tokens)
        if not line:
            omitted = len(messages) - len(lines)
            break
        cost = count_tokens(line) + 1
        if used + cost > budget:
            omitted = len(messages) - len(lines)
            break
        lines.append(line)
        used += cost

    if not lines:
        return None
    rendered = header + "\n" + "\n".join(reversed(lines))
    if omitted > 0:
        rendered += "\n" + OMITTED_NOTE.format(count=omitted)
    return rendered
