"""Tests for cache-05 (compaction prompt cache poison).

See plans/05-compaction-prompt-cache-poison.md.
"""

from __future__ import annotations

from types import SimpleNamespace

from kimi_cli.soul.agent import Agent
from kimi_cli.soul.kimisoul import _compaction_export_missing


def _agent(system_prompt, cached: str | None = None) -> Agent:
    return Agent(
        name="test-agent",
        system_prompt=system_prompt,
        system_prompt_cached=cached,
        toolset=SimpleNamespace(),
        runtime=SimpleNamespace(
            builtin_args=SimpleNamespace(KIMI_WORK_DIR=None),
        ),
    )


# ======================================================================
# cache-05: Agent prompt cache slots
# ======================================================================


class TestAgentPromptSlots:
    def test_get_system_prompt_uses_cached_without_rerender(self):
        """When the persisted prompt is adopted (cache-04 §3.2), the callable
        must not be re-invoked."""
        calls: list[bool] = []

        def sp(runtime, is_compacting: bool = False):
            calls.append(is_compacting)
            return "rendered"

        agent = _agent(sp, cached="persisted-prompt")
        assert agent.get_system_prompt() == "persisted-prompt"
        assert calls == []

    def test_compacting_render_does_not_poison_cache(self):
        """cache-05 §3.1: a compacting render must never overwrite the normal
        cache slot; the normal render stays stable afterwards."""
        def sp(runtime, is_compacting: bool = False, compact_export_path=None):
            if is_compacting:
                return f"compacting:{compact_export_path}"
            return "normal-prompt"

        agent = _agent(sp)
        compacting = agent.get_system_prompt(is_compacting=True, compact_export_path="X")
        assert compacting == "compacting:X"
        assert agent.system_prompt_cached is None  # NOT cached
        assert agent.get_system_prompt() == "normal-prompt"  # cache not poisoned
        assert agent.get_system_prompt() == "normal-prompt"  # now cached
        # A later compacting render still re-renders and never touches the slot.
        assert agent.get_system_prompt(is_compacting=True, compact_export_path="Y") == "compacting:Y"
        assert agent.get_system_prompt() == "normal-prompt"

    def test_compacting_render_is_deterministic(self):
        """cache-05 §3.1: identical compacting args → identical strings."""
        def sp(runtime, is_compacting: bool = False, compact_export_path=None):
            return f"compact:{compact_export_path}"

        agent = _agent(sp)
        a = agent.get_system_prompt(is_compacting=True, compact_export_path="same.md")
        b = agent.get_system_prompt(is_compacting=True, compact_export_path="same.md")
        assert a == b

    def test_compacting_render_without_export_has_no_random_token(self):
        """cache-05 §3.1: with compact_export_path=None the render contains no
        random context_<hex> token."""
        def sp(runtime, is_compacting: bool = False, compact_export_path=None):
            return f"compact:{compact_export_path}"

        agent = _agent(sp)
        out = agent.get_system_prompt(is_compacting=True, compact_export_path=None)
        assert "context_" not in out


# ======================================================================
# cache-05 §3.3: resume guard for deleted compaction exports
# ======================================================================


class TestCompactionExportMissing:
    def test_existing_export_not_missing(self, tmp_path):
        export_dir = tmp_path / ".kimix_cache"
        export_dir.mkdir()
        (export_dir / "context_compacted.md").write_text("snapshot", encoding="utf-8")
        prompt = "Your context was exported to .kimix_cache/context_compacted.md"
        assert _compaction_export_missing(prompt, tmp_path) is False

    def test_deleted_export_detected(self, tmp_path):
        prompt = "Your context was exported to .kimix_cache/context_compacted.md"
        assert _compaction_export_missing(prompt, tmp_path) is True
        # Legacy random-nonce name (16 hex chars) also detected.
        assert _compaction_export_missing(
            "see .kimix_cache/context_abc1234567890def.md", tmp_path
        ) is True

    def test_no_export_path_reference(self, tmp_path):
        assert _compaction_export_missing("plain prompt", tmp_path) is False
        assert _compaction_export_missing("", tmp_path) is False
