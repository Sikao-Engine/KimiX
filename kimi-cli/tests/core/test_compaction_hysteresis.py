"""Auto-compaction hysteresis: no blind retries after a compaction that failed.

A compaction is expensive (a full summarisation call) and invalidates the prompt
cache. When it does not bring the context below the threshold — or does not cut
it by at least 10% — repeating the same call every step is pure waste. The soul
instead: escalates once (aggressive mode, shallow preserve depth, no cascade),
waits ``compaction_cooldown_steps`` between attempts, suppresses the auto path
after ``compaction_max_attempts_without_progress`` attempts, and re-arms as soon
as the context falls back below the threshold (compaction/pruning/`/clear`).
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import kimi_cli.soul.kimisoul as kimisoul_module
from kimi_cli.config import LoopControl
from kimi_cli.soul.compaction import CompactMode
from kimi_cli.soul.kimisoul import KimiSoul
from kimi_cli.soul.context_budget import compaction_threshold

WINDOW = 200_000
MAX_TOKENS = 64_000
THRESHOLD = 131_904  # 200K window, 64K output, 75K policy floor


def _soul(loop_control: LoopControl | None = None) -> KimiSoul:
    soul = object.__new__(KimiSoul)
    soul._loop_control = loop_control or LoopControl()
    soul._compaction_no_progress = 0
    soul._last_auto_compaction_step = -1
    soul._compaction_suppressed_logged = False
    soul._output_budget_warned = False
    return soul


def _soul_with_llm(
    *,
    window: int = WINDOW,
    max_tokens: int | None = MAX_TOKENS,
    reserved: int = 75_000,
    ratio: float = 0.8,
) -> KimiSoul:
    soul = _soul(
        LoopControl(
            reserved_context_size=reserved,
            compaction_trigger_ratio=ratio,
        )
    )
    provider = SimpleNamespace(
        name="kimi",
        model_name="fake-model",
        _generation_kwargs={"max_tokens": max_tokens} if max_tokens is not None else {},
    )
    soul._runtime = SimpleNamespace(
        llm=SimpleNamespace(chat_provider=provider, max_context_size=window),
        config=SimpleNamespace(max_tokens=max_tokens),
    )
    return soul


class TestCompactionThreshold:
    def test_uses_the_window_output_budget_and_floor(self):
        assert _soul_with_llm()._compaction_threshold() == THRESHOLD

    def test_matches_the_shared_policy(self):
        soul = _soul_with_llm()
        assert soul._compaction_threshold() == compaction_threshold(
            WINDOW, trigger_ratio=0.8, reserved_context_size=75_000, max_tokens=MAX_TOKENS
        )

    def test_without_an_llm_it_never_triggers(self):
        soul = _soul()
        soul._runtime = SimpleNamespace(llm=None, config=SimpleNamespace(max_tokens=64_000))
        assert soul._compaction_threshold() == sys.maxsize


class TestAutoCompactionOptions:
    def test_first_attempt_is_plain(self):
        options = _soul()._auto_compaction_options()
        assert options.avoid_cascade is False
        assert options.mode is CompactMode.BALANCED
        assert options.preserve_depth_override is None


class TestProgressDetection:
    def test_below_the_threshold_is_progress(self):
        soul = _soul()
        soul._record_auto_compaction(step_no=4, before=140_000, after=60_000, threshold=THRESHOLD)
        assert soul._compaction_no_progress == 0

    def test_ten_percent_reduction_is_progress(self):
        """Still above the threshold, but 25% smaller: worth another attempt."""
        soul = _soul()
        soul._record_auto_compaction(step_no=4, before=200_000, after=150_000, threshold=THRESHOLD)
        assert soul._compaction_no_progress == 0

    def test_no_reduction_is_no_progress(self):
        soul = _soul()
        soul._record_auto_compaction(step_no=4, before=140_000, after=139_000, threshold=THRESHOLD)
        assert soul._compaction_no_progress == 1

    def test_streak_accumulates(self):
        soul = _soul()
        for after in (139_000, 138_900, 138_800):
            soul._record_auto_compaction(
                step_no=4, before=140_000, after=after, threshold=THRESHOLD
            )
        assert soul._compaction_no_progress == 3

    def test_progress_resets_the_streak(self):
        soul = _soul()
        soul._compaction_no_progress = 2
        soul._record_auto_compaction(step_no=9, before=140_000, after=50_000, threshold=THRESHOLD)
        assert soul._compaction_no_progress == 0


class TestCooldownAndSuppression:
    def test_first_attempt_is_allowed(self):
        assert _soul()._auto_compaction_allowed(step_no=1) is True

    def test_cooldown_blocks_an_immediate_retry(self):
        soul = _soul(
            LoopControl(compaction_cooldown_steps=3, compaction_max_attempts_without_progress=2)
        )
        soul._record_auto_compaction(step_no=10, before=140_000, after=139_000, threshold=THRESHOLD)
        assert soul._auto_compaction_allowed(step_no=11) is False
        assert soul._auto_compaction_allowed(step_no=12) is False
        assert soul._auto_compaction_allowed(step_no=13) is True

    def test_suppressed_after_the_attempt_budget(self):
        soul = _soul(
            LoopControl(compaction_cooldown_steps=0, compaction_max_attempts_without_progress=2)
        )
        soul._compaction_no_progress = 2
        assert soul._auto_compaction_allowed(step_no=999) is False

    def test_reset_rearms_the_auto_path(self):
        soul = _soul()
        soul._compaction_no_progress = 2
        soul._compaction_suppressed_logged = True
        soul._reset_compaction_progress()
        assert soul._compaction_no_progress == 0
        # A new suppression episode logs again.
        assert soul._compaction_suppressed_logged is False
        assert soul._auto_compaction_allowed(step_no=5) is True


class TestEscalation:
    def test_escalates_after_one_failed_attempt(self):
        soul = _soul()
        soul._compaction_no_progress = 1
        options = soul._auto_compaction_options()
        assert options.avoid_cascade is True
        assert options.mode is CompactMode.AGGRESSIVE
        assert options.preserve_depth_override == 1

    def test_escalation_does_not_override_configured_todo_limit(self):
        soul = _soul()
        soul._compaction_no_progress = 1
        assert soul._auto_compaction_options().todos_max_items is None


class TestOutputBudgetWarning:
    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
        calls: list[tuple[str, dict]] = []

        class _Logger:
            def warning(self, message, **kwargs):
                calls.append((message, kwargs))

            def debug(self, message, **kwargs):
                pass

        monkeypatch.setattr(kimisoul_module, "logger", _Logger())
        return calls

    def test_warns_once_for_a_pathological_budget(self, monkeypatch):
        calls = self._capture(monkeypatch)
        soul = _soul_with_llm(max_tokens=384_000)
        soul._maybe_warn_output_budget()
        assert soul._output_budget_warned is True
        assert len(calls) == 1
        assert calls[0][1]["max_tokens"] == 384_000
        # A second call must not repeat the warning.
        soul._maybe_warn_output_budget()
        assert len(calls) == 1

    def test_quiet_for_a_reasonable_budget(self, monkeypatch):
        calls = self._capture(monkeypatch)
        soul = _soul_with_llm(max_tokens=64_000)
        soul._maybe_warn_output_budget()
        assert soul._output_budget_warned is True
        assert calls == []

    def test_quiet_without_an_llm(self, monkeypatch):
        calls = self._capture(monkeypatch)
        soul = _soul()
        soul._runtime = SimpleNamespace(llm=None, config=SimpleNamespace(max_tokens=64_000))
        soul._maybe_warn_output_budget()
        assert calls == []
