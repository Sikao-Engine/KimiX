from __future__ import annotations

import io
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import orjson
import pytest
from PIL import Image
from pydantic import ValidationError

from kimi_cli.soul.approval import ApprovalResult
from kimi_cli.tools.computer_use import ComputerUse, Params
from kimi_cli.tools.computer_use.windows import (
    ActionResult,
    CaptureResult,
    ComputerBackendError,
    UIElement,
    WindowsComputerBackend,
)
from kimi_cli.wire.types import TextPart
from kosong.tooling import ToolError, ToolOk


def _approval() -> MagicMock:
    approval = MagicMock()
    approval.request = AsyncMock(return_value=ApprovalResult(True))
    return approval


def _capture(*, with_image: bool = False) -> CaptureResult:
    png_data = None
    if with_image:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), "white").save(buffer, format="PNG")
        png_data = buffer.getvalue()
    element = UIElement(
        index=1,
        role="Button",
        label="Run",
        bounds=(110, 220, 30, 20),
        pid=42,
        window_id=99,
        control=object(),
    )
    return CaptureResult(
        snapshot_id="snapshot-1",
        mode="som" if with_image else "ax",
        width=40,
        height=30,
        app="notepad.exe",
        window_title="Owned test window",
        window_id=99,
        pid=42,
        origin=(100, 200),
        png_data=png_data,
        elements=[element],
    )


def _tool(runtime: Any, session: Any, backend: Any) -> ComputerUse:
    return ComputerUse(runtime, _approval(), session, backend=backend)


def test_action_specific_schema_validation() -> None:
    with pytest.raises(ValidationError, match="requires element or coordinate"):
        Params(action="click")
    with pytest.raises(ValidationError, match="set_value requires"):
        Params(action="set_value", element=1)
    with pytest.raises(ValidationError, match="exactly one"):
        Params(action="drag", from_element=1, from_coordinate=(0, 0), to_element=2)
    with pytest.raises(ValidationError, match="existing sticky target"):
        Params(action="type", text="hello", app="notepad")


def test_key_translation_is_explicit() -> None:
    assert WindowsComputerBackend.to_pywinauto_keys("ctrl+s") == "^s"
    assert WindowsComputerBackend.to_pywinauto_keys("return") == "{ENTER}"
    assert WindowsComputerBackend.to_pywinauto_keys("win+r") == "{LWIN down}r{LWIN up}"
    with pytest.raises(ComputerBackendError, match="Unsupported key"):
        WindowsComputerBackend.to_pywinauto_keys("ctrl+not-a-key")


async def test_ax_capture_returns_relative_element_coordinates(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.capture.return_value = _capture()
    result = await _tool(runtime, session, backend)(Params(action="capture", mode="ax"))
    assert isinstance(result, ToolOk)
    payload = orjson.loads(result.output)
    assert payload["elements"][0]["bounds"] == [10, 20, 30, 20]
    assert "screenshot_path" not in payload


async def test_background_click_has_action_scoped_approval(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.click.return_value = ActionResult("click", "invoked", "uia_invoke")
    tool = _tool(runtime, session, backend)
    result = await tool(Params(action="click", element=1))
    assert isinstance(result, ToolOk)
    assert backend.click.call_args.kwargs["delivery_mode"] == "background"
    tool._approval.request.assert_awaited_once()
    assert tool._approval.request.await_args.args[1] == "computer_use:click:background"


async def test_stale_snapshot_is_an_error_and_not_replayed(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.click.side_effect = ComputerBackendError("stale", "stale_snapshot")
    tool = _tool(runtime, session, backend)
    result = await tool(Params(action="click", element=2, capture_after=True))
    assert isinstance(result, ToolError)
    assert "stale_snapshot" in result.message
    backend.capture_current.assert_not_called()
    assert backend.click.call_count == 1


async def test_successful_action_can_include_fresh_screenshot(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.click.return_value = ActionResult("click", "invoked", "uia_invoke")
    backend.capture_current.return_value = _capture(with_image=True)
    result = await _tool(runtime, session, backend)(
        Params(action="click", element=1, capture_after=True)
    )
    assert isinstance(result, ToolOk)
    assert isinstance(result.output, list)
    text = next(part.text for part in result.output if isinstance(part, TextPart))
    payload = orjson.loads(text.splitlines()[0])
    path = payload["screenshot_path"]
    assert payload["follow_up_capture"]["snapshot_id"] == "snapshot-1"
    assert Image.open(path).size == (40, 30)


def test_foreground_double_click_uses_public_mouse_api_and_releases_modifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pywinauto import keyboard, mouse

    backend = WindowsComputerBackend()
    backend._target = MagicMock()
    backend._target.is_minimized.return_value = False
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    double_click = MagicMock()
    send_keys = MagicMock()
    monkeypatch.setattr(mouse, "double_click", double_click)
    monkeypatch.setattr(keyboard, "send_keys", send_keys)

    backend.click(
        coordinate=(5, 6),
        click_count=2,
        modifiers=["ctrl", "win"],
        delivery_mode="foreground",
    )
    double_click.assert_called_once_with(button="left", coords=(15, 26))
    assert send_keys.call_args_list == [
        call("{LCONTROL down}"),
        call("{LWIN down}"),
        call("{LWIN up}"),
        call("{LCONTROL up}"),
    ]


def test_background_coordinate_click_is_refused() -> None:
    backend = WindowsComputerBackend()
    with pytest.raises(ComputerBackendError) as caught:
        backend.click(coordinate=(1, 1), delivery_mode="background")
    assert caught.value.code == "background_unavailable"


def test_matching_sticky_window_avoids_desktop_enumeration() -> None:
    backend = WindowsComputerBackend()
    window = MagicMock()
    window.handle = 99
    window.process_id.return_value = 42
    window.is_visible.return_value = True
    backend._target = window
    backend._desktop = MagicMock(side_effect=AssertionError("enumeration is not needed"))

    assert backend._resolve_window(pid=42, window_id=99) is window
    backend._desktop.assert_not_called()


def test_changed_window_selector_still_enumerates() -> None:
    backend = WindowsComputerBackend()
    stale = MagicMock()
    stale.handle = 10
    stale.process_id.return_value = 1
    stale.is_visible.return_value = True
    replacement = MagicMock()
    replacement.handle = 99
    replacement.process_id.return_value = 42
    replacement.is_visible.return_value = True
    backend._target = stale
    desktop = MagicMock()
    desktop.windows.return_value = [replacement]
    backend._desktop = MagicMock(return_value=desktop)

    assert backend._resolve_window(pid=42, window_id=99) is replacement


def test_element_label_uses_uia_name_before_expensive_text_patterns() -> None:
    control = MagicMock()
    control.element_info.name = "Run command"
    control.window_text.side_effect = AssertionError("TextPattern should not be probed")

    assert WindowsComputerBackend._label(control) == "Run command"
    control.window_text.assert_not_called()
