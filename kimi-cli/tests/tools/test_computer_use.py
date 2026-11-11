from __future__ import annotations

import ctypes
import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import orjson
import pytest
from PIL import Image, ImageGrab
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


def _allow_foreground(monkeypatch: pytest.MonkeyPatch, backend: WindowsComputerBackend) -> None:
    """Reach the post-preflight behavior under test on a headless test host."""
    monkeypatch.setattr(backend, "_require_interactive_desktop", MagicMock())


def test_action_specific_schema_validation() -> None:
    assert "list_apps only groups the same visible windows" in ComputerUse.description
    assert "app='screen', which requires imaging in every mode" in ComputerUse.description
    assert "do not retry foreground delivery in that session" in ComputerUse.description
    with pytest.raises(ValidationError, match="requires element or coordinate"):
        Params(action="click")
    with pytest.raises(ValidationError, match="set_value requires"):
        Params(action="set_value", element=1)
    with pytest.raises(ValidationError, match="exactly one"):
        Params(action="drag", from_element=1, from_coordinate=(0, 0), to_element=2)
    with pytest.raises(ValidationError, match="existing sticky target"):
        Params(action="type", text="hello", app="notepad")
    with pytest.raises(ValidationError, match="existing sticky target"):
        Params(action="key", keys="win", window_id=99)
    with pytest.raises(ValidationError, match="existing sticky target"):
        Params(action="click", element=1, pid=42)
    with pytest.raises(ValidationError, match="focus_app requires app, pid, or window_id"):
        Params(action="focus_app")
    with pytest.raises(ValidationError, match="focus_app requires app, pid, or window_id"):
        Params(action="focus_app", app="")
    assert Params(action="focus_app", window_id=99).app is None


def test_key_translation_is_explicit() -> None:
    assert WindowsComputerBackend.to_pywinauto_keys("ctrl+s") == "^s"
    assert WindowsComputerBackend.to_pywinauto_keys("return") == "{ENTER}"
    assert WindowsComputerBackend.to_pywinauto_keys("win+r") == "{LWIN down}r{LWIN up}"
    assert WindowsComputerBackend.to_pywinauto_keys("win") == "{LWIN down}{LWIN up}"
    with pytest.raises(ComputerBackendError, match="Unsupported key"):
        WindowsComputerBackend.to_pywinauto_keys("ctrl+not-a-key")


async def test_ax_capture_returns_relative_element_coordinates(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.capture.return_value = _capture()
    result = await _tool(runtime, session, backend)(Params(action="capture", mode="ax"))
    assert isinstance(result, ToolOk)
    payload = orjson.loads(result.output)
    assert payload["elements"][0]["bounds"] == [10, 20, 30, 20]
    assert payload["capture_origin"] == [100, 200]
    assert "relative to capture_origin" in payload["coordinate_system"]
    assert "No screenshot is included" in payload["coordinate_system"]
    assert "screenshot_path" not in payload


async def test_focus_app_forwards_exact_window_selectors(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.focus_app.return_value = ActionResult(
        "focus_app", "focused", "uia_window", verified=True, effect="confirmed"
    )

    result = await _tool(runtime, session, backend)(
        Params(action="focus_app", pid=6224, window_id=65850, raise_window=True)
    )

    assert isinstance(result, ToolOk)
    backend.focus_app.assert_called_once_with(None, True, pid=6224, window_id=65850)


async def test_background_click_has_action_scoped_approval(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.click.return_value = ActionResult("click", "invoked", "uia_invoke")
    tool = _tool(runtime, session, backend)
    result = await tool(Params(action="click", element=1))
    assert isinstance(result, ToolOk)
    assert backend.click.call_args.kwargs["delivery_mode"] == "background"
    tool._approval.request.assert_awaited_once()
    assert tool._approval.request.await_args.args[1] == "computer_use:click:background"


async def test_mutation_invalidates_desktop_snapshot_before_reuse(
    runtime: Any, session: Any
) -> None:
    backend = WindowsComputerBackend()
    control = MagicMock()
    control.is_visible.return_value = True
    element = UIElement(
        index=1,
        role="Button",
        label="Run",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    tool = _tool(runtime, session, backend)
    params = Params(action="click", element=1, snapshot_id="fresh")

    first = await tool(params)

    assert isinstance(first, ToolOk)
    assert orjson.loads(first.output)["snapshot_id"] is None
    assert backend._elements == []
    assert backend._snapshot_id == ""
    assert backend._capture_origin == (0, 0)
    assert backend._capture_size == (0, 0)
    second = await tool(params)
    assert isinstance(second, ToolError)
    assert "stale_snapshot" in second.message
    assert "snapshot_required" not in second.message
    assert "intervening action" in second.message
    control.iface_invoke.Invoke.assert_called_once_with()


async def test_inactive_desktop_click_preserves_snapshot_for_background_retry(
    runtime: Any, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pywinauto import mouse

    backend = WindowsComputerBackend()
    _allow_foreground(monkeypatch, backend)
    control = MagicMock()
    control.is_visible.return_value = True
    element = UIElement(
        index=1,
        role="Button",
        label="Start",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    monkeypatch.setattr(
        mouse,
        "click",
        MagicMock(
            side_effect=RuntimeError("There is no active desktop required for moving mouse cursor!")
        ),
    )
    tool = _tool(runtime, session, backend)

    failed = await tool(
        Params(
            action="click",
            element=1,
            snapshot_id="fresh",
            delivery_mode="foreground",
        )
    )

    assert isinstance(failed, ToolError)
    assert "no_interactive_desktop" in failed.message
    assert "snapshot remains valid" in failed.message
    assert backend._elements == [element]
    assert backend._snapshot_id == "fresh"
    assert backend._capture_origin == (10, 20)
    assert backend._capture_size == (100, 100)

    # This is the exact retry from the live transcript, now without a redundant
    # recapture between the failed foreground attempt and background UIA.
    retried = await tool(Params(action="click", element=1, snapshot_id="fresh"))
    assert isinstance(retried, ToolOk)
    control.iface_invoke.Invoke.assert_called_once_with()
    assert backend._snapshot_id == ""


async def test_inactive_desktop_type_preflight_preserves_snapshot_and_skips_focus(
    runtime: Any, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pywinauto import keyboard

    backend = WindowsComputerBackend()
    backend._target = MagicMock()
    backend._snapshot_id = "fresh"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    user32 = MagicMock()
    user32.OpenInputDesktop.return_value = 0
    monkeypatch.setattr(ctypes.windll, "user32", user32)
    send_keys = MagicMock()
    monkeypatch.setattr(keyboard, "send_keys", send_keys)

    failed = await _tool(runtime, session, backend)(
        Params(action="type", text="notepad", delivery_mode="foreground")
    )

    assert isinstance(failed, ToolError)
    assert "no_interactive_desktop" in failed.message
    assert "snapshot remains valid" in failed.message
    assert "do not try another foreground action" in failed.message
    backend._target.set_focus.assert_not_called()
    send_keys.assert_not_called()
    user32.CloseDesktop.assert_not_called()
    assert backend._snapshot_id == "fresh"
    assert backend._capture_origin == (10, 20)
    assert backend._capture_size == (100, 100)


def test_interactive_desktop_probe_closes_read_only_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user32 = MagicMock()
    user32.OpenInputDesktop.return_value = 123
    monkeypatch.setattr(ctypes.windll, "user32", user32)

    WindowsComputerBackend._require_interactive_desktop()

    user32.OpenInputDesktop.assert_called_once_with(0, False, 0x0001)
    user32.CloseDesktop.assert_called_once_with(123)


async def test_background_unavailable_preserves_snapshot_for_supported_action(
    runtime: Any, session: Any
) -> None:
    backend = WindowsComputerBackend()
    control = MagicMock()
    control.is_visible.return_value = True
    element = UIElement(
        index=1,
        role="Button",
        label="Start",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    tool = _tool(runtime, session, backend)

    refused = await tool(Params(action="key", keys="win", delivery_mode="background"))

    assert isinstance(refused, ToolError)
    assert "background_unavailable" in refused.message
    assert "snapshot remains valid" in refused.message
    assert backend._elements == [element]
    assert backend._snapshot_id == "fresh"
    assert backend._capture_origin == (10, 20)
    assert backend._capture_size == (100, 100)

    supported = await tool(Params(action="click", element=1, snapshot_id="fresh"))
    assert isinstance(supported, ToolOk)
    control.iface_invoke.Invoke.assert_called_once_with()
    assert backend._snapshot_id == ""


async def test_modified_inactive_desktop_click_still_invalidates_snapshot(
    runtime: Any, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pywinauto import keyboard, mouse

    backend = WindowsComputerBackend()
    _allow_foreground(monkeypatch, backend)
    control = MagicMock()
    control.is_visible.return_value = True
    backend._elements = [
        UIElement(
            index=1,
            role="Button",
            label="Start",
            bounds=(0, 0, 40, 40),
            pid=42,
            window_id=99,
            control=control,
        )
    ]
    backend._snapshot_id = "fresh"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    monkeypatch.setattr(keyboard, "send_keys", MagicMock())
    monkeypatch.setattr(
        mouse,
        "click",
        MagicMock(
            side_effect=RuntimeError("There is no active desktop required for moving mouse cursor!")
        ),
    )

    result = await _tool(runtime, session, backend)(
        Params(
            action="click",
            element=1,
            snapshot_id="fresh",
            delivery_mode="foreground",
            modifiers=["ctrl"],
        )
    )

    assert isinstance(result, ToolError)
    assert "no_interactive_desktop" in result.message
    assert backend._elements == []
    assert backend._snapshot_id == ""
    assert backend._capture_origin == (0, 0)
    assert backend._capture_size == (0, 0)


async def test_stale_snapshot_is_an_error_and_not_replayed(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.click.side_effect = ComputerBackendError("stale", "stale_snapshot")
    tool = _tool(runtime, session, backend)
    result = await tool(Params(action="click", element=2, capture_after=True))
    assert isinstance(result, ToolError)
    assert "stale_snapshot" in result.message
    backend.capture_current.assert_not_called()
    backend.invalidate_snapshot.assert_called_once_with()
    assert backend.click.call_count == 1


@pytest.mark.parametrize("mode", ["som", "vision"])
def test_image_capture_failure_degrades_to_accessibility_snapshot(
    mode: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = WindowsComputerBackend()
    window = MagicMock()
    window.handle = 99
    window.process_id.return_value = 42
    monkeypatch.setattr(backend, "_resolve_window", MagicMock(return_value=window))
    monkeypatch.setattr(backend, "_rectangle", MagicMock(return_value=(10, 20, 110, 120)))
    monkeypatch.setattr(backend, "_collect_elements", MagicMock(return_value=([], False)))
    monkeypatch.setattr(backend, "_process_name", MagicMock(return_value="notepad.exe"))
    monkeypatch.setattr(backend, "_title", MagicMock(return_value="Fixture"))
    monkeypatch.setattr(ImageGrab, "grab", MagicMock(side_effect=OSError("screen grab failed")))

    capture = backend.capture(mode=mode)
    summary = capture.summary()

    assert capture.mode == "ax"
    assert capture.png_data is None
    assert summary["requested_mode"] == mode
    assert summary["degraded"] is True
    assert "accessibility elements only" in summary["warnings"][0]
    assert "app='screen' requires image capture in every mode" in summary["next_step"]
    assert "list_apps only groups" in summary["next_step"]


def test_full_screen_capture_failure_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WindowsComputerBackend()
    monkeypatch.setattr(ImageGrab, "grab", MagicMock(side_effect=OSError("screen grab failed")))

    with pytest.raises(ComputerBackendError) as caught:
        backend.capture(app="screen")

    assert caught.value.code == "screen_capture_unavailable"
    assert "including mode='ax'" in str(caught.value)
    assert "list_windows once" in str(caught.value)


def test_missing_window_error_discourages_redundant_discovery() -> None:
    backend = WindowsComputerBackend()
    desktop = MagicMock()
    desktop.windows.return_value = []
    backend._desktop = MagicMock(return_value=desktop)

    with pytest.raises(ComputerBackendError) as caught:
        backend._resolve_window(app="missing.exe")

    assert caught.value.code == "window_not_found"
    assert "do not repeat equivalent discovery calls" in str(caught.value).lower()
    assert "omit app/pid/window_id" in str(caught.value)
    assert "sticky target and any existing snapshot are unchanged" in str(caught.value)


async def test_repeated_window_listing_explicitly_reports_no_change(
    runtime: Any, session: Any
) -> None:
    backend = MagicMock()
    windows = [{"window_id": 99, "title": "Fixture", "pid": 42}]
    backend.list_windows.side_effect = [windows, windows, []]
    tool = _tool(runtime, session, backend)

    first = await tool(Params(action="list_windows"))
    second = await tool(Params(action="list_windows"))
    changed = await tool(Params(action="list_windows"))

    assert isinstance(first, ToolOk)
    assert "unchanged_from_previous" not in orjson.loads(first.output)
    repeated = orjson.loads(second.output)
    assert repeated["unchanged_from_previous"] is True
    assert repeated["intervening_activity_count"] == 0
    assert "no intervening desktop action or wait" in repeated["next_step"]
    assert "choose a listed target or report" in repeated["next_step"]
    assert orjson.loads(changed.output)["unchanged_from_previous"] is False


async def test_window_listing_distinguishes_ineffective_intervening_action(
    runtime: Any, session: Any
) -> None:
    backend = MagicMock()
    windows = [{"window_id": 99, "title": "Fixture", "pid": 42}]
    backend.list_windows.return_value = windows
    backend.click.return_value = ActionResult("click", "invoked", "uia_invoke")
    tool = _tool(runtime, session, backend)

    await tool(Params(action="list_windows"))
    action = await tool(Params(action="click", element=1))
    repeated = await tool(Params(action="list_windows"))

    assert isinstance(action, ToolOk)
    assert orjson.loads(action.output)["snapshot_id"] is None
    payload = orjson.loads(repeated.output)
    assert payload["unchanged_from_previous"] is True
    assert payload["intervening_activity_count"] == 1
    assert "unchanged despite 1 completed desktop action" in payload["next_step"]
    assert "did not produce a newly discoverable window" in payload["next_step"]
    assert "Do not repeat the same action/discovery cycle" in payload["next_step"]


async def test_list_apps_is_explicitly_not_a_new_discovery_path(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.list_apps.return_value = [{"pid": 42, "app": "fixture.exe"}]

    result = await _tool(runtime, session, backend)(Params(action="list_apps"))

    assert isinstance(result, ToolOk)
    payload = orjson.loads(result.output)
    assert "only regroups currently visible top-level windows" in payload["discovery_scope"]
    assert (
        "cannot reveal targets or processes omitted by list_windows" in payload["discovery_scope"]
    )


async def test_repeated_app_listing_reports_redundant_derived_discovery(
    runtime: Any, session: Any
) -> None:
    backend = MagicMock()
    apps = [{"pid": 42, "app": "fixture.exe", "window_ids": [99]}]
    backend.list_apps.return_value = apps
    backend.click.return_value = ActionResult("click", "invoked", "uia_invoke")
    tool = _tool(runtime, session, backend)

    await tool(Params(action="list_apps"))
    await tool(Params(action="click", element=1))
    repeated = await tool(Params(action="list_apps"))
    polled = await tool(Params(action="list_apps"))

    payload = orjson.loads(repeated.output)
    assert payload["unchanged_from_previous"] is True
    assert payload["intervening_activity_count"] == 1
    assert "derived app list is unchanged despite 1" in payload["next_step"]
    assert "cannot reveal targets or processes omitted by list_windows" in payload["next_step"]
    assert "Do not repeat app discovery" in payload["next_step"]
    polled_payload = orjson.loads(polled.output)
    assert polled_payload["intervening_activity_count"] == 0
    assert "no intervening desktop action or wait" in polled_payload["next_step"]
    assert "Do not repeat list_apps" in polled_payload["next_step"]


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
    assert payload["snapshot_id"] == "snapshot-1"
    assert payload["follow_up_capture"]["snapshot_id"] == "snapshot-1"
    assert "already included in follow_up_capture" in payload["next_step"]
    assert "use its snapshot_id" in payload["next_step"]
    assert not payload["next_step"].startswith("Capture fresh state")
    assert Image.open(path).size == (40, 30)


async def test_follow_up_capture_failure_does_not_hide_action_success(
    runtime: Any, session: Any
) -> None:
    backend = MagicMock()
    backend.click.return_value = ActionResult("click", "invoked", "uia_invoke")
    backend.capture_current.side_effect = OSError("screen grab failed")

    result = await _tool(runtime, session, backend)(
        Params(action="click", element=1, capture_after=True)
    )

    assert isinstance(result, ToolOk)
    payload = orjson.loads(result.output)
    assert payload["ok"] is True
    assert payload["action"] == "click"
    assert payload["verdict"] == "verify_fresh_state"
    assert "screen grab failed" in payload["follow_up_capture_error"]


def test_foreground_double_click_uses_public_mouse_api_and_releases_modifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pywinauto import keyboard, mouse

    backend = WindowsComputerBackend()
    _allow_foreground(monkeypatch, backend)
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
    backend._target.set_focus.assert_not_called()
    assert send_keys.call_args_list == [
        call("{LCONTROL down}"),
        call("{LWIN down}"),
        call("{LWIN up}"),
        call("{LCONTROL up}"),
    ]


async def test_no_active_desktop_is_a_specific_input_error(runtime: Any, session: Any) -> None:
    backend = MagicMock()
    backend.click.side_effect = RuntimeError(
        "There is no active desktop required for moving mouse cursor!"
    )

    result = await _tool(runtime, session, backend)(
        Params(action="click", element=1, delivery_mode="foreground")
    )

    assert isinstance(result, ToolError)
    assert "no_interactive_desktop" in result.message
    assert "outcome_unknown" not in result.message


def test_virtual_uia_element_without_hwnd_remains_actionable() -> None:
    backend = WindowsComputerBackend()
    control = MagicMock()
    control.element_info.handle = None
    control.is_visible.return_value = True
    element = UIElement(
        index=1,
        role="Button",
        label="Start",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"

    assert backend._element(1, "fresh") is element

    control.is_visible.return_value = False
    with pytest.raises(ComputerBackendError) as caught:
        backend._element(1, "fresh")
    assert caught.value.code == "stale_element"


def test_background_button_click_prefers_invoke_when_supported() -> None:
    backend = WindowsComputerBackend()
    control = MagicMock()
    control.is_visible.return_value = True
    element = UIElement(
        index=1,
        role="Button",
        label="Run",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"

    result = backend.click(element=1, snapshot_id="fresh")

    control.iface_invoke.Invoke.assert_called_once_with()
    control.toggle.assert_not_called()
    assert result.path == "uia_invoke"


def test_background_button_click_uses_toggle_when_invoke_is_unsupported() -> None:
    backend = WindowsComputerBackend()
    control = MagicMock(spec=["is_visible", "toggle", "get_toggle_state"])
    control.is_visible.return_value = True
    control.get_toggle_state.side_effect = [False, True]
    element = UIElement(
        index=1,
        role="Button",
        label="Start",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"

    result = backend.click(element=1, snapshot_id="fresh")

    control.toggle.assert_called_once_with()
    assert result.path == "uia_toggle"
    assert result.verified is None
    assert "visible UI outcome remains unverified" in result.message
    assert result.meta["toggle_state_before"] is False
    assert result.meta["toggle_state_after"] is True
    assert result.summary()["verdict"] == "verify_fresh_state"
    assert "not proof of a visible UI transition" in result.summary()["next_step"]
    assert "omit app/pid/window_id" in result.summary()["next_step"]


def test_checkbox_toggle_state_change_is_verified() -> None:
    backend = WindowsComputerBackend()
    control = MagicMock(spec=["is_visible", "toggle", "get_toggle_state"])
    control.is_visible.return_value = True
    control.get_toggle_state.side_effect = [False, True]
    element = UIElement(
        index=1,
        role="CheckBox",
        label="Enabled",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"

    result = backend.click(element=1, snapshot_id="fresh")

    assert result.verified is True
    assert result.effect == "confirmed"
    assert result.summary()["verdict"] == "done"


def test_background_toggle_reports_unchanged_uia_state() -> None:
    backend = WindowsComputerBackend()
    control = MagicMock(spec=["is_visible", "toggle", "get_toggle_state"])
    control.is_visible.return_value = True
    control.get_toggle_state.side_effect = [False, False]
    element = UIElement(
        index=1,
        role="Button",
        label="Start",
        bounds=(0, 0, 40, 40),
        pid=42,
        window_id=99,
        control=control,
    )
    backend._elements = [element]
    backend._snapshot_id = "fresh"

    result = backend.click(element=1, snapshot_id="fresh")
    summary = result.summary()

    control.toggle.assert_called_once_with()
    assert result.verified is False
    assert summary["toggle_state_before"] is False
    assert summary["toggle_state_after"] is False
    assert "did not change" in summary["message"]
    assert "instead of repeating this toggle" in summary["next_step"]


def test_background_coordinate_click_is_refused() -> None:
    backend = WindowsComputerBackend()
    with pytest.raises(ComputerBackendError) as caught:
        backend.click(coordinate=(1, 1), delivery_mode="background")
    assert caught.value.code == "background_unavailable"


def test_stale_coordinate_snapshot_is_rejected_before_foreground_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WindowsComputerBackend()
    backend._snapshot_id = "new"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    foreground = MagicMock()
    monkeypatch.setattr(backend, "_foreground_target", foreground)

    with pytest.raises(ComputerBackendError) as caught:
        backend.scroll("down", 3, (5, 6), snapshot_id="old")
    assert caught.value.code == "stale_snapshot"
    assert caught.value.preserve_snapshot is True
    foreground.assert_not_called()

    with pytest.raises(ComputerBackendError) as caught:
        backend.drag((5, 6), (7, 8), 0.2, snapshot_id="old")
    assert caught.value.code == "stale_snapshot"
    foreground.assert_not_called()


async def test_stale_coordinate_request_preserves_newer_snapshot(
    runtime: Any, session: Any
) -> None:
    backend = WindowsComputerBackend()
    backend._snapshot_id = "new"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)

    result = await _tool(runtime, session, backend)(
        Params(
            action="click",
            coordinate=(5, 6),
            snapshot_id="old",
            delivery_mode="foreground",
        )
    )

    assert isinstance(result, ToolError)
    assert "stale_snapshot" in result.message
    assert "any existing current snapshot remains valid" in result.message
    assert backend._snapshot_id == "new"
    assert backend._capture_origin == (10, 20)
    assert backend._capture_size == (100, 100)


async def test_stale_request_without_snapshot_has_conditional_preservation_message(
    runtime: Any, session: Any
) -> None:
    backend = WindowsComputerBackend()

    result = await _tool(runtime, session, backend)(
        Params(
            action="click",
            coordinate=(5, 6),
            snapshot_id="invented",
            delivery_mode="foreground",
        )
    )

    assert isinstance(result, ToolError)
    assert "stale_snapshot" in result.message
    assert "any existing current snapshot remains valid" in result.message
    assert "the current snapshot remains valid" not in result.message


def test_stale_foreground_element_is_rejected_before_focus() -> None:
    backend = WindowsComputerBackend()
    backend._snapshot_id = "new"
    backend._elements = []
    backend._target = MagicMock()

    with pytest.raises(ComputerBackendError) as caught:
        backend.type_text("text", element=1, delivery_mode="foreground", snapshot_id="old")

    assert caught.value.code == "stale_snapshot"
    backend._target.set_focus.assert_not_called()


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


def test_focus_app_resolves_all_supplied_window_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WindowsComputerBackend()
    _allow_foreground(monkeypatch, backend)
    window = MagicMock()
    window.handle = 65850
    window.process_id.return_value = 6224
    resolve = MagicMock(return_value=window)
    monkeypatch.setattr(backend, "_resolve_window", resolve)
    monkeypatch.setattr(backend, "_foreground_target", MagicMock())

    result = backend.focus_app(None, True, pid=6224, window_id=65850)

    resolve.assert_called_once_with(app=None, pid=6224, window_id=65850)
    backend._foreground_target.assert_called_once_with(desktop_prechecked=True)
    assert result.verified is True
    assert result.meta["window_id"] == 65850


def test_focus_app_preflight_preserves_previous_target_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WindowsComputerBackend()
    previous_target = MagicMock()
    replacement = MagicMock()
    backend._target = previous_target
    backend._snapshot_id = "fresh"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    monkeypatch.setattr(backend, "_resolve_window", MagicMock(return_value=replacement))
    user32 = MagicMock()
    user32.OpenInputDesktop.return_value = 0
    monkeypatch.setattr(ctypes.windll, "user32", user32)

    with pytest.raises(ComputerBackendError) as caught:
        backend.focus_app(None, True, window_id=99)

    assert caught.value.code == "no_interactive_desktop"
    assert caught.value.preserve_snapshot is True
    assert backend._target is previous_target
    assert backend._snapshot_id == "fresh"
    assert backend._capture_origin == (10, 20)
    assert backend._capture_size == (100, 100)
    replacement.set_focus.assert_not_called()


def test_focus_failure_preserves_exception_type_and_empty_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WindowsComputerBackend()
    _allow_foreground(monkeypatch, backend)
    backend._target = MagicMock()
    backend._target.is_minimized.return_value = False
    backend._target.set_focus.side_effect = RuntimeError()

    with pytest.raises(ComputerBackendError) as caught:
        backend._foreground_target()

    assert caught.value.code == "focus_failed"
    assert "RuntimeError: Windows returned no error details" in str(caught.value)


def test_foreground_target_defers_shell_minimization_to_pywinauto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = WindowsComputerBackend()
    _allow_foreground(monkeypatch, backend)
    backend._target = MagicMock()
    backend._target.handle = 99
    backend._target.is_minimized.side_effect = RuntimeError("redundant probe must not run")
    monkeypatch.setattr(backend, "_foreground_window_id", MagicMock(return_value=99))

    backend._foreground_target()

    backend._target.set_focus.assert_called_once_with()
    backend._target.is_minimized.assert_not_called()


def test_keyboard_input_requires_the_actual_foreground_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pywinauto import keyboard

    backend = WindowsComputerBackend()
    _allow_foreground(monkeypatch, backend)
    backend._target = MagicMock()
    backend._target.handle = 99
    backend._target.is_minimized.return_value = False
    foreground = MagicMock(return_value=0)
    monkeypatch.setattr(backend, "_foreground_window_id", foreground)
    send_keys = MagicMock()
    monkeypatch.setattr(keyboard, "send_keys", send_keys)

    with pytest.raises(ComputerBackendError) as caught:
        backend.key("a")

    assert caught.value.code == "focus_failed"
    assert "no active foreground window" in str(caught.value)
    assert "No keyboard input was sent" in str(caught.value)
    send_keys.assert_not_called()

    foreground.return_value = 99
    backend._foreground_target()


async def test_zero_event_sendinput_is_blocked_input_not_unknown(
    runtime: Any, session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pywinauto import keyboard

    backend = WindowsComputerBackend()
    monkeypatch.setattr(backend, "_foreground_target", MagicMock())
    backend._snapshot_id = "fresh"
    backend._capture_origin = (10, 20)
    backend._capture_size = (100, 100)
    send_keys = MagicMock(
        side_effect=RuntimeError("SendInput() inserted only 0 out of 2 keyboard events")
    )
    monkeypatch.setattr(keyboard, "send_keys", send_keys)

    result = await _tool(runtime, session, backend)(
        Params(action="type", text="notepad", delivery_mode="foreground")
    )

    assert isinstance(result, ToolError)
    assert "input_blocked" in result.message
    assert "outcome_unknown" not in result.message
    assert "No keyboard events were inserted" in result.message
    assert "instead of retrying foreground keyboard delivery" in result.message
    send_keys.assert_called_once()
    assert backend._snapshot_id == ""
    assert backend._capture_origin == (0, 0)
    assert backend._capture_size == (0, 0)


def test_window_listing_omits_visible_windows_with_empty_bounds() -> None:
    backend = WindowsComputerBackend()
    window = MagicMock()
    window.is_visible.return_value = True
    window.window_text.return_value = "Empty window"
    window.rectangle.return_value = SimpleNamespace(left=0, top=0, right=0, bottom=0)
    desktop = MagicMock()
    desktop.windows.return_value = [window]
    backend._desktop = MagicMock(return_value=desktop)

    assert backend.list_windows() == []


def test_element_label_uses_uia_name_before_expensive_text_patterns() -> None:
    control = MagicMock()
    control.element_info.name = "Run command"
    control.window_text.side_effect = AssertionError("TextPattern should not be probed")

    assert WindowsComputerBackend._label(control) == "Run command"
    control.window_text.assert_not_called()
