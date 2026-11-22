from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import kimix.cli_impl.commands as commands
from kimi_cli.config import Config, load_config_from_string
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.subagent import Agent, AgentClose, AgentList


_SUBAGENT_TOOL_TYPES = (Agent, AgentList, AgentClose)


def _tool_session(config_json: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        custom_config={"config_json": config_json},
        custom_data={},
    )


def test_subagent_config_defaults_to_enabled() -> None:
    assert Config().subagent is True


def test_subagent_config_loads_from_json() -> None:
    assert load_config_from_string('{"subagent": false}').subagent is False


@pytest.mark.parametrize("tool_type", _SUBAGENT_TOOL_TYPES)
def test_subagent_tools_skip_when_json_config_disables_them(tool_type: type) -> None:
    session = _tool_session({"subagent": False})

    with pytest.raises(SkipThisTool):
        tool_type(session)


@pytest.mark.parametrize("tool_type", _SUBAGENT_TOOL_TYPES)
def test_subagent_tools_skip_when_typed_config_disables_them(tool_type: type) -> None:
    session = _tool_session({})

    with pytest.raises(SkipThisTool):
        tool_type(session, Config(subagent=False))


@pytest.mark.parametrize("tool_type", _SUBAGENT_TOOL_TYPES)
def test_subagent_tools_remain_enabled_by_default(tool_type: type) -> None:
    tool = tool_type(_tool_session({}))

    assert tool._session is not None


def test_workspace_json_overrides_typed_subagent_default() -> None:
    tool = Agent(_tool_session({"subagent": True}), Config(subagent=False))

    assert tool._session is not None


@pytest.mark.parametrize(
    ("enabled", "expected_names"),
    [
        (False, set()),
        (True, {"subagent", "list_agents", "interrupt_agent"}),
    ],
)
def test_tool_loader_applies_subagent_config(
    enabled: bool,
    expected_names: set[str],
) -> None:
    from kimi_cli.session import Session
    from kimi_cli.soul.toolset import KimiToolset

    toolset = KimiToolset()
    toolset.load_tools(
        [
            "kimi_cli.tools.subagent:subagent",
            "kimi_cli.tools.subagent:list_agents",
            "kimi_cli.tools.subagent:interrupt_agent",
        ],
        {
            Session: _tool_session({"subagent": enabled}),
            Config: Config(),
        },
    )

    assert {tool.name for tool in toolset.tools} == expected_names


class _CommandSession:
    def __init__(self) -> None:
        self.custom_config: dict[str, Any] = {"config_json": {}}
        self.runtime_config = Config()
        self._cli = SimpleNamespace(
            soul=SimpleNamespace(
                agent=SimpleNamespace(
                    runtime=SimpleNamespace(config=self.runtime_config),
                ),
            ),
        )
        self._create_kwargs: dict[str, Any] = {"config": self.runtime_config}
        self.restart_count = 0

    def get_custom_config(self) -> dict[str, Any]:
        return self.custom_config

    async def _restart(self) -> None:
        self.restart_count += 1


def _run_command(session: _CommandSession, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(commands, "get_default_session", lambda: session)
    monkeypatch.setattr(commands, "run_on_event_loop", asyncio.run)
    result = commands._cmd_subagent(["subagent", value], [])
    assert result == (None, False)


@pytest.mark.parametrize(
    ("value", "enabled"),
    [("on", True), ("off", False)],
)
def test_subagent_command_updates_session_and_reloads_tools(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    enabled: bool,
) -> None:
    session = _CommandSession()

    _run_command(session, monkeypatch, value)

    assert session.custom_config["config_json"]["subagent"] is enabled
    assert session.runtime_config.subagent is enabled
    assert session._create_kwargs["config"] is session.runtime_config
    assert session.restart_count == 1


def test_subagent_command_rejects_invalid_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _CommandSession()
    monkeypatch.setattr(commands, "get_default_session", lambda: session)

    result = commands._cmd_subagent(["subagent", "invalid"], [])

    assert result == (None, False)
    assert session.restart_count == 0


def test_subagent_command_requires_active_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "get_default_session", lambda: None)

    result = commands._cmd_subagent(["subagent", "off"], [])

    assert result == (None, False)


def test_subagent_command_registered() -> None:
    assert commands._command_map["subagent"] is commands._cmd_subagent
