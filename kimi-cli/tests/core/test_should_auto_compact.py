"""Policy and invariants for the auto-compaction trigger.

The trigger must never report "safe" while the next request's input plus its
maximum output cannot fit the window: reporting "safe" there defers the failure
to the provider (a context-length 400) instead of compacting first.

The reserve is also *usage-independent*: a reservation derived from the
remaining window shrinks as the context fills and therefore cannot act as a
guard. Tool-result growth is bounded where it is produced instead (see
``KimiToolset``), and re-checked at the next step.
"""

from __future__ import annotations

import inspect
import math

import pytest

from kimi_cli.soul.compaction import should_auto_compact
from kimi_cli.soul.context_budget import (
    MAX_POLICY_FREE_RATIO,
    MIN_FREE_CONTEXT_RATIO,
    SAFETY_MARGIN_RATIO,
    SAFETY_MARGIN_TOKENS,
    compaction_threshold,
    context_reserve,
    output_budget_is_wasteful,
    resolve_max_output_tokens,
    safety_margin_for_window,
)

DEFAULT_RATIO = 0.8
DEFAULT_RESERVED = 75_000


def _fires(
    token_count: int,
    max_context_size: int,
    max_tokens: int | None,
    *,
    reserved_context_size: int = DEFAULT_RESERVED,
    trigger_ratio: float = DEFAULT_RATIO,
    safety_margin_tokens: int | None = None,
) -> bool:
    return should_auto_compact(
        token_count,
        max_context_size,
        trigger_ratio=trigger_ratio,
        reserved_context_size=reserved_context_size,
        max_tokens=max_tokens,
        safety_margin_tokens=safety_margin_tokens,
    )


class TestSafetyMargin:
    def test_absolute_floor(self):
        assert SAFETY_MARGIN_TOKENS == 4096
        assert safety_margin_for_window(0) == SAFETY_MARGIN_TOKENS
        # 2% of 100K is 2000, below the floor.
        assert safety_margin_for_window(100_000) == SAFETY_MARGIN_TOKENS

    def test_scales_with_the_window(self):
        assert SAFETY_MARGIN_RATIO == 0.02
        assert safety_margin_for_window(1_000_000) == 20_000
        assert safety_margin_for_window(1_048_576) == math.ceil(0.02 * 1_048_576)

    def test_should_auto_compact_derives_it_by_default(self):
        default = inspect.signature(should_auto_compact).parameters["safety_margin_tokens"].default
        assert default is None


class TestResolveMaxOutputTokens:
    def test_none_means_quarter_of_the_window(self):
        """An unset budget is *not* zero: a provider default still consumes window."""
        assert resolve_max_output_tokens(None, 200_000) == 50_000
        assert resolve_max_output_tokens(None, 32_768) == 8_192
        assert resolve_max_output_tokens(None, 0) == 0

    def test_explicit_budget_is_kept(self):
        assert resolve_max_output_tokens(64_000, 200_000) == 64_000

    def test_explicit_zero_means_no_output_reservation(self):
        assert resolve_max_output_tokens(0, 200_000) == 0

    def test_negative_budget_is_clamped(self):
        assert resolve_max_output_tokens(-5, 200_000) == 0


class TestContextReserve:
    def test_output_budget_dominates(self):
        """200K window, 64K output: 64K + 4096 must stay free."""
        assert (
            context_reserve(200_000, reserved_context_size=DEFAULT_RESERVED, max_tokens=64_000)
            == 68_096
        )

    def test_policy_floor_is_clamped_to_the_window(self):
        """75K is 37% of a 200K window: clamp to a quarter so the ratio stays live."""
        assert MAX_POLICY_FREE_RATIO == 0.25
        assert context_reserve(200_000, reserved_context_size=75_000, max_tokens=0) == 50_000
        assert context_reserve(1_000_000, reserved_context_size=75_000, max_tokens=0) == 75_000

    def test_free_space_floor_scales(self):
        assert MIN_FREE_CONTEXT_RATIO == 0.05
        assert (
            context_reserve(200_000, reserved_context_size=0, max_tokens=0, safety_margin_tokens=0)
            == 10_000
        )
        assert (
            context_reserve(
                1_000_000, reserved_context_size=0, max_tokens=0, safety_margin_tokens=0
            )
            == 50_000
        )

    def test_never_exceeds_the_window(self):
        for window in (16_384, 32_768, 65_536, 200_000, 1_000_000):
            reserve = context_reserve(
                window, reserved_context_size=10_000_000, max_tokens=10_000_000
            )
            assert 0 <= reserve <= window

    def test_infeasible_output_budget_keeps_the_input_floor(self):
        """384K output on a 100K window: no reservation can make that legal."""
        assert context_reserve(100_000, reserved_context_size=75_000, max_tokens=384_000) == 25_000

    def test_zero_window(self):
        assert context_reserve(0, reserved_context_size=75_000, max_tokens=64_000) == 0


class TestThreshold:
    def test_ratio_ceiling(self):
        """Large window, small output budget: the ratio is the binding knob."""
        assert (
            compaction_threshold(
                1_000_000, trigger_ratio=0.8, reserved_context_size=50_000, max_tokens=8_000
            )
            == 800_000
        )

    def test_reserve_ceiling(self):
        """200K window, 64K output: the reserve, not the 80% ratio, binds."""
        limit = compaction_threshold(
            200_000,
            trigger_ratio=0.8,
            reserved_context_size=DEFAULT_RESERVED,
            max_tokens=64_000,
        )
        assert limit == 131_904
        assert limit < int(200_000 * DEFAULT_RATIO)

    def test_whichever_is_earlier(self):
        for max_tokens in (0, 8_000, 32_000, 64_000, 128_000):
            reserve = context_reserve(
                200_000, reserved_context_size=DEFAULT_RESERVED, max_tokens=max_tokens
            )
            expected = math.floor(min(200_000 * DEFAULT_RATIO, 200_000 - reserve))
            assert (
                compaction_threshold(
                    200_000,
                    trigger_ratio=DEFAULT_RATIO,
                    reserved_context_size=DEFAULT_RESERVED,
                    max_tokens=max_tokens,
                )
                == expected
            )

    def test_should_auto_compact_matches_the_threshold(self):
        limit = compaction_threshold(
            200_000,
            trigger_ratio=DEFAULT_RATIO,
            reserved_context_size=DEFAULT_RESERVED,
            max_tokens=64_000,
        )
        assert _fires(limit, 200_000, 64_000)
        assert not _fires(limit - 1, 200_000, 64_000)

    def test_zero_window_never_reserves(self):
        assert (
            compaction_threshold(
                0, trigger_ratio=DEFAULT_RATIO, reserved_context_size=DEFAULT_RESERVED
            )
            == 0
        )


class TestOutputHeadroomInvariant:
    """Whenever the trigger does *not* fire, input + max output fits the window."""

    @pytest.mark.parametrize(
        ("max_context_size", "max_tokens"),
        [
            (200_000, 64_000),  # hosted 200K preset
            (1_000_000, 384_000),  # hosted 1M preset
            (1_048_576, 65_536),  # hosted 1M preset with a small output
            (200_000, 128_000),  # explicit output budget > reserve
            (65_536, 16_384),  # local 64K model (window < 75K floor)
            (32_768, 8_192),  # local 32K model (window < 75K floor)
            (74_000, 18_500),  # window just below the configured floor
        ],
    )
    def test_no_false_negative(self, max_context_size, max_tokens):
        step = max(1, max_context_size // 500)
        for token_count in range(0, max_context_size, step):
            if not _fires(token_count, max_context_size, max_tokens):
                assert token_count + max_tokens <= max_context_size, (
                    f"trigger said safe at {token_count} tokens in a "
                    f"{max_context_size}-token window with a {max_tokens}-token "
                    "output budget"
                )

    @pytest.mark.parametrize(
        ("max_context_size", "max_tokens", "expected_limit"),
        [
            # Regression: the reserve used to collapse to zero for windows
            # smaller than ``reserved_context_size``, so the trigger degraded to
            # the ratio and the request that followed did not fit its output.
            (65_536, 16_384, 65_536 - (16_384 + SAFETY_MARGIN_TOKENS)),
            (32_768, 8_192, 32_768 - (8_192 + SAFETY_MARGIN_TOKENS)),
            (74_000, 18_500, 74_000 - (18_500 + SAFETY_MARGIN_TOKENS)),
            (200_000, 128_000, 200_000 - (128_000 + SAFETY_MARGIN_TOKENS)),
        ],
    )
    def test_boundary_reserves_the_full_output_budget(
        self, max_context_size, max_tokens, expected_limit
    ):
        limit = compaction_threshold(
            max_context_size,
            trigger_ratio=DEFAULT_RATIO,
            reserved_context_size=DEFAULT_RESERVED,
            max_tokens=max_tokens,
        )
        assert limit == expected_limit
        assert limit + max_tokens <= max_context_size
        assert not _fires(limit - 1, max_context_size, max_tokens)

    def test_reserve_does_not_depend_on_usage(self):
        """The same threshold applies at every usage level (no feedback loop)."""
        window, max_tokens = 200_000, 64_000
        limit = compaction_threshold(
            window,
            trigger_ratio=DEFAULT_RATIO,
            reserved_context_size=DEFAULT_RESERVED,
            max_tokens=max_tokens,
        )
        for token_count in range(0, window, 5_000):
            assert _fires(token_count, window, max_tokens) == (token_count >= limit)

    def test_infeasible_output_budget_keeps_the_input_floor(self):
        """A budget larger than the window cannot be reserved; the floor applies."""
        limit = compaction_threshold(
            100_000,
            trigger_ratio=DEFAULT_RATIO,
            reserved_context_size=DEFAULT_RESERVED,
            max_tokens=384_000,
        )
        assert limit == 75_000


class TestOutputBudgetIsWasteful:
    def test_pathological_budget(self):
        assert output_budget_is_wasteful(384_000, 200_000) is True

    def test_reasonable_budget(self):
        assert output_budget_is_wasteful(64_000, 200_000) is False

    def test_unset_budget_is_a_quarter(self):
        # window/4 is exactly 25% -> not wasteful (strictly more than half only)
        assert output_budget_is_wasteful(None, 200_000) is False

    def test_zero_window(self):
        assert output_budget_is_wasteful(64_000, 0) is False
