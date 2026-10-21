"""Integration: the real bash tool ignores unsupported args but warns about them.

Reference case from the requirement: bash has no ``work_dir`` parameter. A call
``{"cmd": "echo hi", "work_dir": "."}`` must still run ``echo hi`` and the
result must carry a <system-warning> naming ``work_dir`` as unsupported/ignored.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from kimi_cli.session import Session
from kimi_cli.soul.toolset import KimiToolset
from kimi_cli.tools import SkipThisTool
from kimi_cli.tools.file.bash.bash_tool import Bash, find_bash
from kimi_cli.wire.types import ToolCall, ToolResult

pytestmark = pytest.mark.skipif(find_bash() is None, reason="bash not found on this platform")


def _output_text(return_value: Any) -> str:
    output = return_value.output
    if isinstance(output, str):
        return output
    parts: list[str] = []
    for part in output if isinstance(output, list) else [output]:
        text = getattr(part, "text", None) or getattr(part, "think", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


async def test_bash_runs_and_warns_about_unsupported_work_dir(session: Session) -> None:
    try:
        tool = Bash(session)
    except SkipThisTool:
        pytest.skip("bash tool disabled on this platform")

    ts = KimiToolset()
    ts.add(tool)

    tool_call = ToolCall(
        id="tc-1",
        function=ToolCall.FunctionBody(
            name="bash",
            arguments=json.dumps({"cmd": "echo hi", "work_dir": "."}),
        ),
    )
    result = ts.handle(tool_call)
    assert isinstance(result, asyncio.Task)
    tr: ToolResult = await result

    assert not tr.return_value.is_error
    text = _output_text(tr.return_value)
    assert "hi" in text
    assert "<system-warning>" in text
    assert "`work_dir`" in text
    assert "Ignored unsupported argument(s)" in text
