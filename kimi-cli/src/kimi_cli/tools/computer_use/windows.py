"""Windows desktop automation backend built on UI Automation and Win32 input.

The backend keeps a sticky window target and a snapshot-scoped element map.
Read-only UIA patterns are preferred for background actions.  Raw mouse and
keyboard input is deliberately foreground-only: Win32 cannot guarantee that a
coordinate click or synthetic key reached its intended semantic target.
"""

from __future__ import annotations

import ctypes
import io
import os
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import regex as re
from PIL import Image, ImageDraw, ImageGrab

CaptureMode = Literal["som", "vision", "ax"]

_INTERACTIVE_ROLES = frozenset(
    {
        "Button",
        "CheckBox",
        "ComboBox",
        "DataItem",
        "Document",
        "Edit",
        "Hyperlink",
        "List",
        "ListItem",
        "Menu",
        "MenuBar",
        "MenuItem",
        "RadioButton",
        "ScrollBar",
        "Slider",
        "Spinner",
        "SplitButton",
        "Tab",
        "TabItem",
        "Text",
        "ToolBar",
        "Tree",
        "TreeItem",
    }
)
_INVOKE_ROLES = frozenset({"Button", "Hyperlink", "MenuItem", "SplitButton"})
_SELECT_ROLES = frozenset({"ListItem", "RadioButton", "TabItem", "TreeItem"})
_NO_INTERACTIVE_DESKTOP_MESSAGE = (
    "Windows foreground input is unavailable because this process has no active interactive "
    "desktop. No input was sent and the current snapshot remains valid; do not try another "
    "foreground action in this session. Use supported UIA background actions or run from an "
    "interactive desktop session."
)


class ComputerBackendError(RuntimeError):
    """An expected desktop error with a stable machine-readable code."""

    def __init__(
        self,
        message: str,
        code: str = "desktop_error",
        *,
        preserve_snapshot: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        # Opt in only when the backend can prove that no input or UI mutation
        # occurred. background_unavailable is reserved for delivery/capability
        # validation before an action; every other failure defaults to conservative.
        self.preserve_snapshot = preserve_snapshot or code == "background_unavailable"


@dataclass(slots=True)
class UIElement:
    index: int
    role: str
    label: str
    bounds: tuple[int, int, int, int]
    pid: int
    window_id: int
    control: Any = field(repr=False)

    def as_dict(self, origin: tuple[int, int]) -> dict[str, Any]:
        x, y, width, height = self.bounds
        return {
            "element": self.index,
            "role": self.role,
            "label": self.label[:160],
            "bounds": [x - origin[0], y - origin[1], width, height],
            "pid": self.pid,
            "window_id": self.window_id,
        }


@dataclass(slots=True)
class CaptureResult:
    snapshot_id: str
    mode: CaptureMode
    width: int
    height: int
    app: str
    window_title: str
    window_id: int
    pid: int
    origin: tuple[int, int]
    png_data: bytes | None
    elements: list[UIElement]
    truncated: bool = False
    requested_mode: CaptureMode | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "action": "capture",
            "mode": self.mode,
            "snapshot_id": self.snapshot_id,
            "app": self.app,
            "window_title": self.window_title,
            "window_id": self.window_id,
            "pid": self.pid,
            "capture_origin": list(self.origin),
            "width": self.width,
            "height": self.height,
            "element_count": len(self.elements),
            "elements": [element.as_dict(self.origin) for element in self.elements[:100]],
            "truncated": self.truncated or len(self.elements) > 100,
        }
        if self.requested_mode is not None and self.requested_mode != self.mode:
            result["requested_mode"] = self.requested_mode
            result["degraded"] = True
            result["next_step"] = (
                "Continue with mode='ax' on a specific window. app='screen' requires image "
                "capture in every mode; use list_windows once to choose a window_id. "
                "list_apps only groups those same windows and cannot reveal new targets."
            )
        if self.warnings:
            result["warnings"] = self.warnings
        result["coordinate_system"] = (
            "Element bounds and action coordinates are relative to capture_origin, the "
            "captured area's top-left."
        )
        if self.png_data is not None:
            result["coordinate_system"] += " Coordinates are relative to the included screenshot."
        else:
            result["coordinate_system"] += " No screenshot is included in this capture mode."
        return result


@dataclass(slots=True)
class ActionResult:
    action: str
    message: str
    path: str
    verified: bool | None = None
    effect: Literal["confirmed", "unverifiable"] = "unverifiable"
    meta: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        verdict = "done" if self.verified else "verify_fresh_state"
        result: dict[str, Any] = {
            "ok": True,
            "action": self.action,
            "message": self.message,
            "path": self.path,
            "verified": self.verified,
            "effect": self.effect,
            "verdict": verdict,
        }
        if verdict == "verify_fresh_state":
            result["next_step"] = (
                "Capture fresh state before deciding whether to act again; never blindly "
                "replay input."
            )
        result.update(self.meta)
        return result


class WindowsComputerBackend:
    """Stateful Windows backend; public methods are synchronous by design."""

    def __init__(self, *, max_elements: int = 200) -> None:
        if os.name != "nt":
            raise ComputerBackendError(
                "The native backend currently supports Windows only.", "unsupported_platform"
            )
        self.max_elements = max_elements
        self._target: Any | None = None
        self._capture_origin = (0, 0)
        self._capture_size = (0, 0)
        self._elements: list[UIElement] = []
        self._snapshot_id = ""

    @staticmethod
    def _desktop() -> Any:
        from pywinauto import Desktop

        return Desktop(backend="uia")

    @staticmethod
    def _rectangle(control: Any) -> tuple[int, int, int, int]:
        rect = control.rectangle()
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    @staticmethod
    def _bounds(control: Any) -> tuple[int, int, int, int]:
        left, top, right, bottom = WindowsComputerBackend._rectangle(control)
        return (left, top, max(0, right - left), max(0, bottom - top))

    @staticmethod
    def _window_id(window: Any) -> int:
        return int(getattr(window, "handle", 0) or window.element_info.handle or 0)

    @staticmethod
    def _title(window: Any) -> str:
        try:
            return str(window.window_text() or "")
        except Exception:
            return str(window.element_info.name or "")

    @staticmethod
    def _process_name(pid: int) -> str:
        """Resolve a process basename without adding another runtime package."""
        if pid <= 0:
            return ""
        query_process = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(query_process, False, pid)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size))
            return Path(buffer.value).name if ok else ""
        finally:
            kernel32.CloseHandle(handle)

    def list_windows(self) -> list[dict[str, Any]]:
        windows: list[dict[str, Any]] = []
        for window in self._desktop().windows():
            try:
                if not window.is_visible():
                    continue
                title = self._title(window)
                left, top, right, bottom = self._rectangle(window)
                if right <= left or bottom <= top:
                    # A visible UIA window with empty bounds cannot be captured
                    # or targeted; omit it rather than returning an unusable ID.
                    continue
                pid = int(window.process_id())
                windows.append(
                    {
                        "window_id": self._window_id(window),
                        "title": title[:300],
                        "pid": pid,
                        "process": self._process_name(pid),
                        "class_name": str(window.element_info.class_name or ""),
                        "bounds": [left, top, max(0, right - left), max(0, bottom - top)],
                    }
                )
            except Exception:
                # Windows can disappear while UIA enumerates them.
                continue
            if len(windows) >= 100:
                break
        return windows

    def list_apps(self) -> list[dict[str, Any]]:
        apps: dict[int, dict[str, Any]] = {}
        for window in self.list_windows():
            pid = int(window["pid"])
            entry = apps.setdefault(
                pid,
                {
                    "pid": pid,
                    "process": window["process"],
                    "app": window["process"] or window["title"],
                    "window_count": 0,
                    "window_ids": [],
                },
            )
            entry["window_count"] += 1
            entry["window_ids"].append(window["window_id"])
        return list(apps.values())

    def _window_matches(
        self, window: Any, app: str | None, pid: int | None, window_id: int | None
    ) -> bool:
        if not window.is_visible():
            return False
        if window_id is not None and self._window_id(window) != window_id:
            return False
        current_pid = int(window.process_id())
        if pid is not None and current_pid != pid:
            return False
        if not app:
            return True
        if app.lower() == "desktop":
            return str(window.element_info.class_name or "") in {"Progman", "WorkerW"}
        needle = app.casefold()
        return needle in f"{self._title(window)}\n{self._process_name(current_pid)}".casefold()

    def _resolve_window(
        self, app: str | None = None, pid: int | None = None, window_id: int | None = None
    ) -> Any:
        # Most captures intentionally reuse the sticky target. Avoid a full UIA
        # top-level enumeration, which dominates latency on an idle desktop.
        if self._target is not None:
            try:
                if self._window_matches(self._target, app, pid, window_id):
                    return self._target
            except Exception:
                pass

        candidates: list[Any] = []
        for window in self._desktop().windows():
            try:
                if self._window_matches(window, app, pid, window_id):
                    candidates.append(window)
            except Exception:
                continue
        if candidates:
            return candidates[0]
        if app is None and pid is None and window_id is None:
            try:
                return self._desktop().get_active()
            except Exception as exc:
                raise ComputerBackendError(
                    "No active interactive Windows desktop is available.", "no_interactive_desktop"
                ) from exc
        selector = f"app={app!r}, pid={pid}, window_id={window_id}"
        raise ComputerBackendError(
            f"No visible window matches {selector}. The sticky target and any existing snapshot "
            "are unchanged: omit app/pid/window_id to inspect that target, or choose a current "
            "list_windows entry. Do not repeat equivalent discovery calls; if current output has "
            "no match, use a listed window or report that the target is unavailable.",
            "window_not_found",
        )

    @staticmethod
    def _read_value(control: Any) -> str | None:
        try:
            return str(control.iface_value.CurrentValue or "")
        except Exception:
            try:
                return str(control.get_value() or "")
            except Exception:
                return None

    @staticmethod
    def _write_value(control: Any, value: str) -> None:
        try:
            control.iface_value.SetValue(value)
            return
        except Exception:
            # RangeValue wrappers (for example sliders) expose set_value rather
            # than the ValuePattern method on the generic UIA wrapper.
            control.set_value(value)

    @staticmethod
    def _label(control: Any) -> str:
        # UIA's direct Name avoids probing TextPattern for every button/label.
        value = str(control.element_info.name or "")
        if not value:
            value = WindowsComputerBackend._read_value(control) or ""
        return " ".join(value.split())[:500]

    def _collect_elements(self, window: Any) -> tuple[list[UIElement], bool]:
        pid = int(window.process_id())
        window_id = self._window_id(window)
        elements: list[UIElement] = []
        truncated = False
        for control in window.descendants():
            try:
                role = str(control.element_info.control_type or "Custom")
                if role not in _INTERACTIVE_ROLES or not control.is_visible():
                    continue
                bounds = self._bounds(control)
                if bounds[2] <= 0 or bounds[3] <= 0:
                    continue
                if len(elements) >= self.max_elements:
                    truncated = True
                    break
                elements.append(
                    UIElement(
                        index=len(elements) + 1,
                        role=role,
                        label=self._label(control),
                        bounds=bounds,
                        pid=pid,
                        window_id=window_id,
                        control=control,
                    )
                )
            except Exception:
                continue
        return elements, truncated

    @staticmethod
    def _png(image: Image.Image) -> bytes:
        buffer = io.BytesIO()
        # PNG's optimize pass costs several times more CPU for negligible
        # screenshot size savings; model delivery still applies its own budget.
        image.save(buffer, format="PNG", compress_level=6)
        return buffer.getvalue()

    @staticmethod
    def _draw_overlay(
        image: Image.Image, elements: list[UIElement], origin: tuple[int, int]
    ) -> None:
        draw = ImageDraw.Draw(image)
        ox, oy = origin
        for element in elements:
            x, y, width, height = element.bounds
            box = (x - ox, y - oy, x - ox + width - 1, y - oy + height - 1)
            draw.rectangle(box, outline=(255, 40, 40), width=2)
            label = str(element.index)
            left = max(0, box[0])
            top = max(0, box[1] - 14)
            text_box = draw.textbbox((left, top), label, stroke_width=1)
            draw.rectangle(text_box, fill=(160, 0, 0))
            draw.text((left, top), label, fill=(255, 255, 0))

    def capture(
        self,
        mode: CaptureMode = "som",
        app: str | None = None,
        pid: int | None = None,
        window_id: int | None = None,
    ) -> CaptureResult:
        if app and app.lower() == "screen":
            try:
                image = ImageGrab.grab(all_screens=True)
            except OSError as exc:
                raise ComputerBackendError(
                    f"Full-screen image capture is unavailable: {exc}. app='screen' requires "
                    "image capture in every mode, including mode='ax'. Use list_windows once, "
                    "then capture a specific window_id with mode='ax' instead.",
                    "screen_capture_unavailable",
                ) from exc
            user32 = ctypes.windll.user32
            origin = (int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77)))
            self._target = None
            self._elements = []
            self._capture_origin = origin
            self._capture_size = image.size
            self._snapshot_id = uuid.uuid4().hex
            return CaptureResult(
                snapshot_id=self._snapshot_id,
                mode=mode,
                width=image.width,
                height=image.height,
                app="screen",
                window_title="Full virtual screen",
                window_id=0,
                pid=0,
                origin=origin,
                png_data=None if mode == "ax" else self._png(image),
                elements=[],
            )

        window = self._resolve_window(app, pid, window_id)
        rect = self._rectangle(window)
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise ComputerBackendError(
                "The target window has no visible bounds.", "window_not_visible"
            )
        requested_mode = mode
        warnings: list[str] = []
        elements: list[UIElement] = []
        truncated = False
        if mode in {"som", "ax"}:
            elements, truncated = self._collect_elements(window)
        image: Image.Image | None = None
        if mode in {"som", "vision"}:
            try:
                image = ImageGrab.grab(bbox=rect, all_screens=True)
            except OSError as exc:
                # Screen imaging can be unavailable in RDP, locked, or otherwise
                # non-interactive sessions while UIA remains fully functional.
                warnings.append(
                    f"Screenshot capture failed ({exc}); returned accessibility elements only."
                )
                if mode == "vision":
                    elements, truncated = self._collect_elements(window)
                mode = "ax"
            if image is not None and mode == "som":
                self._draw_overlay(image, elements, (rect[0], rect[1]))
        self._target = window
        self._elements = elements
        self._capture_origin = (rect[0], rect[1])
        self._capture_size = (
            image.size if image is not None else (rect[2] - rect[0], rect[3] - rect[1])
        )
        self._snapshot_id = uuid.uuid4().hex
        current_pid = int(window.process_id())
        return CaptureResult(
            snapshot_id=self._snapshot_id,
            mode=mode,
            width=self._capture_size[0],
            height=self._capture_size[1],
            app=self._process_name(current_pid) or self._title(window),
            window_title=self._title(window),
            window_id=self._window_id(window),
            pid=current_pid,
            origin=self._capture_origin,
            png_data=self._png(image) if image is not None else None,
            elements=elements,
            truncated=truncated,
            requested_mode=requested_mode if requested_mode != mode else None,
            warnings=warnings,
        )

    def capture_current(self, mode: CaptureMode) -> CaptureResult:
        """Capture the sticky target rather than whichever window is now active."""
        if self._target is None:
            return self.capture(mode=mode)
        return self.capture(
            mode=mode,
            pid=int(self._target.process_id()),
            window_id=self._window_id(self._target),
        )

    def invalidate_snapshot(self) -> None:
        """Invalidate element IDs and coordinates after a mutation or uncertain outcome."""
        self._elements = []
        self._snapshot_id = ""
        self._capture_origin = (0, 0)
        self._capture_size = (0, 0)

    def _validate_snapshot_id(self, snapshot_id: str | None) -> None:
        if snapshot_id is not None and snapshot_id != self._snapshot_id:
            raise ComputerBackendError(
                "The supplied snapshot ID is no longer current because an intervening action "
                "or capture invalidated it. Capture fresh state and use its new element IDs, "
                "coordinates, and snapshot_id; do not replay the old input.",
                "stale_snapshot",
                preserve_snapshot=True,
            )

    def _element(self, index: int, snapshot_id: str | None) -> UIElement:
        self._validate_snapshot_id(snapshot_id)
        if not self._snapshot_id or not self._elements:
            raise ComputerBackendError(
                "No element snapshot exists. Call capture with mode='som' or mode='ax' first.",
                "snapshot_required",
            )
        if index < 1 or index > len(self._elements):
            raise ComputerBackendError(
                f"Element {index} is outside the current 1..{len(self._elements)} snapshot.",
                "element_not_found",
            )
        element = self._elements[index - 1]
        try:
            # Many valid UIA elements (including taskbar buttons) are virtual and
            # have no native HWND. Visibility checks detect detachment without
            # incorrectly rejecting those controls.
            if not element.control.is_visible():
                raise ValueError("element is no longer visible")
        except Exception as exc:
            raise ComputerBackendError(
                "The UI element is no longer available. Capture fresh state.", "stale_element"
            ) from exc
        return element

    def _absolute_coordinate(
        self, coordinate: tuple[int, int], snapshot_id: str | None = None
    ) -> tuple[int, int]:
        self._validate_snapshot_id(snapshot_id)
        if self._capture_size == (0, 0):
            raise ComputerBackendError(
                "Capture a target before using coordinates.", "snapshot_required"
            )
        x, y = coordinate
        if not (0 <= x < self._capture_size[0] and 0 <= y < self._capture_size[1]):
            raise ComputerBackendError(
                f"Coordinate {coordinate} is outside the last {self._capture_size[0]}x"
                f"{self._capture_size[1]} capture.",
                "coordinate_out_of_bounds",
            )
        return (self._capture_origin[0] + x, self._capture_origin[1] + y)

    @staticmethod
    def _foreground_window_id() -> int:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return int(user32.GetForegroundWindow() or 0)

    @staticmethod
    def _require_interactive_desktop() -> None:
        """Preflight foreground input without moving the pointer or changing focus."""
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = wintypes.HANDLE
        user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user32.CloseDesktop.restype = wintypes.BOOL
        user32.CloseDesktop.argtypes = [wintypes.HANDLE]
        # DESKTOP_READOBJECTS is sufficient to establish whether this thread can
        # open the current input desktop. Never upgrade to a writable request.
        desktop = user32.OpenInputDesktop(0, False, 0x0001)
        if not desktop:
            raise ComputerBackendError(
                _NO_INTERACTIVE_DESKTOP_MESSAGE,
                "no_interactive_desktop",
                preserve_snapshot=True,
            )
        user32.CloseDesktop(desktop)

    def _foreground_target(self, *, desktop_prechecked: bool = False) -> None:
        if self._target is None:
            raise ComputerBackendError(
                "Capture or focus a window before foreground input.", "target_required"
            )
        if not desktop_prechecked:
            self._require_interactive_desktop()
        try:
            # UIAWrapper.set_focus() already restores minimized windows and
            # tolerates shell windows without WindowPattern. A separate
            # is_minimized() probe rejects those windows before SetFocus().
            self._target.set_focus()
            time.sleep(0.08)
        except Exception as exc:
            detail = str(exc).strip() or "Windows returned no error details"
            if "no active desktop" in detail.lower():
                raise ComputerBackendError(
                    "Windows foreground input requires an active interactive desktop.",
                    "no_interactive_desktop",
                ) from exc
            raise ComputerBackendError(
                f"Could not focus the captured sticky window: {type(exc).__name__}: {detail}",
                "focus_failed",
            ) from exc
        foreground = self._foreground_window_id()
        expected = self._window_id(self._target)
        if foreground != expected:
            detail = (
                "Windows reports no active foreground window"
                if foreground == 0
                else f"another window is foreground (hwnd={foreground}, expected={expected})"
            )
            raise ComputerBackendError(
                f"Could not focus the captured sticky window: {detail}. No keyboard input was "
                "sent; inspect fresh state before choosing another target.",
                "focus_failed",
            )

    @staticmethod
    def _toggle_state(control: Any) -> bool | None:
        """Read UIA TogglePattern state without making it an action prerequisite."""
        try:
            return bool(control.get_toggle_state())
        except Exception:
            return None

    @staticmethod
    def _toggle(control: Any) -> tuple[bool | None, bool | None]:
        before = WindowsComputerBackend._toggle_state(control)
        control.toggle()
        return before, WindowsComputerBackend._toggle_state(control)

    def click(
        self,
        *,
        element: int | None = None,
        coordinate: tuple[int, int] | None = None,
        button: Literal["left", "right", "middle"] = "left",
        click_count: int = 1,
        modifiers: list[str] | None = None,
        delivery_mode: Literal["background", "foreground"] = "background",
        snapshot_id: str | None = None,
    ) -> ActionResult:
        selected: UIElement | None = (
            self._element(element, snapshot_id) if element is not None else None
        )
        if delivery_mode == "background":
            if selected is None or button != "left" or click_count != 1 or modifiers:
                raise ComputerBackendError(
                    "Background clicking supports an unmodified left invoke/select action on a "
                    "fresh "
                    "element only. Re-capture and use delivery_mode='foreground' for coordinate, "
                    "button, double-click, or modifier input.",
                    "background_unavailable",
                )
            toggle_states: tuple[bool | None, bool | None] | None = None
            try:
                if selected.role in _INVOKE_ROLES:
                    try:
                        invoke_pattern = selected.control.iface_invoke
                    except Exception:
                        if selected.role not in {"Button", "SplitButton"}:
                            raise
                        # Windows 11 Start and other shell buttons expose
                        # TogglePattern instead of InvokePattern.
                        toggle_states = self._toggle(selected.control)
                        path = "uia_toggle"
                    else:
                        invoke_pattern.Invoke()
                        path = "uia_invoke"
                elif selected.role in _SELECT_ROLES:
                    selected.control.select()
                    path = "uia_select"
                elif selected.role == "CheckBox":
                    toggle_states = self._toggle(selected.control)
                    path = "uia_toggle"
                else:
                    raise ComputerBackendError(
                        f"The {selected.role} element exposes no supported background invoke "
                        "pattern.",
                        "background_unavailable",
                    )
            except ComputerBackendError:
                raise
            except Exception as exc:
                raise ComputerBackendError(
                    "Background UIA action failed; inspect fresh state before any foreground "
                    f"retry: {exc}",
                    "background_action_failed",
                ) from exc
            label = selected.label or selected.role
            message = f"Invoked element {selected.index} ({label})."
            if toggle_states is not None and selected.role != "CheckBox":
                message = (
                    f"Sent a UIA toggle to element {selected.index} ({label}); the visible UI "
                    "outcome remains unverified."
                )
            verified: bool | None = None
            meta: dict[str, Any] = {
                "element": selected.index,
                "delivery_mode": delivery_mode,
            }
            if toggle_states is not None:
                before, after = toggle_states
                state_changed = before is not None and after is not None and before != after
                # A checkbox's toggle state is its semantic value. For buttons
                # such as Start it is only diagnostic: a changed button state
                # does not prove that a menu/window opened or became actionable.
                if selected.role == "CheckBox":
                    verified = state_changed
                meta.update({"toggle_state_before": before, "toggle_state_after": after})
                if selected.role != "CheckBox":
                    meta["next_step"] = (
                        "UIA toggle state is not proof of a visible UI transition. Capture "
                        "fresh state before further input; omit app/pid/window_id to inspect "
                        "the same sticky window, and use list_windows only when looking for a "
                        "different window. If the intended UI remains unavailable, use another "
                        "supported path or report the limitation instead of repeating this toggle."
                    )
                if before is not None and after is not None and before == after:
                    verified = False
                    message = (
                        f"Sent a UIA toggle to element {selected.index} ({label}), but its "
                        "reported toggle state did not change."
                    )
            return ActionResult(
                action="click",
                message=message,
                path=path,
                verified=verified,
                effect="confirmed" if verified else "unverifiable",
                meta=meta,
            )

        from pywinauto import keyboard, mouse

        # A pointer click establishes its own target/focus. Requiring SetFocus
        # first breaks valid clicks on shell windows and across integrity levels.
        if selected is not None:
            x, y, width, height = selected.bounds
            absolute = (x + width // 2, y + height // 2)
        elif coordinate is not None:
            absolute = self._absolute_coordinate(coordinate, snapshot_id)
        else:
            raise ComputerBackendError("click requires element or coordinate.", "target_required")
        # Validate elements/coordinates first, then check before pressing modifiers:
        # even a key-down is a mutation and would make snapshot preservation unsafe.
        self._require_interactive_desktop()
        key_codes = {
            "ctrl": "LCONTROL",
            "control": "LCONTROL",
            "shift": "LSHIFT",
            "alt": "LMENU",
            "win": "LWIN",
            "windows": "LWIN",
            "super": "LWIN",
            "meta": "LWIN",
        }
        held = [key_codes[item.lower()] for item in (modifiers or []) if item.lower() in key_codes]
        for key_code in held:
            keyboard.send_keys("{" + key_code + " down}")
        try:
            if click_count == 2:
                mouse.double_click(button=button, coords=absolute)
            else:
                mouse.click(button=button, coords=absolute)
        except RuntimeError as exc:
            # pywinauto's SetCursorPos wrapper raises this exact error before
            # mouse_event. With no held modifiers, no input has been sent, so
            # the same UIA snapshot is safe to reuse for a background action.
            inactive_desktop = (
                "no active desktop required for moving mouse cursor" in str(exc).lower()
            )
            if not held and inactive_desktop:
                raise ComputerBackendError(
                    _NO_INTERACTIVE_DESKTOP_MESSAGE,
                    "no_interactive_desktop",
                    preserve_snapshot=True,
                ) from exc
            raise
        finally:
            for key_code in reversed(held):
                keyboard.send_keys("{" + key_code + " up}")
        return ActionResult(
            action="click",
            message=f"Sent {button} click at screen coordinate {absolute}.",
            path="win32_send_input",
            meta={"coordinate": list(absolute), "delivery_mode": delivery_mode},
        )

    def element_center(self, element: int, snapshot_id: str | None) -> tuple[int, int]:
        """Return a fresh element's center in last-capture coordinates."""
        selected = self._element(element, snapshot_id)
        x, y, width, height = selected.bounds
        return (
            x + width // 2 - self._capture_origin[0],
            y + height // 2 - self._capture_origin[1],
        )

    def set_value(self, element: int, value: str, snapshot_id: str | None = None) -> ActionResult:
        selected = self._element(element, snapshot_id)
        try:
            WindowsComputerBackend._write_value(selected.control, value)
            readback = WindowsComputerBackend._read_value(selected.control)
            verified = readback == value if readback is not None else None
        except Exception as exc:
            raise ComputerBackendError(
                f"The element does not support UIA ValuePattern or rejected the value: {exc}",
                "set_value_failed",
            ) from exc
        return ActionResult(
            action="set_value",
            message=f"Set element {element} through UI Automation.",
            path="uia_value",
            verified=verified,
            effect="confirmed" if verified else "unverifiable",
            meta={"element": element, "readback": readback},
        )

    @staticmethod
    def _send_keys(keys: str, **kwargs: Any) -> None:
        from pywinauto import keyboard

        try:
            keyboard.send_keys(keys, **kwargs)
        except RuntimeError as exc:
            blocked = re.fullmatch(
                r"SendInput\(\) inserted only 0 out of \d+ keyboard events",
                str(exc).strip(),
            )
            if blocked:
                raise ComputerBackendError(
                    "Windows blocked every event in this foreground keyboard call "
                    "(SendInput inserted 0 events). No keyboard events were inserted by this "
                    "call, although window focus may already have changed. Inspect fresh state "
                    "and use supported UIA background input instead of retrying foreground "
                    "keyboard delivery in this session.",
                    "input_blocked",
                ) from exc
            raise

    def type_text(
        self,
        text: str,
        *,
        element: int | None,
        delivery_mode: Literal["background", "foreground"],
        snapshot_id: str | None,
    ) -> ActionResult:
        if delivery_mode == "background":
            if element is None:
                raise ComputerBackendError(
                    "Background typing requires a fresh edit/document element. Otherwise use "
                    "delivery_mode='foreground' after focusing the intended window.",
                    "background_unavailable",
                )
            selected = self._element(element, snapshot_id)
            if selected.role not in {"Document", "Edit"}:
                raise ComputerBackendError(
                    "Background typing supports Edit and Document elements only.",
                    "background_unavailable",
                )
            try:
                previous = WindowsComputerBackend._read_value(selected.control) or ""
                expected = previous + text
                WindowsComputerBackend._write_value(selected.control, expected)
                actual = WindowsComputerBackend._read_value(selected.control) or ""
            except Exception as exc:
                raise ComputerBackendError(
                    f"Background value input failed: {exc}", "background_action_failed"
                ) from exc
            return ActionResult(
                action="type",
                message=f"Appended {len(text)} characters to element {element} through UIA.",
                path="uia_value_append",
                verified=actual == expected,
                effect="confirmed" if actual == expected else "unverifiable",
                meta={"element": element, "characters": len(text)},
            )

        selected = self._element(element, snapshot_id) if element is not None else None
        self._foreground_target()
        if selected is not None:
            selected.control.set_focus()
        escapes = {"{": "{{}", "}": "{}}", "^": "{^}", "%": "{%}", "+": "{+}", "~": "{~}"}
        literal = "".join(escapes.get(character, character) for character in text)
        self._send_keys(literal, with_spaces=True, with_newlines=True, pause=0.005)
        return ActionResult(
            action="type",
            message=f"Sent {len(text)} characters as foreground keyboard input.",
            path="win32_keyboard",
        )

    def key(self, keys: str) -> ActionResult:
        self._foreground_target()
        self._send_keys(self.to_pywinauto_keys(keys), pause=0.02)
        return ActionResult(
            action="key",
            message=f"Sent foreground key combination {keys}.",
            path="win32_keyboard",
        )

    def scroll(
        self,
        direction: Literal["up", "down", "left", "right"],
        amount: int,
        coordinate: tuple[int, int] | None,
        *,
        snapshot_id: str | None = None,
    ) -> ActionResult:
        from pywinauto import mouse

        self._validate_snapshot_id(snapshot_id)
        coords = (
            self._absolute_coordinate(coordinate, snapshot_id) if coordinate is not None else None
        )
        self._foreground_target()
        button = {"up": "wheel", "down": "wheel", "left": "left_scroll", "right": "right_scroll"}[
            direction
        ]
        distance = amount if direction in {"up", "right"} else -amount
        if button == "wheel":
            mouse.scroll(coords=coords, wheel_dist=distance)
        else:
            # Win32 horizontal scrolling is less consistently exposed by pywinauto.
            from pywinauto import keyboard

            keyboard.send_keys("{SHIFT down}")
            try:
                mouse.scroll(coords=coords, wheel_dist=-distance)
            finally:
                keyboard.send_keys("{SHIFT up}")
        return ActionResult(
            action="scroll",
            message=f"Scrolled {direction} by {amount} wheel ticks.",
            path="win32_mouse_wheel",
        )

    def drag(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration: float,
        *,
        snapshot_id: str | None = None,
    ) -> ActionResult:
        from pywinauto import mouse

        absolute_start = self._absolute_coordinate(start, snapshot_id)
        absolute_end = self._absolute_coordinate(end, snapshot_id)
        self._foreground_target()
        mouse.press(coords=absolute_start)
        try:
            steps = max(2, int(duration / 0.02))
            for step in range(1, steps + 1):
                fraction = step / steps
                point = (
                    round(absolute_start[0] + (absolute_end[0] - absolute_start[0]) * fraction),
                    round(absolute_start[1] + (absolute_end[1] - absolute_start[1]) * fraction),
                )
                mouse.move(coords=point)
                time.sleep(duration / steps)
        finally:
            mouse.release(coords=absolute_end)
        return ActionResult(
            action="drag",
            message=f"Dragged from {absolute_start} to {absolute_end}.",
            path="win32_mouse",
        )

    def focus_app(
        self,
        app: str | None,
        raise_window: bool,
        *,
        pid: int | None = None,
        window_id: int | None = None,
    ) -> ActionResult:
        window = self._resolve_window(app=app, pid=pid, window_id=window_id)
        if raise_window:
            # Refuse an unavailable input desktop before replacing the sticky
            # target or discarding a still-reusable snapshot.
            self._require_interactive_desktop()
        self._target = window
        self._elements = []
        self._snapshot_id = ""
        if raise_window:
            self._foreground_target(desktop_prechecked=True)
        return ActionResult(
            action="focus_app",
            message=(
                f"Selected sticky target {self._title(window)!r}"
                + (" and brought it to the foreground." if raise_window else " without raising it.")
            ),
            path="uia_window",
            verified=True,
            effect="confirmed",
            meta={"window_id": self._window_id(window), "pid": int(window.process_id())},
        )

    _KEY_ALIASES = {
        "return": "ENTER",
        "enter": "ENTER",
        "esc": "ESC",
        "escape": "ESC",
        "space": "SPACE",
        "pageup": "PGUP",
        "pagedown": "PGDN",
        "delete": "DELETE",
        "backspace": "BACKSPACE",
        "up": "UP",
        "down": "DOWN",
        "left": "LEFT",
        "right": "RIGHT",
        "home": "HOME",
        "end": "END",
        "tab": "TAB",
    }
    _MODIFIERS = {"ctrl": "^", "control": "^", "shift": "+", "alt": "%"}

    @classmethod
    def to_pywinauto_keys(cls, keys: str) -> str:
        """Translate a small, explicit key vocabulary to pywinauto syntax."""
        parts = [part.strip().lower() for part in keys.split("+") if part.strip()]
        if not parts:
            raise ComputerBackendError("keys must not be empty.", "invalid_keys")
        prefix = ""
        main: list[str] = []
        for part in parts:
            if part in cls._MODIFIERS:
                prefix += cls._MODIFIERS[part]
            elif part in {"win", "windows", "super", "meta", "cmd"}:
                prefix += "{LWIN down}"
                main.append("{LWIN up}")
            elif part in cls._KEY_ALIASES:
                main.insert(0, "{" + cls._KEY_ALIASES[part] + "}")
            elif re.fullmatch(r"f(?:[1-9]|1[0-9]|2[0-4])", part) or re.fullmatch(r"[a-z0-9]", part):
                main.insert(0, "{" + part.upper() + "}" if part.startswith("f") else part)
            else:
                raise ComputerBackendError(f"Unsupported key {part!r}.", "invalid_keys")
        if not main:
            raise ComputerBackendError(
                "A key combination needs a non-modifier key.", "invalid_keys"
            )
        # Modifier prefixes apply to the first main key; a Win key is wrapped around it.
        if prefix.startswith("{LWIN down}"):
            return prefix + main[0] + "".join(main[1:])
        return prefix + "".join(main)
