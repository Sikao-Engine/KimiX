from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from kimix.cli_impl import constants
from kimix.cli_impl.args import set_arg


def test_parse_mcp_serve_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    """``kimix mcp serve --transport stdio`` is parsed correctly."""
    monkeypatch.setattr(sys, "argv", ["kimix", "mcp", "serve", "--transport", "stdio"])

    subcmd, args = set_arg()

    assert subcmd == "mcp"
    assert args.command == "mcp"
    assert args.mcp_command == "serve"
    assert args.transport == "stdio"
    assert args.host == "127.0.0.1"
    assert args.port == 4097


def test_mcp_config_discovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``.kimix/mcp.json`` is discovered and stored on ``args.mcp_config``."""
    config = {"mcpServers": {"example": {"command": "npx", "args": ["-y", "example-mcp"]}}}
    kimix_dir = tmp_path / ".kimix"
    kimix_dir.mkdir()
    (kimix_dir / "mcp.json").write_text(json.dumps(config), encoding="utf-8")

    monkeypatch.setattr(constants, "curr_dir", tmp_path)
    monkeypatch.setattr(sys, "argv", ["kimix", "mcp", "list"])

    subcmd, args = set_arg()

    assert subcmd == "mcp"
    assert args.mcp_command == "list"
    assert args.mcp_config == config
