"""Persistent Playwright/CDP backend for :mod:`kimi_cli.tools.browser_use`."""

from __future__ import annotations

import asyncio
import contextlib
import ntpath
import os
import time
import uuid
from collections import deque
from collections.abc import Iterable
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

from kimi_cli import logger
from kimi_cli.tools.browser_use.discovery import clean_executable_path, discover_browsers
from kimi_cli.tools.browser_use.policy import (
    BrowserPolicyError,
    url_for_output,
    validate_cdp_url,
)

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
WaitState = Literal["attached", "detached", "hidden", "visible"]
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
            // HTMLInputElement.type reflects the default 'text' type and
            // normalizes invalid/missing values; the raw attribute does not.
            const inputType = tag === 'input'
                ? String(element.type || 'text').toLowerCase()
                : (element.getAttribute('type') || '').toLowerCase();
            const password = tag === 'input' && inputType === 'password';
            const associatedLabels = Array.from(element.labels || [])
                .map(label => label.innerText || label.textContent || '')
                .join(' ');
            const hasValue = !password && ['input', 'textarea', 'select'].includes(tag);
            const value = hasValue ? String(element.value) : '';
            const raw = element.getAttribute('aria-label')
                || associatedLabels
                || element.getAttribute('placeholder')
                || element.innerText
                || element.getAttribute('alt')
                || element.getAttribute('title')
                || element.getAttribute('name')
                || value;
            selected.push({element, details: {
                tag,
                role: element.getAttribute('role') || inputType || tag,
                label: password
                    ? '[password field]'
                    : raw.replace(/\s+/g, ' ').trim().slice(0, 500),
                value: hasValue ? value.slice(0, 500) : null,
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

_FIELDS_SCRIPT = r"""(elements, {fields, limit}) => {
    const read = (element, spec) => {
        const separator = spec.lastIndexOf('|');
        const selector = separator > 0 ? spec.slice(0, separator) : spec;
        const attribute = separator > 0 ? spec.slice(separator + 1) : '';
        const target = (selector === '' || selector === '@' || selector === '@self')
            ? element
            : element.querySelector(selector);
        if (!target) return null;
        if (attribute) return target.getAttribute(attribute);
        return String(target.innerText ?? target.textContent ?? '')
            .replace(/\s+/g, ' ')
            .trim()
            .slice(0, 2000);
    };
    return {
        values: elements.slice(0, limit).map((element) => {
            const record = {};
            for (const [key, spec] of Object.entries(fields)) record[key] = read(element, spec);
            return record;
        }),
        truncated: elements.length > limit,
    };
}"""

_MAIN_CONTENT_SCRIPT = r"""(limit) => {
    let best = null;
    let bestScore = 0;
    const candidates = document.querySelectorAll('article, main, [role="main"], section, div');
    for (const element of candidates) {
        const text = element.innerText || '';
        if (text.length < 200) continue;
        let linkText = 0;
        for (const link of element.querySelectorAll('a')) linkText += (link.innerText || '').length;
        // Boilerplate navigation is link-dense; readable prose is not.
        const score = text.length - 3 * linkText;
        if (score > bestScore) {
            bestScore = score;
            best = element;
        }
    }
    const source = best || document.body;
    const text = String((source && source.innerText) || '').replace(/\n{3,}/g, '\n\n').trim();
    return {text: text.slice(0, limit), chars: text.length, container: Boolean(best)};
}"""


#: Codes whose failure clears once the agent re-observes the page.  Everything
#: else is treated as permanent unless a raise site says otherwise.
_RETRYABLE_CODES = frozenset(
    {
        "element_not_found",
        "snapshot_changed",
        "snapshot_required",
        "stale_element",
        "stale_snapshot",
    }
)


class BrowserBackendError(RuntimeError):
    """Expected browser failure with a stable result code.

    ``retryable`` tells the agent whether repeating the call can succeed without
    changing anything else: a stale snapshot is retryable after a fresh snapshot, a
    refused URL is not.  ``None`` means the outcome is genuinely unknown (a timeout)
    and the agent must inspect state instead of retrying.
    """

    def __init__(
        self, message: str, code: str = "browser_error", *, retryable: bool | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable if retryable is not None else code in _RETRYABLE_CODES


_MIN_LAUNCH_BUDGET_MS = 1_000

#: Docker mounts 64 MB on /dev/shm by default; Chromium passes render data through
#: it and crashes renderers when it runs out.
_SHM_SHORTAGE_BYTES = 256 * 1024 * 1024

#: Uploads are model-driven; a handful of files is all a form can take.
_MAX_UPLOAD_FILES = 10


def _dev_shm_free_bytes(path: str = "/dev/shm") -> int | None:
    """Free bytes in a shared-memory mount, or None when it is absent or unknown."""
    statvfs = getattr(os, "statvfs", None)
    if statvfs is None:  # Windows and macOS have no /dev/shm contract.
        return None
    try:
        stats = statvfs(path)
    except OSError:
        return None
    return int(stats.f_bavail * stats.f_frsize)

#: Substrings seen when Chromium rejects its sandbox (containers without user
#: namespaces, hardened kernels).  Only used to retry once without the sandbox and
#: to explain the failure; never to silently weaken a working launch.
_SANDBOX_FAILURE_MARKERS = (
    "sandbox",
    "zygote_host_impl_linux",
    "setuid helper",
)


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """One launch attempt: an executable, a Playwright channel, or the bundle."""

    label: str
    executable: str | None = None
    channel: str | None = None

    def launch_kwargs(self) -> dict[str, Any]:
        if self.channel is not None:
            return {"channel": self.channel}
        if self.executable is not None:
            return {"executable_path": self.executable}
        return {}

    def describe(self) -> str:
        """Label plus the concrete target, for logs and aggregate errors."""
        if self.executable is not None:
            return f"{self.label} [{self.executable}]"
        if self.channel is not None:
            return f"{self.label} [channel]"
        return f"{self.label} [bundled]"


@dataclass(slots=True)
class BrowserSession:
    name: str
    context: BrowserContext
    page: Page
    snapshot_id: str = ""
    elements: list[ElementHandle] = field(default_factory=list)
    last_used: float = field(default_factory=time.monotonic)
    borrowed_context: bool = False
    """The context belongs to an attached browser, so closing it is not ours to do."""
    console: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=5))
    network_failures: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=5))
    wired: list[Page] = field(default_factory=list)
    """Pages that already carry diagnostic listeners."""


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    """Launch policy for the owned browser.

    ``executable_path`` and ``channel`` are *preferences*, not exclusive overrides:
    a configured browser that fails to launch falls through to auto-detection so a
    stale path cannot disable automation on a machine with a working browser.
    """

    headed: bool = False
    executable_path: str | None = None
    channel: str | None = None
    cdp_url: str | None = None
    timeout_ms: int = 30_000
    inactivity_timeout: float = 300.0
    max_elements: int = 100
    launch_timeout_ms: int = 15_000
    launch_budget: float = 40.0
    max_launch_attempts: int = 6
    sandbox: bool = True
    viewport_width: int = 1280
    viewport_height: int = 900
    extra_args: tuple[str, ...] = ()
    """Extra Chromium switches, for example fingerprint-reduction flags."""
    allow_remote_cdp: bool = False
    """Permit a non-loopback ``cdp_url``; a debugging port has no authentication."""
    reuse_default_context: bool = False
    """Attach to the browser's existing context so its login state is visible."""
    storage_state: str | None = None
    """Cookie/localStorage file to load on create and save on close."""
    allowed_origins: tuple[str, ...] = ()
    blocked_origins: tuple[str, ...] = ()
    console_events: int = 5
    """Recent console errors and failed requests reported per result; 0 disables them."""


def _dedupe_specs(specs: Iterable[LaunchSpec]) -> list[LaunchSpec]:
    """Drop repeated attempts, folding case on Windows executable paths."""
    seen: set[tuple[str, str]] = set()
    unique: list[LaunchSpec] = []
    for spec in specs:
        executable = ntpath.normcase(spec.executable) if spec.executable else ""
        key = (spec.channel or "", executable)
        if key in seen:
            continue
        seen.add(key)
        unique.append(spec)
    return unique


def _describe_failure(exc: BaseException) -> str:
    """Collapse a Playwright launch error to one diagnosable line."""
    message = " ".join(str(exc).split())
    return f"{type(exc).__name__}: {message[:400]}"


def _mentions_sandbox(messages: Iterable[str]) -> bool:
    return any(
        marker in message.lower() for message in messages for marker in _SANDBOX_FAILURE_MARKERS
    )


def _looks_like_sandbox_failure(exc: BaseException) -> bool:
    return _mentions_sandbox([str(exc)])


def _env_flag(name: str) -> bool:
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _launch_failure_message(errors: list[str]) -> str:
    """Explain every attempt plus the actionable remedy for the common causes."""
    hints: list[str] = []
    if any("executable doesn't exist" in error.lower() for error in errors):
        hints.append(
            "The Playwright-managed Chromium is not installed: run 'playwright install chromium' "
            "or point PLAYWRIGHT_BROWSERS_PATH at a directory that contains it."
        )
    if _mentions_sandbox(errors):
        hints.append(
            "If this host is a container without user namespaces, disable the sandbox with "
            "browser_use.sandbox=false or KIMI_BROWSER_NO_SANDBOX=1."
        )
    attempts = " | ".join(errors) if errors else "no candidates were available"
    return " ".join(
        [
            "Could not launch a Chromium-family browser. Point browser_use.executable_path or",
            "KIMI_BROWSER_EXECUTABLE at Chrome/Edge, set browser_use.channel, or install",
            "Playwright Chromium.",
            *hints,
            f"Attempts: {attempts}",
        ]
    )


def resolve_workspace_file(raw: str, work_dir: Path) -> Path:
    """Resolve an upload path that must stay inside the workspace root.

    Uploads are the one place a model-facing tool would otherwise hand an arbitrary
    local file to a web page, so the check is containment, not a warning.
    """
    text = raw.strip()
    if not text:
        raise BrowserBackendError(
            "Upload path must not be empty.", "invalid_upload_path", retryable=False
        )
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = work_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BrowserBackendError(
            f"Upload file is not readable: {text} ({type(exc).__name__}).",
            "invalid_upload_path",
            retryable=False,
        ) from exc
    if not resolved.is_relative_to(work_dir.resolve()):
        raise BrowserBackendError(
            f"Upload path {text!r} is outside the workspace root; only workspace files can be "
            "uploaded.",
            "upload_outside_workspace",
            retryable=False,
        )
    if not resolved.is_file():
        raise BrowserBackendError(
            f"Upload path {text!r} is not a regular file.",
            "invalid_upload_path",
            retryable=False,
        )
    return resolved


def _record_console(state: BrowserSession, message: Any) -> None:
    if state.console.maxlen == 0 or message.type not in {"error", "warning"}:
        return
    text = str(message.text).strip()
    if text:
        state.console.append({"type": message.type, "text": text[:300]})


def _record_page_error(state: BrowserSession, error: BaseException) -> None:
    if state.console.maxlen == 0:
        return
    state.console.append({"type": "pageerror", "text": str(error).strip()[:300]})


_IGNORED_REQUEST_FAILURES = ("net::ERR_ABORTED",)


def _record_request_failure(state: BrowserSession, request: Any) -> None:
    if state.network_failures.maxlen == 0:
        return
    reason = str(request.failure or "request failed")
    # Navigation cancels in-flight subresources; those aborts are not failures and
    # would otherwise appear in every receipt right after a navigate.
    if any(marker in reason for marker in _IGNORED_REQUEST_FAILURES):
        return
    state.network_failures.append(
        {
            "url": url_for_output(str(request.url)),
            "reason": reason[:200],
        }
    )


class BrowserManager:
    """Own one browser and multiple isolated, named incognito contexts."""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self.sessions: dict[str, BrowserSession] = {}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._owns_browser = False
        self._launch_info: dict[str, Any] | None = None
        self._sandbox_disabled = False
        self._cleanup_tasks: set[asyncio.Task[None]] = set()

    @staticmethod
    def _launch_args_for(config: BrowserConfig) -> list[str]:
        args = [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            f"--window-size={config.viewport_width},{config.viewport_height}",
        ]
        free = _dev_shm_free_bytes()
        if free is not None and free < _SHM_SHORTAGE_BYTES:
            # Smaller than Chromium needs: keep renderer buffers off /dev/shm.
            args.append("--disable-dev-shm-usage")
        args.extend(config.extra_args)
        return args

    def _launch_args(self) -> list[str]:
        return self._launch_args_for(self.config)

    def _configured_executable(self) -> str | None:
        return clean_executable_path(
            self.config.executable_path or os.environ.get("KIMI_BROWSER_EXECUTABLE")
        )

    def _configured_channel(self) -> str | None:
        channel = self.config.channel or os.environ.get("KIMI_BROWSER_CHANNEL")
        if not channel:
            return None
        return channel.strip() or None

    def launch_specs(self) -> list[LaunchSpec]:
        """Ordered launch attempts: configured, then detected, then the bundle.

        A configured browser that cannot launch falls through to detection rather
        than aborting: a stale ``executable_path`` must not disable automation on a
        machine that has a working browser somewhere else.  Every attempt is
        reported in the aggregate error, so the fallback is never silent.
        """
        specs: list[LaunchSpec] = []
        executable = self._configured_executable()
        if executable:
            specs.append(LaunchSpec("configured executable", executable=executable))
        channel = self._configured_channel()
        if channel:
            specs.append(LaunchSpec(f"{channel} (channel)", channel=channel))
        specs.extend(
            LaunchSpec(browser.label, executable=browser.executable)
            for browser in discover_browsers()
        )
        specs.append(LaunchSpec("Playwright-managed Chromium"))
        return _dedupe_specs(specs)

    def _sandbox_enabled(self) -> bool:
        if self._sandbox_disabled or not self.config.sandbox:
            return False
        return not _env_flag("KIMI_BROWSER_NO_SANDBOX")

    def _cdp_endpoint(self) -> str | None:
        """Validated attach endpoint; refuses non-loopback hosts unless opted in."""
        if not self.config.cdp_url:
            return None
        try:
            return validate_cdp_url(
                self.config.cdp_url, allow_remote=self.config.allow_remote_cdp
            )
        except BrowserPolicyError as exc:
            raise BrowserBackendError(str(exc), exc.code, retryable=False) from exc

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        await self._stop_browser()
        # Validate before starting a driver: a refused endpoint must not leave one.
        endpoint = self._cdp_endpoint()
        self._playwright = await async_playwright().start()
        try:
            if endpoint is not None:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    endpoint, timeout=self.config.timeout_ms
                )
                self._owns_browser = False
                self._record_launch("CDP endpoint", executable=None)
                return self._browser
            return await self._launch_owned_browser()
        except BaseException:
            # A cancelled launch (a timed-out tool call) must not leak a browser
            # process or a driver: `_stop_browser` clears state synchronously, then
            # runs as a shielded task that survives the cancellation delivered here.
            self._schedule_cleanup()
            raise

    def _schedule_cleanup(self) -> None:
        try:
            task = asyncio.ensure_future(self._stop_browser())
        except RuntimeError:  # pragma: no cover - event loop already closing
            return
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    async def _launch_owned_browser(self) -> Browser:
        assert self._playwright is not None
        budget = max(1.0, self.config.launch_budget)
        deadline = time.monotonic() + budget
        max_attempts = max(1, self.config.max_launch_attempts)
        attempted = 0
        errors: list[str] = []
        specs = self.launch_specs()
        logger.debug("browser launch candidates: %s", [spec.describe() for spec in specs])
        for spec in specs:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if attempted >= max_attempts:
                errors.append(f"{spec.describe()}: skipped (attempt limit {max_attempts} reached)")
                continue
            if remaining_ms < _MIN_LAUNCH_BUDGET_MS:
                errors.append(
                    f"{spec.describe()}: skipped (launch budget of {budget:.0f}s exhausted)"
                )
                continue
            attempted += 1
            browser, error = await self._launch_candidate(
                spec, min(self.config.launch_timeout_ms, remaining_ms)
            )
            if browser is not None:
                self._browser = browser
                self._owns_browser = True
                self._record_launch(spec.label, executable=spec.executable)
                return browser
            errors.append(error)
        raise BrowserBackendError(_launch_failure_message(errors), "browser_launch_failed")

    async def _launch_candidate(
        self, spec: LaunchSpec, timeout_ms: int
    ) -> tuple[Browser | None, str]:
        sandbox = self._sandbox_enabled()
        logger.debug(
            "browser launch attempt: %s (timeout %dms, sandbox=%s)",
            spec.describe(),
            timeout_ms,
            sandbox,
        )
        try:
            return await self._do_launch(spec, timeout_ms, sandbox), ""
        except Exception as exc:
            error = f"{spec.describe()}: {_describe_failure(exc)}"
            logger.debug("browser launch failed: %s", error)
            if not sandbox or not _looks_like_sandbox_failure(exc):
                return None, error
        return await self._retry_without_sandbox(spec, timeout_ms, error)

    async def _retry_without_sandbox(
        self, spec: LaunchSpec, timeout_ms: int, error: str
    ) -> tuple[Browser | None, str]:
        logger.warning("Chromium refused its sandbox; retrying %s without it", spec.describe())
        try:
            browser = await self._do_launch(spec, timeout_ms, False)
        except Exception as exc:
            return None, f"{error} | without sandbox: {_describe_failure(exc)}"
        self._sandbox_disabled = True
        logger.warning(
            "The Chromium sandbox is unusable on this host; browser automation continues "
            "without it"
        )
        return browser, ""

    async def _do_launch(self, spec: LaunchSpec, timeout_ms: int, sandbox: bool) -> Browser:
        assert self._playwright is not None
        return await self._playwright.chromium.launch(
            **spec.launch_kwargs(),
            headless=not self.config.headed,
            timeout=timeout_ms,
            args=self._launch_args(),
            chromium_sandbox=sandbox,
        )

    def _record_launch(self, label: str, *, executable: str | None) -> None:
        version: str | None = None
        if self._browser is not None:
            with contextlib.suppress(Exception):
                version = self._browser.version
        self._launch_info = {
            "browser": label,
            "browser_version": version,
            "executable": executable,
        }
        logger.debug("browser_use launched %s (version %s)", label, version)

    def launcher_info(self) -> dict[str, Any]:
        """Describe the browser actually driving the session, for tool output."""
        info = self._launch_info or {}
        return {
            "browser": info.get("browser"),
            "browser_version": info.get("browser_version"),
            "executable": info.get("executable"),
        }

    async def get_session(self, name: str) -> BrowserSession:
        await self.close_idle_sessions()
        browser = await self._ensure_browser()
        existing = self.sessions.get(name)
        if existing is not None and not existing.page.is_closed():
            existing.last_used = time.monotonic()
            return existing
        if existing is not None:
            # A page script or the user may close only the active tab. Preserve
            # the isolated context and its other tabs instead of destroying them.
            remaining = [page for page in existing.context.pages if not page.is_closed()]
            if remaining:
                existing.page = remaining[-1]
                existing.page.set_default_timeout(self.config.timeout_ms)
                self._wire_page(existing, existing.page)
                await self._clear_snapshot(existing)
                existing.last_used = time.monotonic()
                return existing
            await self._close_session(existing)

        try:
            context, borrowed = await self._session_context(browser)
            page = await self._session_page(context, borrowed)
            page.set_default_timeout(self.config.timeout_ms)
            limit = max(0, self.config.console_events)
            state = BrowserSession(
                name=name,
                context=context,
                page=page,
                borrowed_context=borrowed,
                console=deque(maxlen=limit),
                network_failures=deque(maxlen=limit),
            )
            self._wire_page(state, page)
            self.sessions[name] = state
            return state
        except Exception as exc:
            raise BrowserBackendError(
                f"Could not create browser session: {exc}",
                "session_create_failed",
                retryable=False,
            ) from exc

    async def _session_context(self, browser: Browser) -> tuple[BrowserContext, bool]:
        """Create an isolated context, or borrow the attached browser's default one.

        A CDP attach exists to drive the *real* session: a fresh incognito context
        would be empty precisely where the logged-in state lives.  Borrowing is
        opt-in because it also means writing into a profile we do not own.
        """
        if self.config.reuse_default_context and not self._owns_browser and browser.contexts:
            return browser.contexts[0], True
        options: dict[str, Any] = {
            "viewport": {
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            "accept_downloads": False,
        }
        if self.config.storage_state:
            stored = Path(self.config.storage_state)
            if stored.is_file():
                options["storage_state"] = str(stored)
        return await browser.new_context(**options), False

    @staticmethod
    async def _session_page(context: BrowserContext, borrowed: bool) -> Page:
        if borrowed:
            pages = [page for page in context.pages if not page.is_closed()]
            if pages:
                return pages[-1]
        return await context.new_page()

    def _wire_page(self, state: BrowserSession, page: Page) -> None:
        """Collect diagnostics from a page exactly once.

        Every action already returns a state receipt; without page events that
        receipt cannot say why a page looks broken.  Listeners are synchronous
        appends into bounded deques, so a chatty page cannot grow the result.
        """
        if any(existing is page for existing in state.wired):
            return
        if state.console.maxlen != max(0, self.config.console_events):
            state.console = deque(state.console, maxlen=max(0, self.config.console_events))
            state.network_failures = deque(
                state.network_failures, maxlen=max(0, self.config.console_events)
            )
        state.wired.append(page)
        page.on("console", lambda message: _record_console(state, message))
        page.on("pageerror", lambda error: _record_page_error(state, error))
        page.on("requestfailed", lambda request: _record_request_failure(state, request))

    @staticmethod
    def _clear_diagnostics(state: BrowserSession) -> None:
        """Start each document with an empty receipt.

        Console errors describe the document that produced them, so carrying them
        across a navigation would blame the new page for the old one's faults.
        """
        state.console.clear()
        state.network_failures.clear()

    async def invalidate_snapshot(self, state: BrowserSession) -> None:
        """Invalidate element handles after a mutation, including uncertain outcomes."""
        await self._clear_snapshot(state)

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
        if state.borrowed_context:
            # Never close a context that belongs to an attached browser: it is the
            # user's real profile, not something this tool owns.
            return
        await self._save_storage_state(state)
        with contextlib.suppress(Exception):
            await state.context.close()

    async def _save_storage_state(self, state: BrowserSession) -> None:
        if not self.config.storage_state or not self._owns_browser:
            # Copying an attached browser's cookies would exfiltrate a real session.
            return
        path = Path(self.config.storage_state)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            await state.context.storage_state(path=str(path))
        except Exception as exc:
            logger.warning("Could not save browser storage state to %s: %s", path, exc)

    async def close_session(self, name: str) -> bool:
        state = self.sessions.get(name)
        if state is None:
            return False
        await self._close_session(state)
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
        self._launch_info = None
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
        result.update(self.launcher_info())
        # Reported only when present: a healthy page must not pay tokens for an
        # always-empty diagnostic field.
        if state.console:
            result["console"] = list(state.console)
        if state.network_failures:
            result["network_failures"] = list(state.network_failures)
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
        self._clear_diagnostics(state)
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
        position: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        if position is not None:
            await self._click_at(state, position, button, click_count, modifiers)
            await self._clear_snapshot(state)
            return await self.info(state)
        target = await self._target(state, element, selector, snapshot_id)
        await target.click(
            button=button,
            click_count=click_count,
            modifiers=modifiers,
            timeout=self.config.timeout_ms,
        )
        await self._clear_snapshot(state)
        return await self.info(state)

    async def _click_at(
        self,
        state: BrowserSession,
        position: tuple[int, int],
        button: Literal["left", "right", "middle"],
        click_count: int,
        modifiers: list[str],
    ) -> None:
        # Coordinate clicks exist for what references cannot reach: canvas apps,
        # closed shadow roots and cross-origin iframe content.  Viewport-relative CSS
        # pixels, matching what snapshot reports.
        held: list[str] = []
        try:
            for modifier in modifiers:
                await state.page.keyboard.down(modifier)
                held.append(modifier)
            await state.page.mouse.click(
                position[0], position[1], button=button, click_count=click_count
            )
        finally:
            for modifier in reversed(held):
                with contextlib.suppress(Exception):
                    await state.page.keyboard.up(modifier)

    async def select(
        self,
        state: BrowserSession,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
        values: list[str],
    ) -> dict[str, Any]:
        target = await self._target(state, element, selector, snapshot_id)
        selected = await target.select_option(values, timeout=self.config.timeout_ms)
        await self._clear_snapshot(state)
        result = await self.info(state)
        result["selected"] = selected
        return result

    async def upload(
        self,
        state: BrowserSession,
        element: int | None,
        selector: str | None,
        snapshot_id: str | None,
        paths: list[str],
        work_dir: Path | None,
    ) -> dict[str, Any]:
        if work_dir is None:
            raise BrowserBackendError(
                "File uploads need a local workspace directory that this session does not expose.",
                "upload_unavailable",
                retryable=False,
            )
        if not paths:
            raise BrowserBackendError(
                "upload requires at least one path.", "invalid_upload_path", retryable=False
            )
        if len(paths) > _MAX_UPLOAD_FILES:
            raise BrowserBackendError(
                f"upload accepts at most {_MAX_UPLOAD_FILES} files.",
                "invalid_upload_path",
                retryable=False,
            )
        resolved = [resolve_workspace_file(path, work_dir) for path in paths]
        target = await self._target(state, element, selector, snapshot_id)
        await target.set_input_files(
            [str(path) for path in resolved], timeout=self.config.timeout_ms
        )
        await self._clear_snapshot(state)
        result = await self.info(state)
        result["files"] = [str(path) for path in resolved]
        return result

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
        *,
        fields: dict[str, str] | None = None,
        main_content: bool = False,
        content_format: Literal["html", "text"] = "text",
    ) -> dict[str, Any]:
        # Models commonly request attribute='text' when they want inner text.
        # Treat it as an explicit alias rather than returning a missing HTML
        # attribute and forcing a second extraction call.
        attribute = None if attribute == "text" else attribute
        page = state.page
        truncated = False
        if fields is not None:
            if selector is None:
                raise BrowserBackendError(
                    "extract with fields requires a selector that identifies the records.",
                    "target_required",
                    retryable=True,
                )
            payload = await page.locator(selector).evaluate_all(
                _FIELDS_SCRIPT, {"fields": fields, "limit": max_items}
            )
            result = await self.info(state)
            result.update({"records": payload["values"], "truncated": payload["truncated"]})
            return result
        if main_content:
            payload = await page.evaluate(_MAIN_CONTENT_SCRIPT, 50_000)
            text = str(payload["text"])
            result = await self.info(state)
            result.update(
                {
                    "main_content": text,
                    "characters": int(payload["chars"]),
                    "truncated": int(payload["chars"]) > len(text),
                    "container": bool(payload["container"]),
                }
            )
            return result
        if content_format == "html":
            html = await page.locator(selector or "body").first.inner_html()
            result = await self.info(state)
            result.update({"html": html, "truncated": False})
            return result
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
        self,
        state: BrowserSession,
        full_page: bool,
        element: int | None = None,
        selector: str | None = None,
        snapshot_id: str | None = None,
    ) -> tuple[dict[str, Any], bytes]:
        if element is not None or selector is not None:
            target = await self._target(state, element, selector, snapshot_id)
            data = await target.screenshot(type="png", timeout=self.config.timeout_ms)
            summary = await self.info(state)
            summary["scope"] = "element"
            return summary, data
        data = await state.page.screenshot(
            type="png", full_page=full_page, timeout=self.config.timeout_ms
        )
        summary = await self.info(state)
        summary["scope"] = "full_page" if full_page else "viewport"
        return summary, data
    async def new_tab(
        self, state: BrowserSession, url: str | None, wait_until: WaitUntil
    ) -> dict[str, Any]:
        page = await state.context.new_page()
        page.set_default_timeout(self.config.timeout_ms)
        state.page = page
        self._wire_page(state, page)
        if url:
            self._clear_diagnostics(state)
            await page.goto(url, wait_until=wait_until, timeout=self.config.timeout_ms)
        await self._clear_snapshot(state)
        return await self.info(state)

    async def switch_tab(self, state: BrowserSession, tab: int) -> dict[str, Any]:
        pages = state.context.pages
        if tab < 1 or tab > len(pages):
            raise BrowserBackendError(f"Tab must be in 1..{len(pages)}.", "tab_not_found")
        state.page = pages[tab - 1]
        await state.page.bring_to_front()
        self._wire_page(state, state.page)
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
        # Closing a background tab must not unexpectedly switch the active page.
        # Select a fallback only when the active page itself was closed.
        if state.page.is_closed():
            remaining = [page for page in state.context.pages if not page.is_closed()]
            state.page = remaining[-1] if remaining else await state.context.new_page()
            state.page.set_default_timeout(self.config.timeout_ms)
            self._wire_page(state, state.page)
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
        self,
        state: BrowserSession,
        seconds: float,
        selector: str | None = None,
        *,
        text: str | None = None,
        condition: str | None = None,
        wait_state: WaitState = "visible",
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        # A condition wait is bounded by the action timeout; a bare sleep is bounded
        # by ``seconds``.  Conflating the two would turn "wait up to 45s for text"
        # into a one-second wait.
        bounded = max(1, int(timeout_ms)) if timeout_ms is not None else max(1, int(seconds * 1000))
        matched = "elapsed"
        if condition is not None:
            await state.page.wait_for_function(condition, timeout=bounded)
            matched = "condition"
        elif text is not None:
            await state.page.get_by_text(text, exact=False).first.wait_for(
                state="visible", timeout=bounded
            )
            matched = "text"
        elif selector is not None:
            await state.page.locator(selector).first.wait_for(state=wait_state, timeout=bounded)
            matched = "selector"
        else:
            await asyncio.sleep(seconds)
        result = await self.info(state)
        result["waited"] = matched
        return result

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
        self._clear_diagnostics(state)
        if action == "back":
            await state.page.go_back(wait_until=wait_until, timeout=self.config.timeout_ms)
        elif action == "forward":
            await state.page.go_forward(wait_until=wait_until, timeout=self.config.timeout_ms)
        else:
            await state.page.reload(wait_until=wait_until, timeout=self.config.timeout_ms)
        await self._clear_snapshot(state)
        return await self.info(state)
