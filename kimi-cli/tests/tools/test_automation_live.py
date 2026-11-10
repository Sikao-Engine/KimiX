"""Opt-in live smoke tests for real Windows UI and Chromium automation.

Run explicitly with ``KIMI_RUN_WINDOWS_UI_TESTS=1``.  Tests own every app they
control and never inject input into a pre-existing window or browser profile.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import orjson
import pytest
from PIL import Image

from kimi_cli.soul.approval import ApprovalResult
from kimi_cli.tools.browser_use import BrowserUse
from kimi_cli.tools.browser_use import Params as BrowserParams
from kimi_cli.tools.computer_use import ComputerUse
from kimi_cli.tools.computer_use import Params as ComputerParams
from kimi_cli.wire.types import TextPart
from kosong.tooling import ToolError, ToolOk, ToolReturnValue

pytestmark = pytest.mark.skipif(
    os.environ.get("KIMI_RUN_WINDOWS_UI_TESTS") != "1",
    reason="set KIMI_RUN_WINDOWS_UI_TESTS=1 to run live local UI tests",
)


def _approval() -> MagicMock:
    approval = MagicMock()
    approval.request = AsyncMock(return_value=ApprovalResult(True))
    return approval


def _payload(result: ToolReturnValue) -> dict[str, Any]:
    assert isinstance(result, ToolOk), getattr(result, "message", result)
    if isinstance(result.output, str):
        return orjson.loads(result.output)
    text = next(part.text for part in result.output if isinstance(part, TextPart))
    return orjson.loads(text.splitlines()[0])


@pytest.mark.skipif(os.name != "nt", reason="native Windows UI test")
async def test_live_windows_notepad_capture_set_value_and_screenshot(
    runtime: Any, session: Any
) -> None:
    fixture_file = session.dir / "kimi-owned-ui-smoke-fixture.txt"
    fixture_file.write_text("", encoding="utf-8")
    process = subprocess.Popen(["notepad.exe", str(fixture_file)])
    tool = ComputerUse(runtime, _approval(), session)
    actual_pid: int | None = None
    try:
        capture: ToolReturnValue | None = None
        for _ in range(30):
            # WindowsApps notepad.exe is a launcher; its interactive child has a new PID.
            capture = await tool(
                ComputerParams(action="capture", mode="som", app=fixture_file.name)
            )
            if isinstance(capture, ToolOk):
                break
            await asyncio.sleep(0.25)
        assert capture is not None
        captured = _payload(capture)
        actual_pid = captured["pid"]
        editable = next(
            (item for item in captured["elements"] if item["role"] in {"Document", "Edit"}),
            None,
        )
        assert editable is not None, captured
        result = await tool(
            ComputerParams(
                action="set_value",
                element=editable["element"],
                snapshot_id=captured["snapshot_id"],
                value="Kimi-owned Windows UI smoke test",
            )
        )
        changed = _payload(result)
        assert changed["verified"] is True, changed

        screenshot = _payload(
            await tool(ComputerParams(action="capture", mode="vision", app=fixture_file.name))
        )
        assert Image.open(screenshot["screenshot_path"]).width > 0
    finally:
        if actual_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(actual_pid, signal.SIGTERM)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


async def test_live_chromium_session_snapshot_input_tabs_and_screenshot(
    runtime: Any, session: Any
) -> None:
    tool = BrowserUse(runtime, _approval(), session)
    html = """
        <title>Kimi browser fixture</title>
        <label>Name <input id='name' name='name'></label>
        <input type='password' value='never-expose-this'>
        <button hidden>Hidden</button>
        <button id='go' onclick="document.querySelector('#result').textContent = 'Hello ' + document.querySelector('#name').value">Go</button>
        <div id='result'></div>
    """
    url = "data:text/html," + quote(html)
    try:
        navigated = _payload(
            await tool(BrowserParams(action="navigate", session="live-test", url=url))
        )
        assert navigated["title"] == "Kimi browser fixture"
        assert navigated["url"].startswith("data:text/html,[")
        assert "never-expose-this" not in navigated["url"]
        snapshot = _payload(await tool(BrowserParams(action="snapshot", session="live-test")))
        name = next(item for item in snapshot["elements"] if item.get("name") == "name")
        password = next(
            item for item in snapshot["elements"] if item.get("input_type") == "password"
        )
        assert name["label"] == "Name"
        assert name["input_type"] == "text"
        assert name["role"] == "text"
        assert name["value"] == ""
        assert password["label"] == "[password field]"
        assert password["value"] is None
        assert all(item["label"] != "Hidden" for item in snapshot["elements"])
        filled = _payload(
            await tool(
                BrowserParams(
                    action="fill",
                    session="live-test",
                    element=name["element"],
                    snapshot_id=snapshot["snapshot_id"],
                    text="Windows",
                )
            )
        )
        assert filled["verified"] is True

        snapshot = _payload(await tool(BrowserParams(action="snapshot", session="live-test")))
        name = next(item for item in snapshot["elements"] if item.get("name") == "name")
        assert name["label"] == "Name"
        assert name["value"] == "Windows"
        button = next(item for item in snapshot["elements"] if item["tag"] == "button")
        await tool(
            BrowserParams(
                action="click",
                session="live-test",
                element=button["element"],
                snapshot_id=snapshot["snapshot_id"],
            )
        )
        extracted = _payload(
            await tool(
                BrowserParams(
                    action="extract", session="live-test", selector="#result", attribute="text"
                )
            )
        )
        assert extracted["values"] == ["Hello Windows"]

        screenshot = _payload(await tool(BrowserParams(action="screenshot", session="live-test")))
        assert Image.open(screenshot["screenshot_path"]).width > 0
        await tool(
            BrowserParams(
                action="new_tab",
                session="live-test",
                url="data:text/html,<title>Second</title>",
            )
        )
        tabs = _payload(await tool(BrowserParams(action="list_tabs", session="live-test")))
        assert tabs["tab_count"] == 2
        await tool(
            BrowserParams(
                action="new_tab",
                session="live-test",
                url="data:text/html,<title>Third</title>",
            )
        )
        closed_background = _payload(
            await tool(BrowserParams(action="close_tab", session="live-test", tab=2))
        )
        assert closed_background["title"] == "Third"
        assert closed_background["tab_count"] == 2

        # Simulate page script/user closing only the active tab. The next tool
        # call must recover to the original tab without recreating the context.
        await tool._manager.sessions["live-test"].page.close()
        tabs = _payload(await tool(BrowserParams(action="list_tabs", session="live-test")))
        assert tabs["tab_count"] == 1
        assert tabs["tabs"][0]["title"] == "Kimi browser fixture"
    finally:
        result = await tool(BrowserParams(action="close", session="live-test"))
        assert not isinstance(result, ToolError), result.message
