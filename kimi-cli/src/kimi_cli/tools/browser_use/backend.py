"""Persistent Playwright/CDP backend for :mod:`kimi_cli.tools.browser_use`."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from playwright.async_api import (
    Browser,
    BrowserContext,
    ElementHandle,
    Page,
    Playwright,
    async_playwright,
)

from kimi_cli.tools.browser_use.policy import url_for_output

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
_INTERACTIVE_SELECTOR = (
    "a[href],button,input,textarea,select,summary,[role='button'],[role='link'],"
    "[role='checkbox'],[role='tab'],[contenteditable='true'],[onclick],[tabindex]"
)
_SNAPSHOT_SCRIPT = r"""({selector, limit}) => {
    const nodes = Array.from(document.querySelectorAll(selector));
    const selected = [];
    for (const element of nodes) {
        if (selected.length >= limit) break;
        try {
            const box = element.getBoundingClientRect();
            const style = window.getComputedStyle(element);
            if (style.visibility === 'hidden' || box.width <= 0 || box.height <= 0) continue;
            const tag = element.tagName.toLowerCase();
            const inputType = (element.getAttribute('type') || '').toLowerCase();
            const password = tag === 'input' && inputType === 'password';
            const raw = element.getAttribute('aria-label')
                || element.getAttribute('placeholder')
                || element.innerText
                || element.getAttribute('alt')
                || ((!password && 'value' in element) ? String(element.value) : '')
                || '';
            selected.push({element, details: {
                tag,
                role: element.getAttribute('role') || inputType || tag,
                label: password
                    ? '[password field]'
                    : raw.replace(/\s+/g, ' ').trim().slice(0, 500),
                name: element.getAttribute('name'),
                input_type: inputType || null,
                bounds: [
                    Math.round(box.x), Math.round(box.y),
                    Math.round(box.width), Math.round(box.height),
                ],
            }});
        } catch {
            // The page may mutate an individual node during discovery.
        }
    }
    return {
        nodes: selected.map(item => item.element),
        items: selected.map(item => item.details),
        total: nodes.length,
    };
}"""


class BrowserBackendError(RuntimeError):
    """Expected browser failure with a stable result code."""

    def __init__(self, message: str, code: str = "browser_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class BrowserSession:
    name: str
    context: BrowserContext
    page: Page
    snapshot_id: str = ""
    elements: list[ElementHandle] = field(default_factory=list)
    last_used: float = field(default_factory=time.monotonic)


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    headed: bool = False
    executable_path: str | None = None
    cdp_url: str | None = None
    timeout_ms: int = 30_000
    inactivity_timeout: float = 300.0
    max_elements: int = 100


class BrowserManager:
    """Own one browser and multiple isolated, named incognito contexts."""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self.sessions: dict[str, BrowserSession] = {}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._owns_browser = False

    @staticmethod
    def _windows_executables() -> list[str]:
        candidates: list[str] = []
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(variable)
            if not root:
                continue
            candidates.extend(
                [
                    str(Path(root) / "Google/Chrome/Application/chrome.exe"),
                    str(Path(root) / "Microsoft/Edge/Application/msedge.exe"),
                ]
            )
        return candidates

    def _executable_candidates(self) -> list[str | None]:
        explicit = self.config.executable_path or os.environ.get("KIMI_BROWSER_EXECUTABLE")
        candidates: list[str | None] = [explicit] if explicit else []
        if sys.platform == "win32" and not explicit:
            candidates.extend(path for path in self._windows_executables() if Path(path).is_file())
        candidates.append(None)  # Playwright-managed Chromium
        return list(dict.fromkeys(candidates))

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        await self._stop_browser()
        self._playwright = await async_playwright().start()
        try:
            if self.config.cdp_url:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self.config.cdp_url, timeout=self.config.timeout_ms
                )
                self._owns_browser = False
                return self._browser

            errors: list[str] = []
            for executable in self._executable_candidates():
                try:
                    self._browser = await self._playwright.chromium.launch(
                        executable_path=executable,
                        headless=not self.config.headed,
                        timeout=max(self.config.timeout_ms, 60_000),
                        args=[
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-background-networking",
                            "--disable-component-update",
                            "--disable-sync",
                        ],
                    )
                    self._owns_browser = True
                    return self._browser
                except Exception as exc:
                    label = executable or "Playwright Chromium"
                    errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:500]}")
            raise BrowserBackendError(
                "Could not launch Chromium. Install Playwright Chromium with "
                "'uv run playwright install chromium', or set browser_use.executable_path / "
                "KIMI_BROWSER_EXECUTABLE to a Chrome or Edge executable. Attempts: "
                + " | ".join(errors),
                "browser_launch_failed",
            )
        except Exception:
            await self._stop_browser()
            raise

    async def get_session(self, name: str) -> BrowserSession:
        await self.close_idle_sessions()
        browser = await self._ensure_browser()
        existing = self.sessions.get(name)
        if existing is not None and not existing.page.is_closed():
            existing.last_used = time.monotonic()
            return existing
        if existing is not None:
            await self._close_session(existing)

        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
            )
            page = await context.new_page()
            page.set_default_timeout(self.config.timeout_ms)
            state = BrowserSession(name=name, context=context, page=page)
            self.sessions[name] = state
            return state
        except Exception as exc:
            raise BrowserBackendError(
                f"Could not create browser session: {exc}", "session_create_failed"
            ) from exc

    async def _clear_snapshot(self, state: BrowserSession) -> None:
        # Invalidate the snapshot before awaiting cleanup so no caller can observe
        # a half-cleared state. Disposal is independent and need not be serialized.
        handles = state.elements
        state.elements = []
        state.snapshot_id = ""
        if handles:
            await asyncio.gather(*(handle.dispose() for handle in handles), return_exceptions=True)

    async def _close_session(self, state: BrowserSession) -> None:
        self.sessions.pop(state.name, None)
        await self._clear_snapshot(state)
        with contextlib.suppress(Exception):
            await state.context.close()

    async def close_session(self, name: str) -> bool:
        state = self.sessions.pop(name, None)
        if state is None:
            return False
        await self._clear_snapshot(state)
        await state.context.close()
        if not self.sessions:
            await self._stop_browser()
        return True

    async def close_idle_sessions(self) -> list[str]:
        now = time.monotonic()
        expired = [
            name
            for name, state in self.sessions.items()
            if now - state.last_used >= self.config.inactivity_timeout
        ]
        for name in expired:
            state = self.sessions[name]
            await self._close_session(state)
        if not self.sessions:
            await self._stop_browser()
        return expired

    async def _stop_browser(self) -> None:
        browser = self._browser
        playwright = self._playwright
        owns_browser = self._owns_browser
        self._browser = None
        self._playwright = None
        self._owns_browser = False
        if browser is not None and owns_browser:
            with contextlib.suppress(Exception):
                await browser.close()
        if playwright is not None:
            with contextlib.suppress(Exception):
                await playwright.stop()

    async def close_all(self) -> None:
        for state in list(self.sessions.values()):
            await self._close_session(state)
        await self._stop_browser()

    @staticmethod
    def page_info(state: BrowserSession) -> dict[str, Any]:
        return {
            "session": state.name,
            "url": url_for_output(state.page.url),
            "title": "",
            "tab_count": len(state.context.pages),
        }

    async def info(self, state: BrowserSession) -> dict[str, Any]:
        result = self.page_info(state)
        result["title"] = await state.page.title()
        result["snapshot_id"] = state.snapshot_id or None
        return result

    async def snapshot(self, state: BrowserSession) -> dict[str, Any]:
        await self._clear_snapshot(state)
        # Discover metadata, bounds, and handles with a constant number of CDP
        # calls rather than three sequential calls for every element.
        root = await state.page.evaluate_handle(
            _SNAPSHOT_SCRIPT,
            {"selector": _INTERACTIVE_SELECTOR, "limit": self.config.max_elements},
        )
        temporary = [root]
        kept: list[ElementHandle] = []
        try:
            properties = await root.get_properties()
            nodes = properties["nodes"]
            items = properties["items"]
            total = properties["total"]
            temporary.extend((nodes, items, total))
            node_properties = await nodes.get_properties()
            temporary.extend(handle for key, handle in node_properties.items() if not key.isdigit())
            kept = cast(
                list[ElementHandle],
                [
                    handle
                    for key, handle in sorted(
                        node_properties.items(),
                        key=lambda item: int(item[0]) if item[0].isdigit() else -1,
                    )
                    if key.isdigit()
                ],
            )
            details = cast(list[dict[str, Any]], await items.json_value())
            total_count = int(await total.json_value())
            if len(details) != len(kept):
                raise BrowserBackendError(
                    "The page changed while its snapshot was being assembled; retry snapshot.",
                    "snapshot_changed",
                )
        except Exception:
            if kept:
                await asyncio.gather(*(handle.dispose() for handle in kept), return_exceptions=True)
            raise
        finally:
            await asyncio.gather(
                *(handle.dispose() for handle in temporary), return_exceptions=True
            )

        state.elements = kept
        state.snapshot_id = uuid.uuid4().hex
        result = await self.info(state)
        result.update(
            {
                "elements": [
                    {"element": index, **detail} for index, detail in enumerate(details, start=1)
                ],
                "element_count": len(kept),
                "truncated": total_count > self.config.max_elements,
                "coordinate_system": "Bounds are CSS pixels relative to the page viewport.",
            }
        )
        return result

    async def _target(
        self,
        state: BrowserSession,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
    ) -> Any:
        if (element is None) == (selector is None):
            raise BrowserBackendError(
                "Provide exactly one of element or selector.", "target_required"
            )
        if element is not None:
            if not state.snapshot_id or not state.elements:
                raise BrowserBackendError(
                    "Call snapshot before using element indices.", "snapshot_required"
                )
            if snapshot_id is not None and snapshot_id != state.snapshot_id:
                raise BrowserBackendError(
                    "Element snapshot is stale; take a fresh snapshot.", "stale_snapshot"
                )
            if element < 1 or element > len(state.elements):
                raise BrowserBackendError(
                    "Element index is outside the current snapshot.", "element_not_found"
                )
            handle = state.elements[element - 1]
            try:
                if not await handle.is_visible():
                    raise BrowserBackendError(
                        "Element is no longer visible; take a fresh snapshot.", "stale_element"
                    )
            except BrowserBackendError:
                raise
            except Exception as exc:
                raise BrowserBackendError(
                    "Element is detached; take a fresh snapshot.", "stale_element"
                ) from exc
            return handle
        assert selector is not None
        if len(selector) > 4000:
            raise BrowserBackendError("Selector is too long.", "invalid_selector")
        return state.page.locator(selector).first

    async def navigate(
        self, state: BrowserSession, url: str, wait_until: WaitUntil
    ) -> dict[str, Any]:
        await state.page.goto(url, wait_until=wait_until, timeout=self.config.timeout_ms)
        await self._clear_snapshot(state)
        return await self.info(state)

    async def click(
        self,
        state: BrowserSession,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
        button: Literal["left", "right", "middle"],
        click_count: int,
        modifiers: list[str],
    ) -> dict[str, Any]:
        target = await self._target(state, element, selector, snapshot_id)
        await target.click(
            button=button,
            click_count=click_count,
            modifiers=modifiers,
            timeout=self.config.timeout_ms,
        )
        await self._clear_snapshot(state)
        return await self.info(state)

    async def fill(
        self,
        state: BrowserSession,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
        text: str,
    ) -> dict[str, Any]:
        target = await self._target(state, element, selector, snapshot_id)
        await target.fill(text, timeout=self.config.timeout_ms)
        try:
            value: str | None = await target.input_value()
        except Exception:
            value = None
        await self._clear_snapshot(state)
        result = await self.info(state)
        verified = value == text if value is not None else None
        result["verified"] = verified
        result["verdict"] = "done" if verified else "verify_fresh_state"
        return result

    async def type_text(
        self,
        state: BrowserSession,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
        text: str,
        delay_ms: int,
    ) -> dict[str, Any]:
        if element is None and selector is None:
            await state.page.keyboard.type(text, delay=delay_ms)
        else:
            target = await self._target(state, element, selector, snapshot_id)
            if isinstance(target, ElementHandle):
                await target.type(text, delay=delay_ms, timeout=self.config.timeout_ms)
            else:
                await target.press_sequentially(
                    text, delay=delay_ms, timeout=self.config.timeout_ms
                )
        await self._clear_snapshot(state)
        return await self.info(state)

    async def press(
        self,
        state: BrowserSession,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
        key: str,
    ) -> dict[str, Any]:
        if element is None and selector is None:
            await state.page.keyboard.press(key)
        else:
            target = await self._target(state, element, selector, snapshot_id)
            await target.press(key, timeout=self.config.timeout_ms)
        await self._clear_snapshot(state)
        return await self.info(state)

    async def extract(
        self,
        state: BrowserSession,
        selector: str | None,
        attribute: str | None,
        all_matches: bool,
        max_items: int,
    ) -> dict[str, Any]:
        page = state.page
        truncated = False
        if selector is None:
            values = [(await page.locator("body").inner_text())[:50_000]]
        else:
            payload = await page.locator(selector).evaluate_all(
                """(elements, {attribute, limit}) => ({
                    values: elements.slice(0, limit).map(element => attribute
                        ? element.getAttribute(attribute)
                        : String(element.innerText || '').slice(0, 50000)),
                    truncated: elements.length > limit,
                })""",
                {"attribute": attribute, "limit": max_items if all_matches else 1},
            )
            values = payload["values"]
            truncated = payload["truncated"]
        result = await self.info(state)
        result.update({"values": values, "truncated": truncated})
        return result

    async def evaluate(self, state: BrowserSession, script: str, argument: Any) -> dict[str, Any]:
        value = await state.page.evaluate(script, argument)
        await self._clear_snapshot(state)
        result = await self.info(state)
        result["value"] = value
        result["verdict"] = "verify_fresh_state"
        return result

    async def screenshot(
        self, state: BrowserSession, full_page: bool
    ) -> tuple[dict[str, Any], bytes]:
        data = await state.page.screenshot(
            type="png", full_page=full_page, timeout=self.config.timeout_ms
        )
        return await self.info(state), data

    async def new_tab(
        self, state: BrowserSession, url: str | None, wait_until: WaitUntil
    ) -> dict[str, Any]:
        page = await state.context.new_page()
        page.set_default_timeout(self.config.timeout_ms)
        state.page = page
        if url:
            await page.goto(url, wait_until=wait_until, timeout=self.config.timeout_ms)
        await self._clear_snapshot(state)
        return await self.info(state)

    async def switch_tab(self, state: BrowserSession, tab: int) -> dict[str, Any]:
        pages = state.context.pages
        if tab < 1 or tab > len(pages):
            raise BrowserBackendError(f"Tab must be in 1..{len(pages)}.", "tab_not_found")
        state.page = pages[tab - 1]
        await state.page.bring_to_front()
        await self._clear_snapshot(state)
        return await self.info(state)

    async def close_tab(self, state: BrowserSession, tab: int | None) -> dict[str, Any]:
        pages = state.context.pages
        if tab is None:
            target = state.page
        else:
            if tab < 1 or tab > len(pages):
                raise BrowserBackendError(f"Tab must be in 1..{len(pages)}.", "tab_not_found")
            target = pages[tab - 1]
        await target.close()
        await self._clear_snapshot(state)
        remaining = state.context.pages
        state.page = remaining[-1] if remaining else await state.context.new_page()
        state.page.set_default_timeout(self.config.timeout_ms)
        return await self.info(state)

    async def list_tabs(self, state: BrowserSession) -> dict[str, Any]:
        pages = state.context.pages
        titles = await asyncio.gather(*(page.title() for page in pages))
        tabs = [
            {
                "tab": index,
                "active": page == state.page,
                "url": url_for_output(page.url),
                "title": title,
                "closed": page.is_closed(),
            }
            for index, (page, title) in enumerate(zip(pages, titles, strict=True), start=1)
        ]
        result = await self.info(state)
        result["tabs"] = tabs
        return result

    async def wait(
        self, state: BrowserSession, seconds: float, selector: str | None
    ) -> dict[str, Any]:
        if selector:
            await state.page.locator(selector).first.wait_for(
                state="visible", timeout=max(1, int(seconds * 1000))
            )
        else:
            await asyncio.sleep(seconds)
        return await self.info(state)

    async def scroll(
        self,
        state: BrowserSession,
        direction: Literal["up", "down", "left", "right"],
        amount: int,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
    ) -> dict[str, Any]:
        if element is not None or selector is not None:
            target = await self._target(state, element, selector, snapshot_id)
            await target.scroll_into_view_if_needed(timeout=self.config.timeout_ms)
            box = await target.bounding_box()
            if box:
                await state.page.mouse.move(
                    box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                )
        dx = amount if direction == "right" else -amount if direction == "left" else 0
        dy = amount if direction == "down" else -amount if direction == "up" else 0
        await state.page.mouse.wheel(dx, dy)
        await self._clear_snapshot(state)
        return await self.info(state)

    async def history(
        self,
        state: BrowserSession,
        action: Literal["back", "forward", "reload"],
        wait_until: WaitUntil,
    ) -> dict[str, Any]:
        if action == "back":
            await state.page.go_back(wait_until=wait_until, timeout=self.config.timeout_ms)
        elif action == "forward":
            await state.page.go_forward(wait_until=wait_until, timeout=self.config.timeout_ms)
        else:
            await state.page.reload(wait_until=wait_until, timeout=self.config.timeout_ms)
        await self._clear_snapshot(state)
        return await self.info(state)
