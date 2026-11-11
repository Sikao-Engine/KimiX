"""Tests for Chromium discovery and the browser launch ladder.

Discovery is covered through its injected hooks (``environ``, ``is_file``,
``read_app_path``, ``which``), so the Windows registry and ``Program Files``
matrix runs on every host platform.  Launch tests use a fake Playwright object:
no test here starts a real browser.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from kimi_cli.soul.approval import ApprovalResult
from kimi_cli.tools.browser_use import BrowserUse
from kimi_cli.tools.browser_use import backend as backend_module
from kimi_cli.tools.browser_use.backend import (
    BrowserBackendError,
    BrowserConfig,
    BrowserManager,
)
from kimi_cli.tools.browser_use.discovery import (
    DiscoveredBrowser,
    clean_executable_path,
    discover_browsers,
    registry_app_path,
)

DIST_PY = Path(__file__).resolve().parents[3] / "dist.py"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


@pytest.fixture(autouse=True)
def _clean_browser_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "KIMI_BROWSER_EXECUTABLE",
        "KIMI_BROWSER_CHANNEL",
        "KIMI_BROWSER_NO_SANDBOX",
        "KIMI_BROWSER_CDP_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _no_registry(executable: str) -> None:
    del executable
    return None


def _no_path(executable: str) -> None:
    del executable
    return None


# ---------------------------------------------------------------------------
# discovery: Windows
# ---------------------------------------------------------------------------


def test_windows_registry_wins_and_survives_relocation() -> None:
    """A registry hit must not depend on the Program Files environment at all."""
    relocated = {r"E:\Chrome\chrome.exe": "Chrome", r"E:\Edge\msedge.exe": "Edge"}

    def read_app_path(executable: str) -> str | None:
        return {
            "chrome.exe": r"E:\Chrome\chrome.exe",
            "msedge.exe": r"E:\Edge\msedge.exe",
        }.get(executable)

    found = discover_browsers(
        platform="win32",
        environ={"PROGRAMFILES": r"C:\missing", "LOCALAPPDATA": r"C:\missing-too"},
        is_file=lambda path: path in relocated,
        read_app_path=read_app_path,
        which=_no_path,
    )

    assert [browser.executable for browser in found] == [
        r"E:\Chrome\chrome.exe",
        r"E:\Edge\msedge.exe",
    ]
    assert [browser.label for browser in found] == ["Chrome (registry)", "Edge (registry)"]


def test_windows_install_roots_cover_side_by_side_channels() -> None:
    root = r"C:\Program Files"
    found = discover_browsers(
        platform="win32",
        environ={"PROGRAMFILES": root},
        is_file=lambda path: True,
        read_app_path=_no_registry,
        which=_no_path,
    )
    executables = [browser.executable for browser in found]

    assert executables[0] == rf"{root}\Google\Chrome\Application\chrome.exe"
    assert executables[1] == rf"{root}\Microsoft\Edge\Application\msedge.exe"
    assert rf"{root}\Google\Chrome Beta\Application\chrome.exe" in executables
    assert rf"{root}\Google\Chrome Dev\Application\chrome.exe" in executables
    assert rf"{root}\Google\Chrome SxS\Application\chrome.exe" in executables
    assert rf"{root}\BraveSoftware\Brave-Browser\Application\brave.exe" in executables
    assert rf"{root}\Chromium\Application\chrome.exe" in executables


def test_windows_dedupes_programw6432_case_insensitively() -> None:
    """%ProgramFiles% and %ProgramW6432% are one directory on 64-bit Windows."""
    found = discover_browsers(
        platform="win32",
        environ={
            "PROGRAMFILES": r"C:\Program Files",
            "PROGRAMW6432": r"c:\program files",
            "PROGRAMFILES(X86)": r"C:\Program Files (x86)",
            "LOCALAPPDATA": r"C:\Users\test\AppData\Local",
        },
        is_file=lambda path: path.lower() == CHROME.lower(),
        read_app_path=_no_registry,
        which=_no_path,
    )

    assert [browser.executable for browser in found] == [CHROME]


def test_windows_env_values_tolerate_quotes_case_and_padding() -> None:
    """`set PROGRAMFILES="C:\\PF"` must not defeat discovery."""
    found = discover_browsers(
        platform="win32",
        environ={"ProgramFiles": '  "C:\\PF"  '},
        is_file=lambda path: True,
        read_app_path=_no_registry,
        which=_no_path,
    )

    assert found[0].executable == r"C:\PF\Google\Chrome\Application\chrome.exe"
    assert found[0].source == "%PROGRAMFILES%"


def test_windows_missing_environment_does_not_raise() -> None:
    assert (
        discover_browsers(
            platform="win32",
            environ={},
            is_file=lambda path: True,
            read_app_path=_no_registry,
            which=_no_path,
        )
        == []
    )


def test_windows_path_lookup_is_the_last_resort() -> None:
    found = discover_browsers(
        platform="win32",
        environ={},
        is_file=lambda path: path == r"C:\tools\chrome.exe",
        read_app_path=_no_registry,
        which=lambda executable: r"C:\tools\chrome.exe" if executable == "chrome.exe" else None,
    )

    assert [(browser.executable, browser.source) for browser in found] == [
        (r"C:\tools\chrome.exe", "PATH")
    ]


def test_registry_app_path_reads_default_value_and_normalizes() -> None:
    class _Key:
        def __init__(self, value: Any) -> None:
            self.value = value

        def __enter__(self) -> _Key:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class _Winreg:
        HKEY_CURRENT_USER = "hkcu"
        HKEY_LOCAL_MACHINE = "hklm"
        KEY_READ = 0x20019

        def __init__(self, values: dict[str, Any]) -> None:
            self.values = values

        def OpenKey(self, hive: str, subkey: str, reserved: int, access: int) -> _Key:
            del reserved, access
            value = self.values.get(f"{hive}:{subkey}")
            if value is None:
                raise FileNotFoundError(subkey)
            return _Key(value)

        def QueryValueEx(self, key: _Key, name: str) -> Any:
            assert name == ""
            return key.value, 1

    subkey = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    module = _Winreg({"hklm:" + subkey: '  "D:\\Chrome\\chrome.exe"  '})

    assert registry_app_path("chrome.exe", module) == r"D:\Chrome\chrome.exe"
    assert registry_app_path("msedge.exe", module) is None


def test_clean_executable_path_normalizes_user_input() -> None:
    assert clean_executable_path('  "C:\\Chrome\\chrome.exe" ') == r"C:\Chrome\chrome.exe"
    assert clean_executable_path("   ") is None
    assert clean_executable_path(None) is None
    assert clean_executable_path(42) is None


# ---------------------------------------------------------------------------
# discovery: other platforms
# ---------------------------------------------------------------------------


def test_macos_discovery_probes_system_and_user_application_bundles() -> None:
    wanted = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    found = discover_browsers(
        platform="darwin",
        home="/Users/test",
        is_file=lambda path: path == wanted,
    )

    assert [browser.executable for browser in found] == [wanted]
    assert found[0].name == "Chrome"


def test_linux_discovery_prefers_path_then_well_known_locations() -> None:
    on_path = "/usr/local/bin/google-chrome"
    found = discover_browsers(
        platform="linux",
        is_file=lambda path: True,
        which=lambda executable: on_path if executable == "google-chrome" else None,
    )
    executables = [browser.executable for browser in found]

    assert executables[0] == on_path
    assert found[0].source == "PATH"
    assert "/opt/google/chrome/chrome" in executables
    assert "/snap/bin/chromium" in executables


def test_discovery_filters_missing_executables() -> None:
    found = discover_browsers(
        platform="win32",
        environ={"PROGRAMFILES": r"C:\Program Files"},
        is_file=lambda path: False,
        read_app_path=lambda executable: r"C:\gone\chrome.exe",
        which=_no_path,
    )

    assert found == []


def test_discovered_browser_label_falls_back_to_executable_name() -> None:
    assert DiscoveredBrowser(r"C:\x\msedge.exe", "PATH").label == "Edge (PATH)"
    assert DiscoveredBrowser(r"C:\x\custom.exe", "PATH").label == "custom.exe (PATH)"


# ---------------------------------------------------------------------------
# launch ladder
# ---------------------------------------------------------------------------


class FakeBrowser:
    def __init__(self, version: str = "999.0") -> None:
        self.version = version
        self.outcome = None
        self.page = SimpleNamespace(
            url="about:blank", title=self._title, is_closed=lambda: False
        )
        self.closed = False

    async def _title(self) -> str:
        return "Fixture"

    def is_connected(self) -> bool:
        return not self.closed

    async def close(self) -> None:
        self.closed = True

    async def new_context(self, **kwargs: Any) -> Any:
        del kwargs
        return SimpleNamespace(new_page=self._new_page, pages=[], close=self._close_context)

    async def _new_page(self) -> Any:
        return self.page

    async def _close_context(self) -> None:
        return None


class FakePlaywright:
    """Minimal stand-in for the Playwright object graph used by the manager."""

    def __init__(self, launch: Any) -> None:
        self.launch_calls: list[dict[str, Any]] = []
        self.stop_count = 0
        self._launch = launch
        self.chromium = SimpleNamespace(launch=self._launch_wrapper)

    async def _launch_wrapper(self, **kwargs: Any) -> FakeBrowser:
        self.launch_calls.append(kwargs)
        return await self._launch(**kwargs)

    async def stop(self) -> None:
        self.stop_count += 1


def _install_playwright(monkeypatch: pytest.MonkeyPatch, launch: Any) -> FakePlaywright:
    playwright = FakePlaywright(launch)

    class _Starter:
        async def start(self) -> FakePlaywright:
            return playwright

    monkeypatch.setattr(backend_module, "async_playwright", lambda: _Starter())
    return playwright


def _install_discovery(monkeypatch: pytest.MonkeyPatch, paths: list[str]) -> None:
    monkeypatch.setattr(
        backend_module,
        "discover_browsers",
        lambda: [DiscoveredBrowser(path, "registry") for path in paths],
    )


def _always_fail(message: str) -> Any:
    async def launch(**kwargs: Any) -> FakeBrowser:
        del kwargs
        raise RuntimeError(message)

    return launch


def test_launch_specs_order_configured_channel_detected_bundled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_discovery(monkeypatch, [CHROME, EDGE])
    manager = BrowserManager(
        BrowserConfig(executable_path=r"D:\Portable\chrome.exe", channel="msedge")
    )

    specs = manager.launch_specs()

    assert [spec.label for spec in specs] == [
        "configured executable",
        "msedge (channel)",
        "Chrome (registry)",
        "Edge (registry)",
        "Playwright-managed Chromium",
    ]
    assert specs[0].launch_kwargs() == {"executable_path": r"D:\Portable\chrome.exe"}
    assert specs[1].launch_kwargs() == {"channel": "msedge"}
    assert specs[-1].launch_kwargs() == {}


def test_launch_specs_dedupe_case_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME, CHROME.lower()])
    manager = BrowserManager(BrowserConfig())

    specs = manager.launch_specs()

    assert [spec.label for spec in specs] == ["Chrome (registry)", "Playwright-managed Chromium"]


def test_launch_falls_back_to_the_next_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME, EDGE])
    browser = FakeBrowser()

    async def launch(**kwargs: Any) -> FakeBrowser:
        if kwargs.get("executable_path") == CHROME:
            raise RuntimeError("Chrome is broken on this machine")
        return browser

    playwright = _install_playwright(monkeypatch, launch)
    manager = BrowserManager(BrowserConfig())

    result = asyncio.run(manager._ensure_browser())

    assert result is browser
    assert [call.get("executable_path") for call in playwright.launch_calls] == [CHROME, EDGE]
    assert manager.launcher_info() == {
        "browser": "Edge (registry)",
        "browser_version": "999.0",
        "executable": EDGE,
    }


def test_launch_failure_reports_every_attempt_and_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_discovery(monkeypatch, [CHROME])
    _install_playwright(monkeypatch, _always_fail("Executable doesn't exist at C:\\gone"))
    manager = BrowserManager(BrowserConfig())

    with pytest.raises(BrowserBackendError) as caught:
        asyncio.run(manager._ensure_browser())

    message = str(caught.value)
    assert caught.value.code == "browser_launch_failed"
    assert "Attempts:" in message
    assert "Chrome (registry)" in message
    assert "Playwright-managed Chromium" in message
    assert "playwright install chromium" in message


def test_launch_budget_skips_remaining_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME, EDGE])

    async def slow_failure(**kwargs: Any) -> FakeBrowser:
        del kwargs
        await asyncio.sleep(0.4)
        raise RuntimeError("nope")

    playwright = _install_playwright(monkeypatch, slow_failure)
    manager = BrowserManager(BrowserConfig(launch_budget=1.2, launch_timeout_ms=3_000))

    with pytest.raises(BrowserBackendError) as caught:
        asyncio.run(manager._ensure_browser())

    assert len(playwright.launch_calls) == 1
    assert "launch budget" in str(caught.value)


def test_launch_attempt_limit_stops_the_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME, EDGE])
    playwright = _install_playwright(monkeypatch, _always_fail("boom"))
    manager = BrowserManager(BrowserConfig(max_launch_attempts=1))

    with pytest.raises(BrowserBackendError) as caught:
        asyncio.run(manager._ensure_browser())

    assert len(playwright.launch_calls) == 1
    assert "attempt limit 1" in str(caught.value)


def test_sandbox_rejection_retries_once_without_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME])
    browser = FakeBrowser()

    async def launch(**kwargs: Any) -> FakeBrowser:
        if kwargs.get("chromium_sandbox"):
            raise RuntimeError("Failed to launch: Sandboxing is not supported on this host")
        return browser

    playwright = _install_playwright(monkeypatch, launch)
    manager = BrowserManager(BrowserConfig(sandbox=True))

    assert asyncio.run(manager._ensure_browser()) is browser
    assert [call["chromium_sandbox"] for call in playwright.launch_calls] == [True, False]
    assert manager._sandbox_enabled() is False


def test_sandbox_is_active_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME])
    playwright = _install_playwright(monkeypatch, _always_fail("boom"))
    manager = BrowserManager(BrowserConfig())

    with pytest.raises(BrowserBackendError):
        asyncio.run(manager._ensure_browser())

    assert playwright.launch_calls[0]["chromium_sandbox"] is True


def test_no_sandbox_environment_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME])
    playwright = _install_playwright(monkeypatch, _always_fail("boom"))
    monkeypatch.setenv("KIMI_BROWSER_NO_SANDBOX", "1")

    with pytest.raises(BrowserBackendError):
        asyncio.run(BrowserManager(BrowserConfig())._ensure_browser())

    assert playwright.launch_calls[0]["chromium_sandbox"] is False


def test_cancelled_launch_releases_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_discovery(monkeypatch, [CHROME])

    async def hang(**kwargs: Any) -> FakeBrowser:
        del kwargs
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    playwright = _install_playwright(monkeypatch, hang)
    manager = BrowserManager(BrowserConfig(launch_budget=60, launch_timeout_ms=60_000))

    async def run() -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(manager._ensure_browser(), timeout=0.05)
        for _ in range(10):
            await asyncio.sleep(0)
        assert playwright.stop_count >= 1
        assert manager._browser is None
        assert manager._launch_info is None

    asyncio.run(run())


def test_info_reports_the_browser_that_is_driving_the_session() -> None:
    manager = BrowserManager(BrowserConfig())
    manager._browser = FakeBrowser("123.0")
    manager._record_launch("Chrome (registry)", executable=CHROME)
    page = FakeBrowser().page
    state = SimpleNamespace(
        name="test",
        context=SimpleNamespace(pages=[page]),
        page=page,
        snapshot_id="snap",
        elements=[],
        last_used=0.0,
        borrowed_context=False,
        console=deque(maxlen=5),
        network_failures=deque(maxlen=5),
        wired=[],
    )

    info = asyncio.run(manager.info(state))

    assert info["browser"] == "Chrome (registry)"
    assert info["browser_version"] == "123.0"
    assert info["executable"] == CHROME
    assert info["snapshot_id"] == "snap"


# ---------------------------------------------------------------------------
# tool configuration plumbing
# ---------------------------------------------------------------------------


def _approval() -> MagicMock:
    approval = MagicMock()
    approval.request.return_value = ApprovalResult(True)
    return approval


def test_tool_config_reads_channel_budget_and_sandbox(runtime: Any, session: Any) -> None:
    session.custom_config = {
        "config_json": {
            "browser_use": {
                "channel": "msedge",
                "launch_timeout_ms": 5_000,
                "launch_budget": 10,
                "max_launch_attempts": 2,
                "sandbox": True,
            }
        }
    }

    config = BrowserUse(runtime, _approval(), session)._config()

    assert config.channel == "msedge"
    assert config.launch_timeout_ms == 5_000
    assert config.launch_budget == 10.0
    assert config.max_launch_attempts == 2
    assert config.sandbox is True


def test_tool_config_rejects_string_booleans_and_out_of_range_values(
    runtime: Any, session: Any
) -> None:
    session.custom_config = {
        "config_json": {
            "browser_use": {
                "sandbox": "false",
                "headed": "false",
                "launch_timeout_ms": 10,
                "max_launch_attempts": 99,
            }
        }
    }

    config = BrowserUse(runtime, _approval(), session)._config()

    assert config.sandbox is True
    assert config.headed is False
    assert config.launch_timeout_ms == 3_000
    assert config.max_launch_attempts == 12


# ---------------------------------------------------------------------------
# offline bundle wiring
# ---------------------------------------------------------------------------


def _dist_module() -> Any:
    spec = importlib.util.spec_from_file_location("kimix_dist_for_tests", DIST_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_bundle_launchers_wire_playwright_browsers_path() -> None:
    dist = _dist_module()

    assert "PLAYWRIGHT_BROWSERS_PATH=%KIMIX_HOME%pw-browsers" in dist.windows_launcher()
    assert 'export PLAYWRIGHT_BROWSERS_PATH="$KIMIX_HOME/pw-browsers"' in dist.linux_launcher()


def test_offline_bundle_launchers_keep_an_operator_override() -> None:
    """A pre-set PLAYWRIGHT_BROWSERS_PATH must win over the bundled directory."""
    dist = _dist_module()

    assert (
        'if not defined PLAYWRIGHT_BROWSERS_PATH if exist "%KIMIX_HOME%pw-browsers"'
        in dist.windows_launcher()
    )
    assert '[ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]' in dist.linux_launcher()


def test_offline_bundle_includes_chromium_unless_disabled() -> None:
    dist = _dist_module()

    assert dist.parse_args([]).with_playwright is True
    assert dist.parse_args(["--no-playwright"]).with_playwright is False
    assert dist.parse_args(["--with-playwright"]).with_playwright is True


def test_playwright_is_not_started_for_a_cdp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """CDP attaches to an existing browser: discovery and launching must not run."""
    playwright = _install_playwright(monkeypatch, _always_fail("must not launch"))
    playwright.chromium.connect_over_cdp = _fake_connect
    manager = BrowserManager(BrowserConfig(cdp_url="http://127.0.0.1:9222"))

    browser = asyncio.run(manager._ensure_browser())

    assert browser.version == "999.0"
    assert playwright.launch_calls == []
    assert manager.launcher_info()["browser"] == "CDP endpoint"


async def _fake_connect(url: str, timeout: int) -> FakeBrowser:
    del url, timeout
    return FakeBrowser()

