"""Tests for ``kimix.cli_impl.init`` provider templates and interactive init flow."""
from __future__ import annotations

import builtins
import json
from pathlib import Path

import orjson
import pytest

from kimix.cli_impl import init as init_mod
from kimix.cli_impl.init import (
    _CONTEXT_SIZE_OPTIONS,
    _ask_context_size,
    _ask_max_token,
    _ask_sub_provider,
    _first_sub_provider,
    deepseek_default_config,
    init,
    kimi_default_config,
    minimax_default_config,
)

REQUIRED_PROVIDER_KEYS = ("type", "max_context_size", "model", "url")

DOCS_DIR = Path(__file__).parent.parent.parent / "docs"
DEFAULT_CONFIG_JSON = Path(init_mod.__file__).parent.parent / "default_config.json"


# ── Template consistency ────────────────────────────────────────────────────

@pytest.mark.parametrize("template", [kimi_default_config, deepseek_default_config, minimax_default_config])
def test_templates_are_valid_json_with_required_keys(template: str) -> None:
    config = orjson.loads(template)
    for key in REQUIRED_PROVIDER_KEYS:
        assert key in config, f"missing required key {key!r}"


def test_kimi_template_matches_shipped_default_config() -> None:
    shipped = orjson.loads(DEFAULT_CONFIG_JSON.read_bytes())
    template = orjson.loads(kimi_default_config)
    # The shipped file only adds the user-provided api_key on top of the template.
    shipped.pop("api_key", None)
    assert shipped == template


@pytest.mark.parametrize(
    "template,doc_name",
    [
        (kimi_default_config, "kimi.json"),
        (deepseek_default_config, "deepseek.json"),
        (minimax_default_config, "minimax.json"),
    ],
)
def test_templates_match_docs_examples(template: str, doc_name: str) -> None:
    template_cfg = orjson.loads(template)
    doc_cfg = json.loads((DOCS_DIR / doc_name).read_text(encoding="utf-8"))
    for key, value in template_cfg.items():
        assert doc_cfg.get(key) == value, f"{doc_name}: {key!r} mismatch: {doc_cfg.get(key)!r} != {value!r}"


# ── Context size options ────────────────────────────────────────────────────

def test_one_megabyte_option_is_1_mib() -> None:
    assert _CONTEXT_SIZE_OPTIONS["1M"] == 1048576


def test_context_size_reverse_lookup_accepts_plain_million(monkeypatch: pytest.MonkeyPatch) -> None:
    # Existing config with 1000000 (legacy "1M") should still preset to the 1M option.
    monkeypatch.setattr(builtins, "input", lambda: "")
    assert _ask_context_size({"max_context_size": 1000000}) == 1048576
    assert _ask_context_size({"max_context_size": 1048576}) == 1048576
    assert _ask_context_size({"max_context_size": 256000}) == 256000


# ── max_tokens prompting ────────────────────────────────────────────────────

def test_ask_max_token_reasks_on_invalid_then_accepts(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["abc", "999999999", "64000"])
    monkeypatch.setattr(builtins, "input", lambda: next(answers))
    assert _ask_max_token(1048576, 50000, 131072) == 64000


def test_ask_max_token_empty_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtins, "input", lambda: "")
    assert _ask_max_token(1048576, 50000, 131072) == 131072


# ── Sub-provider handling ───────────────────────────────────────────────────

def test_first_sub_provider_reads_new_and_legacy_forms() -> None:
    entry = {"role": "planner", "model": "m"}
    assert _first_sub_provider({"sub_providers": [entry, {"role": "sub_agent"}]}) is entry
    assert _first_sub_provider({"sub_provider": entry}) is entry
    assert _first_sub_provider({"sub_providers": "bogus"}) is None
    assert _first_sub_provider({}) is None


def test_ask_sub_provider_defaults_role_to_sub_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Decline the sub-provider prompt.
    monkeypatch.setattr(builtins, "input", lambda: "n")
    assert _ask_sub_provider() is None


# ── Full init() flow ────────────────────────────────────────────────────────

def _run_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answers: list[str]) -> dict:
    config_path = tmp_path / "default_config.json"
    monkeypatch.setattr(init_mod, "_DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("KIMIX_API_KEY", raising=False)
    monkeypatch.setattr(builtins, "input", lambda: answers.pop(0))
    monkeypatch.setattr("os.startfile", lambda p: None, raising=False)
    monkeypatch.setattr(init_mod.subprocess, "run", lambda *a, **k: None)
    init(initialize=True)
    return orjson.loads(config_path.read_bytes())


def test_init_writes_sub_providers_list_with_role(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    answers = [
        "",          # template -> kimi
        "",          # model
        "",          # type
        "sk-test",   # api key
        "",          # context size -> 1M
        "",          # max tokens
        "",          # thinking effort
        "",          # capabilities
        "",          # url
        "y",         # configure sub provider
        "",          # role -> sub_agent
        "",          # sub model
        "",          # sub type
        "",          # sub url
        "",          # sub api key (first prompt)
        "",          # sub api key (skip confirmation)
        "",          # sub context size
        "",          # sub thinking effort
        "",          # sub capabilities
        "",          # sub max tokens
    ]
    config = _run_init(monkeypatch, tmp_path, answers)
    assert config["model"] == "kimi-for-coding"
    assert config["max_context_size"] == 1048576
    sub = config["sub_providers"]
    assert isinstance(sub, list) and len(sub) == 1
    assert sub[0]["role"] == "sub_agent"
    assert sub[0]["type"] == "kimi"
    assert "sub_provider" not in config


def test_init_without_sub_provider_strips_legacy_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_path = tmp_path / "default_config.json"
    legacy = orjson.loads(kimi_default_config)
    legacy["sub_provider"] = {"role": "sub_agent", "model": "old", "type": "kimi",
                              "max_context_size": 128000, "url": "https://api.kimi.com/coding/v1"}
    config_path.write_bytes(orjson.dumps(legacy))
    answers = [
        "",          # template
        "",          # model
        "",          # type
        "sk-test",   # api key
        "",          # context size
        "",          # max tokens
        "",          # thinking effort
        "",          # capabilities
        "",          # url
        "n",         # no sub provider
    ]
    config = _run_init(monkeypatch, tmp_path, answers)
    assert "sub_provider" not in config
    assert "sub_providers" not in config
