"""Tests for the ``researcher`` sub-provider role.

The research tool spawns a read-only child session backed by a sub-provider
with ``role = "researcher"`` and the ``agent_readonly.json`` manifest. Config
normalization itself is role-generic, so these tests pin only the
role-specific contract:

  - ``config._ROLE_DEFAULT_AGENT_FILES`` maps ``researcher`` to the
    ``agent_readonly.json`` manifest that actually ships with the package.
  - ``config._SUB_PROVIDER_PICK_PRIORITY`` ranks ``researcher`` last, so a
    researcher sub-provider can backfill the main provider only when nothing
    more suitable exists (never ahead of ``sub_agent``/``planner``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import kimix.base as base
from kimix.utils import config as config_mod


def _provider(model: str, role: str | None = None) -> dict[str, Any]:
    """A minimal but complete provider dict (all required keys present)."""
    entry: dict[str, Any] = {
        "model": model,
        "max_context_size": 100000,
        "url": f"https://api.example.com/{model}",
        "type": "openai_legacy",
        "api_key": f"sk-{model}",
    }
    if role is not None:
        entry["role"] = role
    return entry


class TestResearcherDefaultAgentFile:
    def test_researcher_maps_to_agent_readonly_json(self) -> None:
        assert config_mod._ROLE_DEFAULT_AGENT_FILES["researcher"] == "agent_readonly.json"

    def test_mapped_manifest_exists_in_package(self) -> None:
        manifest = base._default_agent_file_dir / config_mod._ROLE_DEFAULT_AGENT_FILES["researcher"]
        assert manifest.is_file(), f"manifest not found: {manifest}"

    def test_mapped_manifest_declares_read_only_tools(self) -> None:
        import orjson

        manifest = base._default_agent_file_dir / config_mod._ROLE_DEFAULT_AGENT_FILES["researcher"]
        tools = orjson.loads(manifest.read_bytes())["agent"]["tools"]
        assert set(tools) == {
            "kimi_cli.tools.file:read",
            "kimi_cli.tools.file:read_image",
            "kimi_cli.tools.file:glob",
            "kimi_cli.tools.file:grep",
            "kimi_cli.tools.web.kimix_fetch:fetch_url",
            "kimi_cli.tools.web:web_search",
            "kimi_cli.tools.skill:skill",
        }

    def test_only_known_roles_have_default_manifests(self) -> None:
        # Roles without a dedicated manifest must not appear here; extending
        # the dict is a deliberate, reviewed change.
        assert set(config_mod._ROLE_DEFAULT_AGENT_FILES) == {"researcher"}


class TestResearcherPickPriority:
    def test_researcher_is_last_in_pick_priority(self) -> None:
        priority = config_mod._SUB_PROVIDER_PICK_PRIORITY
        assert priority[-1] == "researcher"
        assert "sub_agent" in priority[:-1]
        assert "planner" in priority[:-1]

    def test_researcher_never_picked_ahead_of_existing_roles(self) -> None:
        # Root has no 'model': the main provider must come from the
        # no-role/sub_agent/planner entries even when a researcher exists.
        for higher in (
            _provider("plain-model"),                       # no role
            _provider("sub-model", role="sub_agent"),
            _provider("plan-model", role="planner"),
        ):
            config_data: dict[str, Any] = {}
            sub_providers = [higher, _provider("research-model", role="researcher")]
            config_mod._pick_main_from_sub_providers(config_data, None, sub_providers)
            assert config_data.get("model") == higher["model"]

    def test_researcher_backfills_main_when_nothing_else_available(self) -> None:
        # A researcher-only config still gets a usable main provider.
        config_data: dict[str, Any] = {}
        sub_providers = [_provider("research-model", role="researcher")]
        config_mod._pick_main_from_sub_providers(config_data, None, sub_providers)
        assert config_data.get("model") == "research-model"
        assert "role" not in config_data

    def test_root_model_wins_over_researcher(self) -> None:
        config_data: dict[str, Any] = {"model": "root-model"}
        sub_providers = [_provider("research-model", role="researcher")]
        config_mod._pick_main_from_sub_providers(config_data, None, sub_providers)
        assert config_data["model"] == "root-model"


class TestResearcherNormalization:
    def test_explicit_researcher_role_is_kept(self) -> None:
        normalized = config_mod._normalize_sub_providers(
            None, [_provider("research-model", role="researcher")]
        )
        assert len(normalized) == 1
        assert normalized[0]["role"] == "researcher"

    def test_missing_role_defaults_to_sub_agent_not_researcher(self) -> None:
        # Role-generic behavior: only an explicit role becomes researcher.
        normalized = config_mod._normalize_sub_providers(None, [_provider("plain")])
        assert normalized[0]["role"] == "sub_agent"

    def test_load_and_set_provider_registers_researcher_role(self, monkeypatch) -> None:
        captured: list[list[dict[str, Any]]] = []
        monkeypatch.setattr(base, "set_default_sub_providers", captured.append)
        config_data: dict[str, Any] = {
            "model": "root-model",
            "max_context_size": 100000,
            "url": "https://api.example.com/root",
            "type": "openai_legacy",
            "sub_providers": [_provider("research-model", role="researcher")],
        }
        config_mod._load_and_set_provider(config_data, Path("config.json"))
        assert captured, "set_default_sub_providers was not called"
        roles = [p.get("role", "sub_agent") for p in captured[0]]
        assert roles == ["researcher"]
