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


class ComputerBackendError(RuntimeError):
    """An expected desktop error with a stable machine-readable code."""

    def __init__(self, message: str, code: str = "desktop_error") -> None:
        super().__init__(message)
        self.code = code


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
            "width": self.width,
            "height": self.height,
            "element_count": len(self.elements),
            "elements": [element.as_dict(self.origin) for element in self.elements[:100]],
            "truncated": self.truncated or len(self.elements) > 100,
        }
        if self.png_data is not None:
            result["coordinate_system"] = (
                "Coordinates supplied to actions are relative to this screenshot's top-left."
            )
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
                if not title and right <= left:
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
        raise ComputerBackendError(f"No visible window matches {selector}.", "window_not_found")

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
            image = ImageGrab.grab(all_screens=True)
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
        elements: list[UIElement] = []
        truncated = False
        if mode in {"som", "ax"}:
            elements, truncated = self._collect_elements(window)
        image: Image.Image | None = None
        if mode in {"som", "vision"}:
            image = ImageGrab.grab(bbox=rect, all_screens=True)
            if mode == "som":
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

    def _element(self, index: int, snapshot_id: str | None) -> UIElement:
        if not self._snapshot_id or not self._elements:
            raise ComputerBackendError(
                "No element snapshot exists. Call capture with mode='som' or mode='ax' first.",
                "snapshot_required",
            )
        if snapshot_id is not None and snapshot_id != self._snapshot_id:
            raise ComputerBackendError(
                "The element belongs to an old snapshot. Capture fresh state before acting.",
                "stale_snapshot",
            )
        if index < 1 or index > len(self._elements):
            raise ComputerBackendError(
                f"Element {index} is outside the current 1..{len(self._elements)} snapshot.",
                "element_not_found",
            )
        element = self._elements[index - 1]
        try:
            if element.control.element_info.handle is None:
                raise ValueError("detached element")
        except Exception as exc:
            raise ComputerBackendError(
                "The UI element is no longer available. Capture fresh state.", "stale_element"
            ) from exc
        return element

    def _absolute_coordinate(self, coordinate: tuple[int, int]) -> tuple[int, int]:
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

    def _foreground_target(self) -> None:
        if self._target is None:
            raise ComputerBackendError(
                "Capture or focus a window before foreground input.", "target_required"
            )
        try:
            if self._target.is_minimized():
                self._target.restore()
            self._target.set_focus()
            time.sleep(0.08)
        except Exception as exc:
            raise ComputerBackendError(
                f"Could not focus the sticky window: {exc}", "focus_failed"
            ) from exc

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
            try:
                if selected.role in _INVOKE_ROLES:
                    selected.control.invoke()
                    path = "uia_invoke"
                elif selected.role in _SELECT_ROLES:
                    selected.control.select()
                    path = "uia_select"
                elif selected.role == "CheckBox":
                    selected.control.toggle()
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
            return ActionResult(
                action="click",
                message=f"Invoked element {selected.index} ({selected.label or selected.role}).",
                path=path,
                meta={"element": selected.index, "delivery_mode": delivery_mode},
            )

        from pywinauto import keyboard, mouse

        self._foreground_target()
        if selected is not None:
            x, y, width, height = selected.bounds
            absolute = (x + width // 2, y + height // 2)
        elif coordinate is not None:
            absolute = self._absolute_coordinate(coordinate)
        else:
            raise ComputerBackendError("click requires element or coordinate.", "target_required")
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

        from pywinauto import keyboard

        self._foreground_target()
        if element is not None:
            selected = self._element(element, snapshot_id)
            selected.control.set_focus()
        escapes = {"{": "{{}", "}": "{}}", "^": "{^}", "%": "{%}", "+": "{+}", "~": "{~}"}
        literal = "".join(escapes.get(character, character) for character in text)
        keyboard.send_keys(literal, with_spaces=True, with_newlines=True, pause=0.005)
        return ActionResult(
            action="type",
            message=f"Sent {len(text)} characters as foreground keyboard input.",
            path="win32_keyboard",
        )

    def key(self, keys: str) -> ActionResult:
        from pywinauto import keyboard

        self._foreground_target()
        keyboard.send_keys(self.to_pywinauto_keys(keys), pause=0.02)
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
    ) -> ActionResult:
        from pywinauto import mouse

        self._foreground_target()
        coords = self._absolute_coordinate(coordinate) if coordinate is not None else None
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

    def drag(self, start: tuple[int, int], end: tuple[int, int], duration: float) -> ActionResult:
        from pywinauto import mouse

        self._foreground_target()
        absolute_start = self._absolute_coordinate(start)
        absolute_end = self._absolute_coordinate(end)
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

    def focus_app(self, app: str, raise_window: bool) -> ActionResult:
        window = self._resolve_window(app=app)
        self._target = window
        self._elements = []
        self._snapshot_id = ""
        if raise_window:
            self._foreground_target()
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
