from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import orjson

from kimix.base import print_info, print_success, print_warning
default_config = '''
{
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
        "compaction_trigger_ratio": 0.85
    },
    "max_tokens": 128000,
    "show_thinking_stream": true,
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
        "keep_alive_on_exit": false,
        "agent_task_timeout_s": 900,
        "print_wait_ceiling_s": 3600
    }
}
'''
_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent / "default_config.json"

_CONTEXT_SIZE_OPTIONS: dict[str, int] = {
    "128k": 131072,
    "200k": 204800,
    "256k": 262144,
    "512k": 524288,
    "1M": 1048576,
}

_VALID_TYPES = (
    "kimi",
    "openai_legacy",
    "openai_responses",
    "anthropic",
    "google_genai",
    "gemini",
    "vertexai",
)

_VALID_THINKING_EFFORTS = ('off', 'low', 'medium', 'high', 'xhigh', 'max')


def _load_default_config() -> dict[str, Any]:
    if _DEFAULT_CONFIG_PATH.exists():
        with open(_DEFAULT_CONFIG_PATH, "rb") as f:
            return orjson.loads(f.read())
    return orjson.loads(default_config)


def _save_config(config: dict[str, Any]) -> None:
    with open(_DEFAULT_CONFIG_PATH, "wb") as f:
        f.write(orjson.dumps(config, option=orjson.OPT_INDENT_2))


def _ask(prompt: str, default: str) -> str:
    print_info(f"{prompt} [{default}]: ", end="")
    value = input().strip()
    return value if value else default


def _ask_model_name(default: str = "kimi-for-coding") -> str:
    return _ask("Enter model name", default)


def _ask_model_type(default: str = "kimi") -> str:
    options_str = ", ".join(_VALID_TYPES)
    while True:
        value = _ask(f"Enter model type ({options_str})", default)
        if value in _VALID_TYPES:
            return value
        print_warning(f"Invalid type '{value}', please choose from: {options_str}")


def _ask_api_key() -> str:
    skip = False
    if os.environ.get("KIMI_API_KEY"):
        print_info("API key already set in environment variable KIMI_API_KEY.")
        skip = True
    if os.environ.get("KIMIX_API_KEY"):
        print_info("API key already set in environment variable KIMIX_API_KEY.")
        skip = True
    print_info("Enter API key (usually required, or set KIMI_API_KEY/KIMIX_API_KEY env var): ", end="")
    value = input().strip()
    if not skip and not value:
        print_warning("API key is usually required. Press Enter again to skip.")
        print_info("Enter API key: ", end="")
        value = input().strip()
    return value


def _ask_context_size(default: str = "256k") -> int:
    options_str = " ".join(_CONTEXT_SIZE_OPTIONS.keys())
    while True:
        value = _ask(f"Enter model context size ({options_str})", default)
        if value in _CONTEXT_SIZE_OPTIONS:
            return _CONTEXT_SIZE_OPTIONS[value]
        print_warning(f"Invalid size '{value}', please choose from: {options_str}")


def _ask_thinking_effort(default: str = "low") -> str:
    options_str = ", ".join(_VALID_THINKING_EFFORTS)
    while True:
        value = _ask(f"Enter thinking effort ({options_str})", default)
        if value in _VALID_THINKING_EFFORTS:
            return value
        print_warning(f"Invalid effort '{value}', please choose from: {options_str}")


def _ask_url(default: str = "https://api.kimi.com/coding/v1") -> str:
    return _ask("Enter model URL", default)


def _ask_max_token(context_size: int, reserved: int, default: int) -> int:
    max_allowed = context_size - reserved
    prompt = f"Enter max tokens (max {max_allowed})"
    while True:
        value = _ask(prompt, str(default)).strip()
        if not value:
            return default
        try:
            num = int(value)
        except ValueError:
            print_warning(f"Invalid number '{value}', using default {default}")
            return default
        if num <= 0 or num > max_allowed:
            print_warning(f"Value {num} out of range, using default {default}")
            return default
        return num


def init(initialize: bool = True) -> None:
    if not initialize:
        v = input('default config not found, initialize? you can use /init any time. (y/n)').strip().lower() 
        initialize = v == 'y' or not v
    config = _load_default_config()
    try:
        if initialize:
            model = _ask_model_name(config.get("model", "kimi-for-coding"))
            config["model"] = model

            model_type = _ask_model_type(config.get("type", "kimi"))
            config["type"] = model_type

            api_key = _ask_api_key()
            config["api_key"] = api_key

            context_size = _ask_context_size()
            config["max_context_size"] = context_size

            reserved = config.get("loop_control", {}).get("reserved_context_size", 50000)
            max_tokens = _ask_max_token(context_size, reserved, 128000)
            config["max_tokens"] = max_tokens

            thinking = _ask_thinking_effort(config.get("thinking_effort", "low"))
            config["thinking_effort"] = thinking

            url = _ask_url(config.get("url", "https://api.kimi.com/coding/v1"))
            config["url"] = url
    except KeyboardInterrupt:
        print_warning('keyboard interruped.')
        return
    _save_config(config)
    if initialize:
        print_success(f"Configuration saved successfully to {_DEFAULT_CONFIG_PATH}!")
