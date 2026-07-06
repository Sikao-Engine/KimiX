"""Tests for kimi_cli.mcp.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from kimi_cli.mcp.config import load_project_mcp_config


def test_load_project_mcp_config_requires_work_dir():
    """load_project_mcp_config must not fall back to Path.cwd()."""
    with pytest.raises(ValueError, match="work_dir is required"):
        load_project_mcp_config()


def test_load_project_mcp_config_uses_provided_work_dir(tmp_path: Path):
    """Project config must be loaded relative to the provided work_dir."""
    config_dir = tmp_path / ".kimix"
    config_dir.mkdir()
    config_file = config_dir / "mcp.json"
    config_file.write_text('{"mcpServers": {"test": {"command": "test"}}}', encoding="utf-8")

    result = load_project_mcp_config(tmp_path)
    assert result == {"mcpServers": {"test": {"command": "test"}}}


def test_load_project_mcp_config_missing_file(tmp_path: Path):
    """Missing project config should return an empty servers dict."""
    result = load_project_mcp_config(tmp_path)
    assert result == {"mcpServers": {}}
