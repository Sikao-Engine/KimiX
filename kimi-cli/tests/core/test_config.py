from __future__ import annotations

import pytest
from inline_snapshot import snapshot

from kimi_cli.config import (
    Config,
    get_default_config,
    load_config,
    load_config_from_string,
)
from kimi_cli.exception import ConfigError


def test_default_config():
    config = get_default_config()
    assert config == snapshot(Config())


def test_default_config_dump():
    config = get_default_config()
    assert config.model_dump() == snapshot(
        {
            "default_model": "",
            "default_thinking": False,
            "default_yolo": False,
            "default_editor": "",
            "theme": "dark",
            "show_thinking_stream": True,
            "models": {},
            "providers": {},
            "loop_control": {
                "max_steps_per_turn": 5000,
                "max_retries_per_step": 3,
                "max_ralph_iterations": 0,
                "reserved_context_size": 75000,
                "compaction_trigger_ratio": 0.75,
                "max_system_prompt_tokens": 4000,
                "max_preserved_messages": 2,
                "min_preserved_messages": 1,
                "adaptive_preserve_enabled": True,
                "compact_reminder_enabled": True,
                "compact_reminder_threshold": 0.7,
                "done_reminder_enabled": True,
                "done_reminder_cooldown_steps": 10,
                "auto_retrieve_history": True,
                "auto_retrieve_history_threshold": 5.0,
                "auto_retrieve_working_memory": True,
                "auto_retrieve_working_memory_threshold": 5.0,
                "auto_retrieve_recency_memory": True,
                "auto_retrieve_recency_memory_threshold": 4.0,
                "auto_retrieve_recency_weight": 1.0,
                "auto_retrieve_max_injections_per_turn": 3,
                "auto_retrieve_max_tokens_per_turn": 2000,
            },
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
            "notifications": {
                "claim_stale_after_ms": 15000,
            },
            "services": {"moonshot_search": None, "moonshot_fetch": None},
            "mcp": {"client": {"tool_call_timeout_ms": 60000}},
            "hooks": [],
            "merge_all_available_skills": True,
            "extra_skill_dirs": [],
            "max_tokens": 128000,
            "temperature": None,
            "top_p": None,
            "top_k": None,
            "thinking_effort": None,
        }
    )


def test_load_config_text_toml():
    config = load_config_from_string('default_model = ""\n')
    assert config == get_default_config()


def test_load_config_text_json():
    config = load_config_from_string('{"default_model": ""}')
    assert config == get_default_config()


def test_load_config_sets_source_file(tmp_path):
    config_file = tmp_path / "custom.toml"

    config = load_config(config_file)

    assert config.source_file == config_file.resolve()
    assert not config.is_from_default_location


def test_load_config_text_has_no_source_file():
    config = load_config_from_string('{"default_model": ""}')

    assert config.source_file is None


def test_load_config_text_invalid():
    with pytest.raises(ConfigError, match="Invalid configuration text"):
        load_config_from_string("not valid {")


def test_load_config_invalid_ralph_iterations():
    with pytest.raises(ConfigError, match="max_ralph_iterations"):
        load_config_from_string('{"loop_control": {"max_ralph_iterations": -2}}')


def test_load_config_reserved_context_size():
    config = load_config_from_string('{"loop_control": {"reserved_context_size": 30000}}')
    assert config.loop_control.reserved_context_size == 30000


def test_load_config_max_steps_per_turn():
    config = load_config_from_string("[loop_control]\nmax_steps_per_turn = 42\n")
    assert config.loop_control.max_steps_per_turn == 42


def test_load_config_max_steps_per_run():
    config = load_config_from_string('{"loop_control": {"max_steps_per_run": 7}}')
    assert config.loop_control.max_steps_per_turn == 7


def test_load_config_reserved_context_size_too_low():
    with pytest.raises(ConfigError, match="reserved_context_size"):
        load_config_from_string('{"loop_control": {"reserved_context_size": 500}}')


def test_load_config_compaction_trigger_ratio():
    config = load_config_from_string('{"loop_control": {"compaction_trigger_ratio": 0.8}}')
    assert config.loop_control.compaction_trigger_ratio == 0.8


def test_load_config_compaction_trigger_ratio_default():
    config = load_config_from_string("{}")
    assert config.loop_control.compaction_trigger_ratio == 0.75


def test_load_config_compaction_trigger_ratio_too_low():
    with pytest.raises(ConfigError, match="compaction_trigger_ratio"):
        load_config_from_string('{"loop_control": {"compaction_trigger_ratio": 0.3}}')


def test_load_config_compaction_trigger_ratio_too_high():
    with pytest.raises(ConfigError, match="compaction_trigger_ratio"):
        load_config_from_string('{"loop_control": {"compaction_trigger_ratio": 1.0}}')


def test_load_config_supported_efforts():
    config = load_config_from_string(
        '{"models": {"m": {"provider": "p", "model": "m", "max_context_size": 1000, "supported_efforts": ["low", "high"]}}, "providers": {"p": {"type": "anthropic", "base_url": "https://example.com", "api_key": "k"}}}'
    )
    assert config.models["m"].supported_efforts == {"low", "high"}


def test_load_config_supported_efforts_defaults_to_full_set():
    config = load_config_from_string(
        '{"models": {"m": {"provider": "p", "model": "m", "max_context_size": 1000}}, "providers": {"p": {"type": "anthropic", "base_url": "https://example.com", "api_key": "k"}}}'
    )
    assert config.models["m"].supported_efforts == {"low", "medium", "high", "xhigh", "max"}


def test_load_config_invalid_supported_efforts():
    with pytest.raises(ConfigError, match="supported_efforts"):
        load_config_from_string(
            '{"models": {"m": {"provider": "p", "model": "m", "max_context_size": 1000, "supported_efforts": ["low", "invalid"]}}, "providers": {"p": {"type": "anthropic", "base_url": "https://example.com", "api_key": "k"}}}'
        )


def test_load_config_supported_efforts_rejects_off():
    with pytest.raises(ConfigError, match="supported_efforts|off"):
        load_config_from_string(
            '{"models": {"m": {"provider": "p", "model": "m", "max_context_size": 1000, "supported_efforts": ["low", "off"]}}, "providers": {"p": {"type": "anthropic", "base_url": "https://example.com", "api_key": "k"}}}'
        )
