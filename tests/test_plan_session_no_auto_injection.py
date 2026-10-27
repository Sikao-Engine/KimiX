"""Tests: the plan session is built hardened, and auto-retrieved memory is gone.

Background: the planner agent runs with a "Plan only; never implement."
system prompt. Two mechanisms used to leak foreign content into sessions as
fake user messages:

- ``KimiSoul._maybe_auto_retrieve_history`` auto-injected archived past turns
  (e.g. an earlier "ok, begin to implement") retrieved from the history index.
- ``KimiSoul`` notification delivery appends pending notifications to any
  root, non-read-only runtime.

The auto-retrieval mechanism has since been **removed entirely** from
``kimi_cli`` (method, LoopControl fields, and ``HistoryIndex.search_with_recency``);
this file keeps a removal guard so it cannot come back. The notification
delivery gate (``read_only``) remains, and every planner-session creation site
must route through the shared helpers:

- ``build_planner_provider_dict`` — planner sub-provider selection + fallback
- ``lock_planner_session_read_only`` — blocks notification delivery into the
  plan session

Coverage:
- builder selects the planner sub-provider, falls back to the main provider,
  deep-copies (never mutates the shared provider dicts)
- read-only lock sets ``runtime.read_only`` and tolerates missing pieces
- removal guard: no auto-retrieve method / config fields / index search API
- drift guard: every planner creation site uses the shared helpers
- end-to-end: ``prompt_plan_async`` locks the planner session read-only
"""

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import kimix.base as base

# NB: ``import kimix.utils.prompt as prompt_mod`` would bind the *function*
# ``prompt`` re-exported by kimix.utils.__init__ (it shadows the submodule
# attribute); importlib reliably returns the module object.
prompt_mod = importlib.import_module("kimix.utils.prompt")
from kimix.utils.prompt import (
    build_planner_provider_dict,
    lock_planner_session_read_only,
    prompt_plan_async,
)

REQUIREMENT = "Add --max-distance and --ao-gamma to the AO baker."


@pytest.fixture()
def provider_env(monkeypatch):
    """Main provider + a planner sub-provider."""
    main_provider = {
        "type": "test",
        "model": "main-model",
        "url": "http://main.example.com",
        "max_context_size": 128000,
        "loop_control": {"context_pruning_enabled": True},
    }
    planner_provider = {
        "type": "test",
        "model": "planner-model",
        "url": "http://planner.example.com",
        "max_context_size": 128000,
        "role": "planner",
        "loop_control": {"context_pruning_enabled": True},
    }
    monkeypatch.setattr(base, "_default_provider", main_provider)
    monkeypatch.setattr(base, "_default_sub_providers", [dict(planner_provider)])
    return main_provider, planner_provider


class TestBuildPlannerProviderDict:
    def test_uses_planner_sub_provider_when_configured(self, provider_env):
        _, planner_provider = provider_env
        built = build_planner_provider_dict()
        assert built.get("model") == planner_provider["model"]

    def test_falls_back_to_main_provider_without_sub_providers(
        self, provider_env, monkeypatch
    ):
        monkeypatch.setattr(base, "get_default_sub_provider", lambda role: None)
        main_provider, _ = provider_env
        built = build_planner_provider_dict()
        assert built.get("model") == main_provider["model"]

    def test_does_not_mutate_shared_provider_dicts(self, provider_env):
        main_provider, planner_provider = provider_env
        planner_before = dict(planner_provider["loop_control"])
        main_before = dict(main_provider["loop_control"])
        built = build_planner_provider_dict()
        # Mutating the built dict must not touch the shared ones.
        built["loop_control"]["context_pruning_enabled"] = False
        assert planner_provider["loop_control"] == planner_before
        assert main_provider["loop_control"] == main_before

    def test_tolerates_empty_provider(self, monkeypatch):
        monkeypatch.setattr(base, "get_default_sub_provider", lambda role: None)
        monkeypatch.setattr(base, "_default_provider", None)
        built = build_planner_provider_dict()
        assert built == {}


class TestLockPlannerSessionReadOnly:
    def test_sets_read_only_flag(self):
        runtime = SimpleNamespace(read_only=False)
        session = SimpleNamespace(_cli=SimpleNamespace(_runtime=runtime))
        lock_planner_session_read_only(session)
        assert runtime.read_only is True

    def test_tolerates_missing_cli_or_runtime(self):
        # No _cli at all — must not raise.
        lock_planner_session_read_only(SimpleNamespace())
        # _cli present but no _runtime — must not raise.
        lock_planner_session_read_only(SimpleNamespace(_cli=SimpleNamespace()))


class TestAutoRetrieveRemovalGuard:
    """Auto-retrieved memory must stay removed from every session type."""

    def test_no_auto_retrieve_method_on_soul(self):
        from kimi_cli.soul.kimisoul import KimiSoul

        assert not hasattr(KimiSoul, "_maybe_auto_retrieve_history")

    def test_no_auto_retrieve_fields_in_loop_control(self):
        from kimi_cli.config import LoopControl

        for name in LoopControl.model_fields:
            assert not name.startswith("auto_retrieve"), (
                f"LoopControl still has auto-retrieve field {name!r}"
            )

    def test_no_search_with_recency_on_history_index(self):
        from kimi_cli.soul.history_index import HistoryIndex

        assert not hasattr(HistoryIndex, "search_with_recency")


class _FakePlannerSession:
    """Minimal stand-in for the planner Session used by prompt_plan_async."""

    def __init__(self, plan_file: Path):
        self._plan_file = plan_file
        self._cli = SimpleNamespace(_runtime=SimpleNamespace(read_only=False))
        self._cancel_event = None

    def get_custom_data(self) -> dict:
        return {}

    def cancel(self) -> None:
        pass

    async def prompt(self, text: str, **kwargs):
        self._plan_file.write_text("# Plan\n\nstep one\n", encoding="utf-8")
        return
        yield  # pragma: no cover — makes this an async generator, yields nothing


class TestPromptPlanAsyncHardening:
    @pytest.mark.asyncio
    async def test_planner_session_is_locked_read_only(self, monkeypatch, tmp_path):
        """End-to-end: prompt_plan_async must build the planner from the
        shared provider builder and lock it read-only (blocks notification
        delivery into the plan session)."""
        plan_file = tmp_path / "plan.md"
        fake = _FakePlannerSession(plan_file)

        create_mock = AsyncMock(return_value=fake)
        captured_kwargs = {}

        async def _spy_create_session_async(*args, **kwargs):
            captured_kwargs.update(kwargs)
            return await create_mock(*args, **kwargs)

        monkeypatch.setattr(prompt_mod, "_create_session_async", _spy_create_session_async)
        monkeypatch.setattr(prompt_mod, "close_session_async", AsyncMock())
        monkeypatch.setattr(base, "_stream", MagicMock())

        # Review loop: "n" declines implementation, "/quit" exits the loop.
        answers = iter(["n", "/quit"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        # _open_plan_file side effects must never touch the real system.
        import os as _os

        monkeypatch.setattr(_os, "startfile", lambda *a: None, raising=False)
        monkeypatch.setattr(prompt_mod.subprocess, "run", lambda *a, **k: None)

        monkeypatch.setattr(base, "get_default_sub_provider", lambda role: None)
        monkeypatch.setattr(
            base,
            "_default_provider",
            {
                "type": "test",
                "model": "main-model",
                "url": "http://main.example.com",
                "max_context_size": 128000,
            },
        )

        from kimix.utils import _globals as session_globals

        monkeypatch.setattr(session_globals, "_default_session", None, raising=False)

        await prompt_plan_async(REQUIREMENT, plan_file)

        # The planner session was created with a provider dict.
        provider_dict = captured_kwargs.get("provider_dict")
        assert provider_dict is not None, "planner session created without provider_dict"
        assert provider_dict.get("model") == "main-model"

        # The planner session is locked read-only (blocks notification delivery).
        assert fake._cli._runtime.read_only is True


class TestCallSiteDriftGuard:
    """Every planner-session creation site must use the shared helpers."""

    SITES = [
        "src/kimix/utils/prompt.py",
        "src/kimix/server/session_manager.py",
        "src/kimix/server/dummy_session_manager.py",
    ]

    def test_planner_sites_use_shared_helpers(self):
        root = Path(__file__).resolve().parents[1]
        for rel in self.SITES[1:]:
            src = (root / rel).read_text(encoding="utf-8")
            assert "build_planner_provider_dict" in src, (
                f"{rel} bypasses build_planner_provider_dict"
            )
            assert "lock_planner_session_read_only" in src, (
                f"{rel} bypasses lock_planner_session_read_only"
            )

    def test_helpers_exported_from_kimix_utils(self):
        import kimix.utils as kimix_utils

        assert kimix_utils.build_planner_provider_dict is build_planner_provider_dict
        assert kimix_utils.lock_planner_session_read_only is lock_planner_session_read_only
