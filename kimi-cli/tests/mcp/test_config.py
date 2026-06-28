from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from fastmcp.mcp_config import MCPConfig

from kimi_cli.mcp.config import (
    discover_mcp_configs,
    load_global_mcp_config,
    load_project_mcp_config,
    merge_mcp_configs,
)


@pytest.fixture
def home_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("KIMI_SHARE_DIR", str(tmp_path))
    return tmp_path


def test_load_global_mcp_config_returns_empty_when_missing(home_dir: Path) -> None:
    assert load_global_mcp_config() == {"mcpServers": {}}


def test_load_global_mcp_config_reads_file(home_dir: Path) -> None:
    config = home_dir / "mcp.json"
    config.write_text(
        orjson.dumps({"mcpServers": {"test": {"command": "echo", "args": ["hi"]}}}).decode(),
        encoding="utf-8",
    )
    result = load_global_mcp_config()
    assert result["mcpServers"]["test"]["command"] == "echo"


def test_load_project_mcp_config_reads_kimix_mcp_json(tmp_path: Path) -> None:
    kimix_dir = tmp_path / ".kimix"
    kimix_dir.mkdir()
    config = kimix_dir / "mcp.json"
    config.write_text(
        orjson.dumps({"mcpServers": {"proj": {"url": "https://example.com/mcp"}}}).decode(),
        encoding="utf-8",
    )
    result = load_project_mcp_config(tmp_path)
    assert result["mcpServers"]["proj"]["url"] == "https://example.com/mcp"


def test_merge_mcp_configs_priority_explicit_over_project_over_global() -> None:
    global_cfg = {"mcpServers": {"a": {"command": "global"}, "b": {"command": "global-b"}}}
    project_cfg = {"mcpServers": {"a": {"command": "project"}, "c": {"command": "project-c"}}}
    explicit = [{"mcpServers": {"a": {"command": "explicit"}}}]

    merged = merge_mcp_configs(global_cfg, project_cfg, explicit)
    assert merged["mcpServers"]["a"]["command"] == "explicit"
    assert merged["mcpServers"]["b"]["command"] == "global-b"
    assert merged["mcpServers"]["c"]["command"] == "project-c"


def test_discover_mcp_configs_merges_global_and_project(home_dir: Path, tmp_path: Path) -> None:
    global_config = home_dir / "mcp.json"
    global_config.write_text(
        orjson.dumps({"mcpServers": {"global-server": {"command": "global-cmd"}}}).decode(),
        encoding="utf-8",
    )

    kimix_dir = tmp_path / ".kimix"
    kimix_dir.mkdir()
    project_config = kimix_dir / "mcp.json"
    project_config.write_text(
        orjson.dumps(
            {"mcpServers": {"project-server": {"url": "https://example.com/mcp"}}}
        ).decode(),
        encoding="utf-8",
    )

    configs = discover_mcp_configs(tmp_path)
    assert len(configs) == 1
    validated = configs[0]
    assert isinstance(validated, MCPConfig)
    assert "global-server" in validated.mcpServers
    assert "project-server" in validated.mcpServers


def test_discover_mcp_configs_returns_empty_when_no_servers(home_dir: Path, tmp_path: Path) -> None:
    assert discover_mcp_configs(tmp_path) == []
