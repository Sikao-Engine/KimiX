"""Tests for the "weaker sub-provider" warnings.

A sub-provider entry (``sub_provider`` / ``sub_providers`` with role
``sub_agent`` in the provider config) may point at a cheaper model than the
main agent -- less knowledge, weaker reasoning, more likely to fail.  Two
prompts must carry that fact, and only when the providers really differ:

* the ``subagent`` tool description shown to the *parent*, so the parent packs
  the missing knowledge into the task and reviews the child's work;
* the child's own system prompt, so it gives up on tasks beyond it instead of
  guessing, and reports what the parent has to verify.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import kimi_cli.tools.subagent as agent_module
import pytest
from kaos.path import KaosPath
from kimi_cli.tools.subagent import Agent, SubAgentParams, _weaker_provider

import kimix.base as base
from kimix.utils.config import (
    is_different_provider,
    provider_identity,
    sub_provider_differs,
)
from kimix.utils.session import _create_session_async
from kimix.utils.system_prompt import SystemPromptType, get_system_prompt

MAIN_PROVIDER: dict[str, Any] = {
    "type": "openai",
    "model": "kimi-k2",
    "url": "https://api.example.com/v1/",
    "max_context_size": 200_000,
}

SUB_PROVIDER: dict[str, Any] = {
    "type": "openai",
    "model": "qwen-flash",
    "url": "https://api.example.com/v1",
    "max_context_size": 32_000,
    "role": "sub_agent",
}

# One marker line from each clause family, so the assertions do not depend on
# the full wording of the prompts.
GIVE_UP_MARKER = "give up early"
REVIEW_MARKER = "tell the parent agent to review"
TOOL_NOTE_MARKER = "different, weaker model"
TOOL_REVIEW_MARKER = "always review and verify their output"


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch):
    """Install the main/sub provider pair on ``kimix.base`` for one test."""

    def _install(
        main: dict[str, Any] | None = MAIN_PROVIDER,
        subs: list[dict[str, Any]] | None = None,
    ) -> None:
        monkeypatch.setattr(base, "_default_provider", main)
        monkeypatch.setattr(base, "_default_sub_providers", list(subs or []))

    return _install


@pytest.fixture(autouse=True)
def _clean_agent_registries():
    """Keep the subagent tool's process-wide registries from leaking."""

    def _reset() -> None:
        agent_module._agent_entries.clear()
        agent_module._agent_sessions.clear()
        agent_module._children_by_parent.clear()
        agent_module._child_parent.clear()

    _reset()
    yield
    _reset()


def _parent_session(work_dir: Path, **custom_config: Any) -> SimpleNamespace:
    """Minimal parent session: an id, a work dir and a custom config dict."""
    return SimpleNamespace(
        id="parent-1",
        work_dir=KaosPath(str(work_dir)),
        custom_config={"chat_provider": None, **custom_config},
        custom_data={},
    )


def _prompt(work_dir: Path, role: SystemPromptType, weaker_provider: bool) -> str:
    prompt_func = get_system_prompt(
        work_dir=work_dir,
        agent_role=role,
        max_system_prompt_tokens=10_000,
        weaker_provider=weaker_provider,
    )
    runtime = SimpleNamespace(
        builtin_args=SimpleNamespace(
            KIMI_WORK_DIR=work_dir,
            KIMI_SKILLS="",
            KIMI_OS="Windows",
        ),
        session=SimpleNamespace(dir=work_dir, id="test-session", custom_data={}),
    )
    return prompt_func(runtime)


# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------


class TestProviderIdentity:
    def test_identity_normalizes_endpoint_and_type(self):
        """Trailing slashes and case must not fake a different provider."""
        assert provider_identity(MAIN_PROVIDER) == (
            "openai",
            "kimi-k2",
            "https://api.example.com/v1",
        )
        assert provider_identity(
            {"type": "OpenAI", "model": "kimi-k2", "base_url": "https://API.example.com/v1//"}
        ) == ("openai", "kimi-k2", "https://api.example.com/v1")

    def test_unknown_entries_are_never_different(self):
        """A missing entry yields empties rather than raising."""
        assert provider_identity(None) == ("", "", "")
        assert provider_identity("not-a-dict") == ("", "", "")
        assert not is_different_provider(None, {})

    def test_same_model_and_endpoint_is_the_same_provider(self):
        assert not is_different_provider(MAIN_PROVIDER, dict(SUB_PROVIDER, model="kimi-k2"))

    def test_a_different_model_or_endpoint_is_a_different_provider(self):
        assert is_different_provider(MAIN_PROVIDER, SUB_PROVIDER)
        assert is_different_provider(
            MAIN_PROVIDER, dict(SUB_PROVIDER, model="kimi-k2", url="https://other.example.com/v1")
        )


class TestSubProviderDiffers:
    def test_no_sub_provider_configured(self, providers):
        providers(subs=None)
        assert sub_provider_differs("sub_agent") is False

    def test_same_provider_is_not_reported(self, providers):
        providers(subs=[dict(SUB_PROVIDER, model="kimi-k2")])
        assert sub_provider_differs("sub_agent") is False

    def test_different_provider_is_reported(self, providers):
        providers(subs=[SUB_PROVIDER])
        assert sub_provider_differs("sub_agent") is True

    def test_only_the_requested_role_is_compared(self, providers):
        providers(subs=[dict(SUB_PROVIDER, role="researcher")])
        assert sub_provider_differs("sub_agent") is False
        assert sub_provider_differs("researcher") is True

    def test_explicit_main_provider_wins(self, providers):
        providers(subs=[SUB_PROVIDER])
        assert sub_provider_differs("sub_agent", MAIN_PROVIDER) is True
        assert sub_provider_differs("sub_agent", dict(SUB_PROVIDER, role=None)) is False

    def test_unknown_main_provider_is_not_a_comparison(self, providers):
        providers(main=None, subs=[SUB_PROVIDER])
        assert sub_provider_differs("sub_agent") is False


# ---------------------------------------------------------------------------
# The child's system prompt
# ---------------------------------------------------------------------------


class TestChildSystemPrompt:
    @pytest.mark.parametrize(
        "role", (SystemPromptType.TrivialSubAgent, SystemPromptType.Reader)
    )
    def test_weaker_provider_warns_to_give_up_and_ask_for_review(
        self, tmp_path: Path, role: SystemPromptType
    ):
        prompt = _prompt(tmp_path, role, weaker_provider=True)
        assert GIVE_UP_MARKER in prompt
        assert REVIEW_MARKER in prompt

    @pytest.mark.parametrize(
        "role", (SystemPromptType.TrivialSubAgent, SystemPromptType.Reader)
    )
    def test_same_provider_keeps_the_prompt_unchanged(
        self, tmp_path: Path, role: SystemPromptType
    ):
        prompt = _prompt(tmp_path, role, weaker_provider=False)
        assert GIVE_UP_MARKER not in prompt
        assert REVIEW_MARKER not in prompt

    def test_main_agent_roles_never_get_the_child_clause(self, tmp_path: Path):
        """The flag marks a *delegated child*; the parent must not be told to give up."""
        for role in (SystemPromptType.Worker, SystemPromptType.Thinker):
            prompt = _prompt(tmp_path, role, weaker_provider=True)
            assert GIVE_UP_MARKER not in prompt

    async def test_session_creation_passes_the_flag_to_the_prompt(self, monkeypatch):
        """``_create_session_async`` must forward ``weaker_provider`` verbatim."""
        assert "weaker_provider" in inspect.signature(_create_session_async).parameters
        seen: dict[str, Any] = {}

        def _factory(**kwargs: Any) -> Any:
            seen.update(kwargs)
            return lambda runtime: "prompt"

        monkeypatch.setattr("kimix.utils.session.get_system_prompt", _factory)
        monkeypatch.setattr(
            "kimix.utils.session.Session",
            SimpleNamespace(
                resume=AsyncMock(return_value=None),
                create=AsyncMock(
                    return_value=SimpleNamespace(get_custom_config=lambda: {})
                ),
            ),
        )
        monkeypatch.setattr(
            "kimix.utils.session._globals._track_session", lambda *a, **k: None
        )

        await _create_session_async(
            session_id="s-1",
            work_dir=KaosPath("."),
            agent_type=SystemPromptType.TrivialSubAgent,
            weaker_provider=True,
        )
        assert seen["weaker_provider"] is True
        assert seen["agent_role"] is SystemPromptType.TrivialSubAgent


# ---------------------------------------------------------------------------
# The parent's tool description
# ---------------------------------------------------------------------------


class TestToolDescription:
    def test_no_note_when_the_child_uses_the_same_model(self, tmp_path: Path, providers):
        providers(subs=[dict(SUB_PROVIDER, model="kimi-k2")])
        tool = Agent(_parent_session(tmp_path))
        assert TOOL_NOTE_MARKER not in tool.base.description

    def test_note_when_the_child_uses_a_different_model(self, tmp_path: Path, providers):
        providers(subs=[SUB_PROVIDER])
        tool = Agent(_parent_session(tmp_path))
        description = tool.base.description
        assert TOOL_NOTE_MARKER in description
        assert TOOL_REVIEW_MARKER in description
        # Appended once, however often the definition is read.
        assert description.count(TOOL_NOTE_MARKER) == 1
        assert tool.base.description == description

    def test_note_explains_the_gaps_the_parent_must_cover(self, tmp_path: Path, providers):
        providers(subs=[SUB_PROVIDER])
        description = Agent(_parent_session(tmp_path)).base.description
        assert "know less" in description
        assert "prompt must carry" in description

    def test_explicit_chat_provider_is_the_same_model(self, tmp_path: Path, providers):
        """A reused ``chat_provider`` object overrides any provider dict."""
        providers(subs=[SUB_PROVIDER])
        tool = Agent(_parent_session(tmp_path, chat_provider=object()))
        assert _weaker_provider(tool._session) is False
        assert TOOL_NOTE_MARKER not in tool.base.description

    def test_session_provider_overrides_the_global_one(self, tmp_path: Path, providers):
        providers(subs=[SUB_PROVIDER])
        assert _weaker_provider(_parent_session(tmp_path, provider_dict=SUB_PROVIDER)) is False
        assert (
            _weaker_provider(_parent_session(tmp_path, provider_dict=MAIN_PROVIDER)) is True
        )

    def test_unreadable_config_is_reported_as_same(self):
        """The helper runs on the model's request path: it must never raise."""
        assert _weaker_provider(SimpleNamespace(custom_config="broken")) is False


# ---------------------------------------------------------------------------
# The run forwards the flag to the child session
# ---------------------------------------------------------------------------


class TestRunForwardsTheFlag:
    async def _run(self, parent: SimpleNamespace) -> Any:
        sub_session = MagicMock()
        sub_session.id = "sub-1"
        sub_session.get_custom_config.return_value = {}
        with (
            patch(
                "kimix.utils._create_session_async", new_callable=AsyncMock
            ) as mock_create,
            patch("kimix.utils.prompt_async", new_callable=AsyncMock),
            patch("kimix.utils.close_session_async", new_callable=AsyncMock),
        ):
            mock_create.return_value = sub_session
            result = await Agent(parent)(SubAgentParams(prompt="do X"))
        assert not result.is_error
        return mock_create.await_args.kwargs

    async def test_different_provider_runs_the_child_as_weaker(
        self, tmp_path: Path, providers
    ):
        providers(subs=[SUB_PROVIDER])
        kwargs = await self._run(_parent_session(tmp_path))
        assert kwargs["weaker_provider"] is True

    async def test_same_provider_runs_the_child_normally(self, tmp_path: Path, providers):
        providers(subs=None)
        kwargs = await self._run(_parent_session(tmp_path))
        assert kwargs["weaker_provider"] is False
