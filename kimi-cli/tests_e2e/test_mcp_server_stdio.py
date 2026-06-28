from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from typing import Any

from tests_e2e.wire_helpers import (
    LineReader,
    make_env,
    make_home_dir,
    make_work_dir,
    repo_root,
)


def _start_mcp_server(
    *,
    home_dir: Path,
    work_dir: Path,
) -> tuple[subprocess.Popen[str], LineReader]:
    cmd = [
        "uv",
        "run",
        "kimix",
        "mcp",
        "serve",
        "--transport",
        "stdio",
        "--work-dir",
        str(work_dir),
    ]
    process = subprocess.Popen(
        cmd,
        cwd=repo_root(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=make_env(home_dir),
    )
    assert process.stdout is not None
    reader = LineReader(process.stdout)
    return process, reader


def _send_json(
    process: subprocess.Popen[str], reader: LineReader, payload: dict[str, Any]
) -> dict[str, Any]:
    assert process.stdin is not None
    line = json.dumps(payload)
    process.stdin.write(line + "\n")
    process.stdin.flush()

    deadline = 10.0
    while True:
        response_line = reader.read_line(timeout=deadline)
        if response_line is None:
            raise EOFError("MCP server closed output stream")
        response_line = response_line.strip()
        if not response_line:
            continue
        try:
            msg = json.loads(response_line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == payload.get("id"):
            return msg  # type: ignore[no-any-return]


def _close(process: subprocess.Popen[str], reader: LineReader) -> None:
    if process.stdin is not None:
        with contextlib.suppress(Exception):
            process.stdin.close()
    reader.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_kimix_mcp_serve_stdio_lists_tools_and_resources(tmp_path: Path) -> None:
    home_dir = make_home_dir(tmp_path)
    work_dir = make_work_dir(tmp_path)
    agents_md = work_dir / "AGENTS.md"
    agents_md.write_text("# Agent rules for MCP test", encoding="utf-8")
    test_file = work_dir / "test.txt"
    test_file.write_text("hello from mcp resource", encoding="utf-8")

    process, reader = _start_mcp_server(
        home_dir=home_dir,
        work_dir=work_dir,
    )
    try:
        init = _send_json(
            process,
            reader,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.1.0"},
                },
            },
        )
        assert init["result"]["serverInfo"]["name"] == "kimix"

        # notifications/initialized has no response
        assert process.stdin is not None
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        process.stdin.flush()

        tools = _send_json(
            process,
            reader,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        assert "ReadFile" in tool_names
        assert "Shell" in tool_names

        resources = _send_json(
            process,
            reader,
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        )
        resource_uris = {res["uri"] for res in resources["result"]["resources"]}
        assert any("AGENTS.md" in uri for uri in resource_uris)

        prompts = _send_json(
            process,
            reader,
            {"jsonrpc": "2.0", "id": 4, "method": "prompts/list"},
        )
        prompt_names = {p["name"] for p in prompts["result"]["prompts"]}
        assert "system" in prompt_names

        templates = _send_json(
            process,
            reader,
            {"jsonrpc": "2.0", "id": 5, "method": "resources/templates/list"},
        )
        template_uris = {t["uriTemplate"] for t in templates["result"]["resourceTemplates"]}
        assert any("{path}" in uri for uri in template_uris)

        resource_read = _send_json(
            process,
            reader,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": f"file:///{test_file.name}"},
            },
        )
        contents = resource_read["result"]["contents"]
        assert any("hello from mcp resource" in c.get("text", "") for c in contents)
    finally:
        _close(process, reader)
