"""Context-budget policy: how much of the window may be used before compaction.

One concern per knob:

* the **output reserve** is a hard constraint. A request must satisfy
  ``input + max_output + margin <= window`` or the provider rejects it. It is
  derived from the live output budget, never configured directly.
* the **safety margin** absorbs estimation error (the count is provider usage
  plus a ``tiktoken``/heuristic estimate of what was appended since), so it
  scales with the window: a fixed 4K is 2 % of 200K but 0.4 % of 1M.
* the **free-space floor** keeps a proportional slice of the window unused
  (``MIN_FREE_CONTEXT_RATIO``): running a session on fumes makes every step a
  coin flip on tokenizer drift.
* ``reserved_context_size`` is a *policy* request for free space. It is an
  absolute number, so it is clamped to ``MAX_POLICY_FREE_RATIO`` of the window
  — 75K is 37 % of a 200K window but 7 % of a 1M one, and an unclamped floor
  silently makes the ratio threshold inert on large windows.
* ``compaction_trigger_ratio`` is the policy ceiling on usage.

The trigger is ``min(ratio * window, window - reserve)`` and the reserve is
**usage-independent**: a reserve computed from the remaining window (the old
per-tool buffer term) shrinks as the context fills, so it can never act as a
guard. Growth is bounded at the source instead — tool output is truncated to
what fits after the reserve (see ``KimiToolset``), and any request is checked
again at the next step.
"""

from __future__ import annotations

import math

SAFETY_MARGIN_TOKENS: int = 4096
"""Absolute floor for the safety margin (tokens)."""

SAFETY_MARGIN_RATIO: float = 0.02
"""Safety margin as a fraction of the window (tokenizer/estimate drift scales)."""

MIN_FREE_CONTEXT_RATIO: float = 0.05
"""Never plan to run with less than this fraction of the window free."""

MAX_POLICY_FREE_RATIO: float = 0.25
"""A configured ``reserved_context_size`` cannot claim more than this fraction."""


def safety_margin_for_window(max_context_size: int) -> int:
    """Return the safety margin for *max_context_size* (absolute floor or ratio)."""
    window = max(0, max_context_size)
    return max(SAFETY_MARGIN_TOKENS, math.ceil(SAFETY_MARGIN_RATIO * window))


def resolve_max_output_tokens(max_tokens: int | None, max_context_size: int) -> int:
    """Resolve the output budget used by the context reservation.

    ``None`` means "no budget configured, use the provider default". A provider
    default is *not* zero, so this falls back to the same convention
    :class:`LLMModel` uses when deriving an unset output budget: a quarter of
    the context window. Callers that can read the live provider budget should
    pass it instead of ``None`` (see
    :func:`kimi_cli.llm.effective_max_output_tokens`).

    An explicit ``0`` is preserved: it means "no output reservation" and is
    only useful for diagnostics/tests that pin the rest of the formula.
    """
    if max_tokens is not None:
        return max(0, max_tokens)
    return max(0, max_context_size // 4)


def context_reserve(
    max_context_size: int,
    *,
    reserved_context_size: int,
    max_tokens: int | None = None,
    safety_margin_tokens: int | None = None,
    min_free_context_ratio: float = MIN_FREE_CONTEXT_RATIO,
    max_policy_free_ratio: float = MAX_POLICY_FREE_RATIO,
) -> int:
    """Return how many tokens must stay free before the next request.

    ``max(output reserve, clamped policy floor, free-space floor)``, never more
    than the window.

    ``safety_margin_tokens=None`` derives the margin from the window via
    :func:`safety_margin_for_window`; pass a number to pin it (tests).
    """
    window = max(0, max_context_size)
    if window <= 0:
        return 0
    margin = (
        safety_margin_tokens
        if safety_margin_tokens is not None
        else safety_margin_for_window(window)
    )
    output_reserve = resolve_max_output_tokens(max_tokens, window) + margin
    min_free = math.ceil(max(0.0, min_free_context_ratio) * window)
    policy_free = min(
        max(0, reserved_context_size),
        math.ceil(max(0.0, max_policy_free_ratio) * window),
    )
    if output_reserve <= window:
        return min(window, max(output_reserve, policy_free, min_free))
    # The configured output budget cannot fit the window at all: no reservation
    # can make such a request legal, and reserving the whole window would
    # compact on every single step. Keep the input floor instead.
    return min(window, max(min_free, min(policy_free, window - min_free)))


def compaction_threshold(
    max_context_size: int,
    *,
    trigger_ratio: float,
    reserved_context_size: int,
    max_tokens: int | None = None,
    safety_margin_tokens: int | None = None,
    min_free_context_ratio: float = MIN_FREE_CONTEXT_RATIO,
    max_policy_free_ratio: float = MAX_POLICY_FREE_RATIO,
) -> int:
    """Return the smallest token count at which compaction must run.

    ``min(ratio * window, window - reserve)``: the ratio is the policy ceiling,
    the reserve is the hard constraint, and whichever is earlier wins.
    """
    window = max(0, max_context_size)
    if window <= 0:
        return 0
    reserve = context_reserve(
        window,
        reserved_context_size=reserved_context_size,
        max_tokens=max_tokens,
        safety_margin_tokens=safety_margin_tokens,
        min_free_context_ratio=min_free_context_ratio,
        max_policy_free_ratio=max_policy_free_ratio,
    )
    return math.floor(min(window * trigger_ratio, window - reserve))


def output_budget_is_wasteful(max_tokens: int | None, max_context_size: int) -> bool:
    """True when the output budget reserves more than half of the window.

    Such a configuration is legal but gives the conversation less than half the
    window; a smaller ``max_tokens`` would use the context far better.
    """
    window = max(0, max_context_size)
    if window <= 0:
        return False
    return resolve_max_output_tokens(max_tokens, window) * 2 > window
