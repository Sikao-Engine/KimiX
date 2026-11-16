"""Tool output is bounded by the compaction reserve, at the source.

The compaction trigger reserves output budget + safety margin + free-space
floors. For that reservation to be a real guard, a single tool result must not
be able to outgrow it: the tool budget is therefore ``remaining - reserve``,
not a fraction of the remaining window. (A fraction shrinks as the context
fills, so it can never bound the growth it is supposed to bound.)
"""

from __future__ import annotations

from types import SimpleNamespace

from kimi_cli.config import LoopControl
from kimi_cli.soul.context_budget import compaction_threshold, context_reserve
from kimi_cli.soul.toolset import KimiToolset

WINDOW = 200_000
MAX_TOKENS = 64_000


def _toolset(*, max_tokens: int | None = MAX_TOKENS, reserved: int = 75_000) -> KimiToolset:
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            max_tokens=max_tokens,
            loop_control=LoopControl(reserved_context_size=reserved),
        )
    )
    return KimiToolset(runtime=runtime)


class TestToolOutputBudget:
    def test_reserve_matches_the_compaction_policy(self):
        ts = _toolset()
        expected = context_reserve(WINDOW, reserved_context_size=75_000, max_tokens=MAX_TOKENS)
        assert ts._context_reserve_tokens(WINDOW) == expected == 68_096

    def test_budget_is_capped_by_remaining_after_reserve(self):
        ts = _toolset()
        # 200K - 124K used - 68_096 reserve = 7_904 tokens left for the result.
        assert ts.estimate_tool_output_token_budget(WINDOW, 124_000) == 7_904

    def test_budget_never_exceeds_the_window(self):
        ts = _toolset()
        assert ts.estimate_tool_output_token_budget(WINDOW, 199_000) == 0
        assert ts.estimate_tool_output_token_budget(WINDOW, 250_000) == 0

    def test_budget_shrinks_as_the_context_fills(self):
        ts = _toolset()
        budgets = [
            ts.estimate_tool_output_token_budget(WINDOW, used)
            for used in (0, 50_000, 100_000, 124_000)
        ]
        assert budgets == [32_768, 32_768, 31_904, 7_904]

    def test_absolute_ceiling(self):
        ts = _toolset()
        # 128 KiB / 4 bytes-per-token, independent of the window size.
        assert ts.estimate_tool_output_token_budget(2_000_000, 0) == 32_768

    def test_result_plus_output_always_fits_below_the_threshold(self):
        """The invariant the reserve exists for."""
        ts = _toolset()
        limit = compaction_threshold(
            WINDOW,
            trigger_ratio=0.8,
            reserved_context_size=75_000,
            max_tokens=MAX_TOKENS,
        )
        for used in range(0, limit, 2_000):
            budget = ts.estimate_tool_output_token_budget(WINDOW, used)
            assert used + budget + MAX_TOKENS <= WINDOW
            assert used + budget <= WINDOW - ts._context_reserve_tokens(WINDOW)

    def test_bare_toolset_uses_the_safety_margin_only(self):
        """No runtime/config: fall back to the margin instead of reserving nothing."""
        ts = KimiToolset()
        # (32K - 20K used - 4096 margin) * 4 bytes / 4 bytes-per-token
        assert ts.estimate_tool_output_token_budget(32_768, 20_000) == 8_672

    def test_live_config_changes_the_budget(self):
        """A larger output budget reserves more, so tool output shrinks."""
        small = _toolset(max_tokens=8_000).estimate_tool_output_token_budget(WINDOW, 100_000)
        large = _toolset(max_tokens=128_000).estimate_tool_output_token_budget(WINDOW, 100_000)
        assert large < small
