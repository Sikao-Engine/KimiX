"""Comprehensive tests for kimix.cli_impl.init."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import orjson
import pytest

from kimix.cli_impl import init as init_module


@pytest.fixture(autouse=True)
def clear_api_key_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMIX_API_KEY", raising=False)


@pytest.fixture
def default_config() -> dict[str, Any]:
    return {
        "model_name": "kimi-for-coding",
        "name": "moonshot",
        "model": "kimi-for-coding",
        "max_context_size": 262144,
        "capabilities": ["always_thinking"],
        "url": "https://api.kimi.com/coding/v1",
        "type": "kimi",
        "loop_control": {
            "max_steps_per_turn": 5000,
            "max_retries_per_step": 3,
            "max_ralph_iterations": 0,
            "reserved_context_size": 50000,
            "compaction_trigger_ratio": 0.85,
        },
        "max_tokens": 128000,
        "show_thinking_stream": True,
        "thinking_effort": "low",
        "background": {
            "max_running_tasks": 4,
            "read_max_bytes": 30000,
            "notification_tail_lines": 20,
            "notification_tail_chars": 3000,
            "wait_poll_interval_ms": 500,
            "worker_heartbeat_interval_ms": 5000,
            "worker_stale_after_ms": 15000,
            "kill_grace_period_ms": 2000,
            "keep_alive_on_exit": False,
            "agent_task_timeout_s": 900,
            "print_wait_ceiling_s": 3600,
        },
    }


@pytest.fixture
def temp_config_path(tmp_path: Path, default_config: dict[str, Any]) -> Path:
    path = tmp_path / "default_config.json"
    path.write_bytes(orjson.dumps(default_config))
    return path


class TestAskHelpers:
    def test_ask_uses_default_when_empty(self) -> None:
        with patch("builtins.input", return_value="   "):
            result = init_module._ask("Prompt", "default_value")
        assert result == "default_value"

    def test_ask_uses_input_when_provided(self) -> None:
        with patch("builtins.input", return_value="custom"):
            result = init_module._ask("Prompt", "default_value")
        assert result == "custom"

    def test_ask_strips_input(self) -> None:
        with patch("builtins.input", return_value="  custom  "):
            result = init_module._ask("Prompt", "default_value")
        assert result == "custom"


class TestAskModelName:
    def test_default(self) -> None:
        with patch("builtins.input", return_value=""):
            result = init_module._ask_model_name()
        assert result == "kimi-for-coding"

    def test_custom(self) -> None:
        with patch("builtins.input", return_value="gpt-4"):
            result = init_module._ask_model_name()
        assert result == "gpt-4"


class TestAskModelType:
    def test_default(self) -> None:
        with patch("builtins.input", return_value=""):
            result = init_module._ask_model_type()
        assert result == "kimi"

    def test_custom_valid(self) -> None:
        with patch("builtins.input", return_value="anthropic"):
            result = init_module._ask_model_type()
        assert result == "anthropic"

    def test_invalid_then_valid(self) -> None:
        inputs = iter(["invalid", "openai_legacy"])
        with patch("builtins.input", side_effect=lambda: next(inputs)):
            result = init_module._ask_model_type()
        assert result == "openai_legacy"

    def test_all_valid_types(self) -> None:
        for t in init_module._VALID_TYPES:
            with patch("builtins.input", return_value=t):
                result = init_module._ask_model_type()
            assert result == t


class TestAskApiKey:
    def test_provided_first_time(self) -> None:
        with patch("builtins.input", return_value="secret-key"):
            result = init_module._ask_api_key()
        assert result == "secret-key"

    def test_empty_first_time_then_provided(self) -> None:
        inputs = iter(["", "secret-key"])
        with patch("builtins.input", side_effect=lambda: next(inputs)):
            result = init_module._ask_api_key()
        assert result == "secret-key"

    def test_empty_both_times(self) -> None:
        with patch("builtins.input", return_value=""):
            result = init_module._ask_api_key()
        assert result == ""


class TestAskContextSize:
    def test_default(self) -> None:
        with patch("builtins.input", return_value=""):
            result = init_module._ask_context_size()
        assert result == 262144

    def test_custom_valid(self) -> None:
        with patch("builtins.input", return_value="512k"):
            result = init_module._ask_context_size()
        assert result == 524288

    def test_1m(self) -> None:
        with patch("builtins.input", return_value="1M"):
            result = init_module._ask_context_size()
        assert result == 1048576

    def test_invalid_then_valid(self) -> None:
        inputs = iter(["invalid", "128k"])
        with patch("builtins.input", side_effect=lambda: next(inputs)):
            result = init_module._ask_context_size()
        assert result == 131072


class TestAskThinkingEffort:
    def test_default(self) -> None:
        with patch("builtins.input", return_value=""):
            result = init_module._ask_thinking_effort()
        assert result == "low"

    def test_custom_valid(self) -> None:
        with patch("builtins.input", return_value="high"):
            result = init_module._ask_thinking_effort()
        assert result == "high"

    def test_invalid_then_valid(self) -> None:
        inputs = iter(["invalid", "medium"])
        with patch("builtins.input", side_effect=lambda: next(inputs)):
            result = init_module._ask_thinking_effort()
        assert result == "medium"


class TestAskUrl:
    def test_default(self) -> None:
        with patch("builtins.input", return_value=""):
            result = init_module._ask_url()
        assert result == "https://api.kimi.com/coding/v1"

    def test_custom(self) -> None:
        with patch("builtins.input", return_value="https://api.openai.com/v1"):
            result = init_module._ask_url()
        assert result == "https://api.openai.com/v1"


class TestInit:
    def test_all_defaults(self, temp_config_path: Path, default_config: dict[str, Any]) -> None:
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", return_value=""),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["model"] == "kimi-for-coding"
        assert saved["type"] == "kimi"
        assert saved["api_key"] == ""
        assert saved["max_context_size"] == 262144
        assert saved["max_tokens"] == 128000
        assert saved["thinking_effort"] == "low"
        assert saved["url"] == "https://api.kimi.com/coding/v1"
        assert saved["temperature"] == 1.0

    def test_all_custom(self, temp_config_path: Path, default_config: dict[str, Any]) -> None:
        inputs = iter([
            "gpt-4",           # model name
            "openai_legacy",   # type
            "sk-test",         # api key
            "512k",            # context size
            "200000",          # max tokens
            "high",            # thinking effort
            "https://api.openai.com/v1",  # url
            "0.5",             # temperature
        ])
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", side_effect=lambda: next(inputs)),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["model"] == "gpt-4"
        assert saved["type"] == "openai_legacy"
        assert saved["api_key"] == "sk-test"
        assert saved["max_context_size"] == 524288
        assert saved["max_tokens"] == 200000
        assert saved["thinking_effort"] == "high"
        assert saved["url"] == "https://api.openai.com/v1"
        assert saved["temperature"] == 0.5

    def test_api_key_empty_then_custom(
        self, temp_config_path: Path, default_config: dict[str, Any]
    ) -> None:
        inputs = iter([
            "",                # model name (default)
            "",                # type (default)
            "",                # api key first empty
            "sk-real",         # api key second
            "",                # context size (default)
            "",                # max tokens (default)
            "",                # thinking effort (default)
            "",                # url (default)
            "",                # temperature (default)
        ])
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", side_effect=lambda: next(inputs)),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["api_key"] == "sk-real"

    def test_api_key_completely_empty(
        self, temp_config_path: Path, default_config: dict[str, Any]
    ) -> None:
        inputs = iter([
            "",                # model name (default)
            "",                # type (default)
            "",                # api key first empty
            "",                # api key second empty (skip)
            "",                # context size (default)
            "",                # max tokens (default)
            "",                # thinking effort (default)
            "",                # url (default)
            "",                # temperature (default)
        ])
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", side_effect=lambda: next(inputs)),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["api_key"] == ""

    def test_invalid_type_then_valid(
        self, temp_config_path: Path, default_config: dict[str, Any]
    ) -> None:
        inputs = iter([
            "",                # model name (default)
            "bad_type",        # type invalid
            "anthropic",       # type valid
            "",                # api key first
            "",                # api key second
            "",                # context size (default)
            "",                # max tokens (default)
            "",                # thinking effort (default)
            "",                # url (default)
            "",                # temperature (default)
        ])
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", side_effect=lambda: next(inputs)),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["type"] == "anthropic"

    def test_invalid_context_size_then_valid(
        self, temp_config_path: Path, default_config: dict[str, Any]
    ) -> None:
        inputs = iter([
            "",                # model name (default)
            "",                # type (default)
            "",                # api key first empty
            "",                # api key second empty
            "bad_size",        # context size invalid
            "1M",              # context size valid
            "",                # max tokens (default)
            "",                # thinking effort (default)
            "",                # url (default)
            "",                # temperature (default)
        ])
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", side_effect=lambda: next(inputs)),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["max_context_size"] == 1048576
        assert saved["max_tokens"] == 128000

    def test_invalid_thinking_effort_then_valid(
        self, temp_config_path: Path, default_config: dict[str, Any]
    ) -> None:
        inputs = iter([
            "",                # model name (default)
            "",                # type (default)
            "",                # api key first empty
            "",                # api key second empty
            "",                # context size (default)
            "",                # max tokens (default)
            "bad_effort",      # thinking effort invalid
            "medium",          # thinking effort valid
            "",                # url (default)
            "",                # temperature (default)
        ])
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", side_effect=lambda: next(inputs)),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["thinking_effort"] == "medium"

    def test_preserves_other_config_keys(
        self, temp_config_path: Path, default_config: dict[str, Any]
    ) -> None:
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", return_value=""),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["model_name"] == default_config["model_name"]
        assert saved["name"] == default_config["name"]
        assert saved["capabilities"] == default_config["capabilities"]
        assert saved["loop_control"] == default_config["loop_control"]
        assert saved["show_thinking_stream"] == default_config["show_thinking_stream"]
        assert saved["background"] == default_config["background"]

    def test_custom_reserved_context_size(
        self, temp_config_path: Path, default_config: dict[str, Any]
    ) -> None:
        default_config["loop_control"]["reserved_context_size"] = 10000
        temp_config_path.write_bytes(orjson.dumps(default_config))

        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", return_value=""),
        ):
            init_module.init()

        saved = orjson.loads(temp_config_path.read_bytes())
        assert saved["max_tokens"] == 128000
        assert saved["loop_control"]["reserved_context_size"] == 10000

    def test_missing_loop_control_defaults_reserved_to_50000(
        self, tmp_path: Path
    ) -> None:
        config = {"model": "kimi-for-coding"}
        path = tmp_path / "default_config.json"
        path.write_bytes(orjson.dumps(config))

        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", path),
            patch("builtins.input", return_value=""),
        ):
            init_module.init()

        saved = orjson.loads(path.read_bytes())
        assert saved["max_tokens"] == 128000
        assert saved["loop_control"]["reserved_context_size"] == 50000

    def test_load_and_save_use_orjson(self, temp_config_path: Path) -> None:
        with (
            patch.object(init_module, "_DEFAULT_CONFIG_PATH", temp_config_path),
            patch("builtins.input", return_value=""),
            patch("kimix.cli_impl.init.orjson") as mock_orjson,
        ):
            mock_orjson.loads.return_value = {
                "model": "kimi-for-coding",
                "type": "kimi",
                "loop_control": {"reserved_context_size": 50000},
                "thinking_effort": "low",
                "url": "https://api.kimi.com/coding/v1",
            }
            mock_orjson.dumps.return_value = b'{}'
            mock_orjson.OPT_INDENT_2 = 1
            init_module.init()

        mock_orjson.loads.assert_called_once()
        mock_orjson.dumps.assert_called_once()
