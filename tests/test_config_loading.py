from __future__ import annotations

import orjson

import kimix.base as base
from kimix.utils.config import _create_config


def test_create_config_reads_share_default_config(tmp_path, monkeypatch):
    share_dir = tmp_path / ".kimi"
    share_dir.mkdir()
    (share_dir / "kimix_default_config.json").write_bytes(
        orjson.dumps(
            {
                "model_name": "test-model",
                "name": "test-provider",
                "model": "test-model-id",
                "max_context_size": 8192,
                "capabilities": ["thinking"],
                "url": "https://example.invalid/v1",
                "type": "openai_legacy",
                "api_key": "test-key",
            }
        )
    )
    monkeypatch.setenv("KIMI_SHARE_DIR", str(share_dir))
    monkeypatch.setattr(base, "_default_provider", None)

    cfg, provider_dict = _create_config(None)

    assert provider_dict is not None
    assert provider_dict["model_name"] == "test-model"
    assert cfg.default_model == "test-model"
    assert list(cfg.models) == ["test-model"]
    assert list(cfg.providers) == ["test-provider"]
    assert cfg.providers["test-provider"].api_key.get_secret_value() == "test-key"
