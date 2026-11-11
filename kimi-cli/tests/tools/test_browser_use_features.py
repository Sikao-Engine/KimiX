"""Tests for the extended browser action surface and its safety rails.

Covers CDP endpoint validation, origin allow/deny, container launch flags, attach
reuse, storage state, page diagnostics, retry dispositions, and the select / upload /
coordinate-click / wait-condition / structured-extract actions.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import ValidationError

from kimi_cli.soul.approval import ApprovalResult
from kimi_cli.tools.browser_use import BrowserUse, Params
from kimi_cli.tools.browser_use import backend as backend_module
from kimi_cli.tools.browser_use.backend import (
    BrowserBackendError,
    BrowserConfig,
    BrowserManager,
    BrowserSession,
    resolve_workspace_file,
)
from kimi_cli.tools.browser_use.policy import (
    BrowserPolicyError,
    enforce_origin_policy,
    origin_pattern_matches,
    validate_cdp_url,
)
from kosong.tooling import ToolError, ToolOk


def _page(url: str = "about:blank", title: str = "Fixture") -> Any:
    page = MagicMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.is_closed.return_value = False
    return page


def _state(page: Any) -> BrowserSession:
    return BrowserSession(name="test", context=SimpleNamespace(pages=[page]), page=page)


def _approval() -> MagicMock:
    approval = MagicMock()
    approval.request = AsyncMock(return_value=ApprovalResult(True))
    return approval


def _tool(runtime: Any, session: Any, manager: Any) -> BrowserUse:
    if not isinstance(getattr(manager, "config", None), BrowserConfig):
        manager.config = BrowserConfig()
    # Every call invalidates the snapshot in a finally block.
    manager.invalidate_snapshot = AsyncMock()
    return BrowserUse(runtime, _approval(), session, manager=manager)


# ---------------------------------------------------------------------------
# CDP endpoint policy
# ---------------------------------------------------------------------------


def test_cdp_endpoint_accepts_only_loopback_by_default() -> None:
    for endpoint in (
        "http://127.0.0.1:9222",
        "http://localhost:9222",
        "ws://[::1]:9222/devtools/browser/abc",
        "http://127.4.5.6:9222",
    ):
        assert validate_cdp_url(endpoint) == endpoint

    with pytest.raises(BrowserPolicyError) as caught:
        validate_cdp_url("http://10.1.2.3:9222")
    assert caught.value.code == "cdp_remote_refused"
    assert "allow_remote_cdp" in str(caught.value)
    assert "SSH tunnel" in str(caught.value)

    assert validate_cdp_url("http://10.1.2.3:9222", allow_remote=True) == "http://10.1.2.3:9222"


def test_cdp_endpoint_rejects_bad_schemes_and_embedded_credentials() -> None:
    with pytest.raises(BrowserPolicyError) as caught:
        validate_cdp_url("ftp://127.0.0.1:9222")
    assert caught.value.code == "invalid_cdp_url"

    with pytest.raises(BrowserPolicyError) as caught:
        validate_cdp_url("http://user:secret@127.0.0.1:9222")
    assert caught.value.code == "secret_in_url"


# ---------------------------------------------------------------------------
# Origin allow / deny
# ---------------------------------------------------------------------------


def test_origin_patterns_cover_wildcards_origins_and_hosts() -> None:
    url = "https://app.example.com/page"
    assert origin_pattern_matches("*", url)
    assert origin_pattern_matches("https://app.example.com", url)
    assert not origin_pattern_matches("http://app.example.com", url)
    assert origin_pattern_matches("https://app.example.com:443/", url)
    assert origin_pattern_matches("*.example.com", url)
    assert origin_pattern_matches("*.example.com", "https://example.com/")
    assert not origin_pattern_matches("*.example.com", "https://example.com.evil.test/")
    assert origin_pattern_matches("app.example.com", url)
    assert not origin_pattern_matches("example.com", url)


def test_origin_policy_blocks_first_then_requires_an_allow_match() -> None:
    with pytest.raises(BrowserPolicyError) as caught:
        enforce_origin_policy(
            "https://bank.test/account",
            allowed=("*",),
            blocked=("https://bank.test",),
        )
    assert caught.value.code == "origin_blocked"

    enforce_origin_policy("https://docs.test/a")  # empty lists allow everything
    enforce_origin_policy("https://docs.test/a", allowed=("*.test",))

    with pytest.raises(BrowserPolicyError) as caught:
        enforce_origin_policy("https://other.dev/a", allowed=("*.test",))
    assert caught.value.code == "origin_blocked"
    assert "allowed_origins" in str(caught.value)


# ---------------------------------------------------------------------------
# Launch configuration
# ---------------------------------------------------------------------------


def test_launch_args_carry_the_viewport_and_operator_flags(monkeypatch: Any) -> None:
    monkeypatch.setattr(backend_module, "_dev_shm_free_bytes", lambda path="/dev/shm": None)
    config = BrowserConfig(
        viewport_width=1600,
        viewport_height=1000,
        extra_args=("--disable-blink-features=AutomationControlled",),
    )

    args = BrowserManager._launch_args_for(config)

    assert "--window-size=1600,1000" in args
    assert args[-1] == "--disable-blink-features=AutomationControlled"
    assert "--disable-dev-shm-usage" not in args


def test_a_small_dev_shm_adds_the_container_workaround(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        backend_module, "_dev_shm_free_bytes", lambda path="/dev/shm": 64 * 1024 * 1024
    )

    assert "--disable-dev-shm-usage" in BrowserManager._launch_args_for(BrowserConfig())


def test_default_launch_args_are_unchanged_without_a_shm_shortage(monkeypatch: Any) -> None:
    monkeypatch.setattr(backend_module, "_dev_shm_free_bytes", lambda path="/dev/shm": None)

    args = BrowserManager._launch_args_for(BrowserConfig())

    assert args == [
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-sync",
        "--window-size=1280,900",
    ]


async def test_remote_cdp_is_refused_before_any_driver_starts(monkeypatch: Any) -> None:
    def refuse() -> Any:
        raise AssertionError("Playwright must not start for a refused endpoint")

    monkeypatch.setattr(backend_module, "async_playwright", refuse)
    manager = BrowserManager(BrowserConfig(cdp_url="http://10.1.2.3:9222"))

    with pytest.raises(BrowserBackendError) as caught:
        await manager._ensure_browser()

    assert caught.value.code == "cdp_remote_refused"
    assert caught.value.retryable is False
    assert manager._playwright is None


async def test_remote_cdp_is_allowed_when_the_operator_opts_in(monkeypatch: Any) -> None:
    browser = MagicMock()
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp = AsyncMock(return_value=browser)
    fake_api = lambda: SimpleNamespace(start=AsyncMock(return_value=playwright))  # noqa: E731
    monkeypatch.setattr(backend_module, "async_playwright", fake_api)
    manager = BrowserManager(BrowserConfig(cdp_url="http://10.1.2.3:9222", allow_remote_cdp=True))

    assert await manager._ensure_browser() is browser
    playwright.chromium.connect_over_cdp.assert_awaited_once()
    assert manager._owns_browser is False


# ---------------------------------------------------------------------------
# Session creation: viewport, attach reuse, storage state
# ---------------------------------------------------------------------------


def _browser_with_context(context: Any) -> Any:
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.contexts = []
    return browser


def context_kwargs(manager: BrowserManager) -> dict[str, Any]:
    """Read the kwargs of the last ``new_context`` call on the manager's browser."""
    browser = manager._ensure_browser.return_value  # type: ignore[attr-defined]
    return dict(browser.new_context.await_args.kwargs)


async def test_owned_sessions_use_the_configured_viewport() -> None:
    page = _page()
    context = MagicMock(pages=[page])
    context.new_page = AsyncMock(return_value=page)
    manager = BrowserManager(BrowserConfig(viewport_width=800, viewport_height=600))
    manager._ensure_browser = AsyncMock(return_value=_browser_with_context(context))
    manager._owns_browser = True

    state = await manager.get_session("default")

    kwargs = context_kwargs(manager)
    assert kwargs["viewport"] == {"width": 800, "height": 600}
    assert kwargs["accept_downloads"] is False
    assert "storage_state" not in kwargs
    assert state.borrowed_context is False


async def test_attach_can_reuse_the_browsers_existing_context() -> None:
    page = _page(url="https://intranet.test/")
    context = MagicMock(pages=[page])
    browser = MagicMock(contexts=[context])
    manager = BrowserManager(BrowserConfig(reuse_default_context=True))
    manager._ensure_browser = AsyncMock(return_value=browser)
    manager._owns_browser = False

    state = await manager.get_session("work")

    assert state.page is page
    assert state.borrowed_context is True
    browser.new_context.assert_not_called()
    page.set_default_timeout.assert_called_once_with(manager.config.timeout_ms)
    page.on.assert_called()

    context.close = AsyncMock()
    context.storage_state = AsyncMock()
    assert await manager.close_session("work") is True
    context.close.assert_not_awaited()


async def test_owned_browsers_ignore_the_reuse_setting() -> None:
    page = _page()
    context = MagicMock(pages=[page])
    context.new_page = AsyncMock(return_value=page)
    manager = BrowserManager(BrowserConfig(reuse_default_context=True))
    manager._ensure_browser = AsyncMock(return_value=_browser_with_context(context))
    manager._owns_browser = True

    state = await manager.get_session("default")

    assert state.borrowed_context is False


async def test_storage_state_is_loaded_and_saved_for_owned_browsers(tmp_path: Any) -> None:
    store = tmp_path / "storage.json"
    store.write_text('{"cookies": []}', encoding="utf-8")
    page = _page()
    context = MagicMock(pages=[page])
    context.new_page = AsyncMock(return_value=page)
    context.storage_state = AsyncMock()
    manager = BrowserManager(BrowserConfig(storage_state=str(store)))
    manager._ensure_browser = AsyncMock(return_value=_browser_with_context(context))
    manager._owns_browser = True

    await manager.get_session("default")

    assert context_kwargs(manager)["storage_state"] == str(store)
    # close_idle_sessions() stops the idle browser, which clears ownership; the
    # real _launch_owned_browser sets it back when a session is (re)created.
    manager._owns_browser = True
    await manager.close_session("default")
    context.storage_state.assert_awaited_once_with(path=str(store))


async def test_storage_state_never_dumps_an_attached_browser(tmp_path: Any) -> None:
    store = tmp_path / "storage.json"
    context = MagicMock()
    context.close = AsyncMock()
    context.storage_state = AsyncMock()
    manager = BrowserManager(BrowserConfig(storage_state=str(store)))
    manager._owns_browser = False

    await manager._close_session(BrowserSession(name="x", context=context, page=_page()))

    context.storage_state.assert_not_awaited()


async def test_missing_storage_state_file_is_not_passed(tmp_path: Any) -> None:
    page = _page()
    context = MagicMock(pages=[page])
    context.new_page = AsyncMock(return_value=page)
    manager = BrowserManager(BrowserConfig(storage_state=str(tmp_path / "absent.json")))
    manager._ensure_browser = AsyncMock(return_value=_browser_with_context(context))
    manager._owns_browser = True

    await manager.get_session("default")

    assert "storage_state" not in context_kwargs(manager)


# ---------------------------------------------------------------------------
# Page diagnostics in the state receipt
# ---------------------------------------------------------------------------


def _handlers(page: Any) -> dict[str, Any]:
    return {call.args[0]: call.args[1] for call in page.on.call_args_list}


async def test_console_errors_and_failed_requests_reach_the_result() -> None:
    page = _page()
    manager = BrowserManager(BrowserConfig(console_events=2))
    state = _state(page)

    manager._wire_page(state, page)
    manager._wire_page(state, page)  # idempotent per page

    handlers = _handlers(page)
    assert set(handlers) == {"console", "pageerror", "requestfailed"}
    page.on.assert_any_call("console", handlers["console"])

    handlers["console"](SimpleNamespace(type="error", text="boom"))
    handlers["console"](SimpleNamespace(type="log", text="ignored"))
    handlers["pageerror"](ValueError("bad script"))
    handlers["requestfailed"](
        SimpleNamespace(url="https://x.test/a?access_token=secret", failure="net::ERR_FAILED")
    )

    info = await manager.info(state)

    assert [entry["type"] for entry in info["console"]] == ["error", "pageerror"]
    assert info["console"][1]["text"] == "bad script"
    assert info["network_failures"] == [
        {"url": "https://x.test/a?redacted=1", "reason": "net::ERR_FAILED"}
    ]


async def test_quiet_pages_pay_nothing_for_diagnostics() -> None:
    manager = BrowserManager(BrowserConfig())
    info = await manager.info(_state(_page()))

    assert "console" not in info
    assert "network_failures" not in info


async def test_console_events_zero_disables_capture() -> None:
    page = _page()
    manager = BrowserManager(BrowserConfig(console_events=0))
    state = _state(page)

    manager._wire_page(state, page)
    _handlers(page)["console"](SimpleNamespace(type="error", text="boom"))

    assert list(state.console) == []
    assert "console" not in await manager.info(state)


async def test_cancelled_requests_are_not_reported_as_failures() -> None:
    page = _page()
    manager = BrowserManager(BrowserConfig())
    state = _state(page)
    manager._wire_page(state, page)

    _handlers(page)["requestfailed"](
        SimpleNamespace(url="https://x.test/nav.png", failure="net::ERR_ABORTED")
    )

    assert list(state.network_failures) == []


async def test_navigation_starts_with_a_clean_receipt() -> None:
    page = _page()
    page.goto = AsyncMock()
    manager = BrowserManager(BrowserConfig())
    state = _state(page)
    manager._wire_page(state, page)
    _handlers(page)["console"](SimpleNamespace(type="error", text="old page"))

    await manager.navigate(state, "https://example.test/next", "domcontentloaded")

    assert list(state.console) == []
    page.goto.assert_awaited_once()


async def test_diagnostics_are_bounded() -> None:
    page = _page()
    manager = BrowserManager(BrowserConfig(console_events=2))
    state = _state(page)
    manager._wire_page(state, page)

    for index in range(5):
        _handlers(page)["console"](SimpleNamespace(type="error", text=f"e{index}"))

    assert [entry["text"] for entry in state.console] == ["e3", "e4"]


# ---------------------------------------------------------------------------
# Retry disposition
# ---------------------------------------------------------------------------


def test_error_codes_carry_a_retry_disposition() -> None:
    assert BrowserBackendError("x", "stale_snapshot").retryable is True
    assert BrowserBackendError("x", "snapshot_required").retryable is True
    assert BrowserBackendError("x", "element_not_found").retryable is True
    assert BrowserBackendError("x", "stale_element").retryable is True
    assert BrowserBackendError("x", "browser_launch_failed").retryable is False
    assert BrowserBackendError("x", "origin_blocked").retryable is False
    assert BrowserBackendError("x", "anything", retryable=True).retryable is True


async def test_retryable_disposition_reaches_the_model(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(
        side_effect=BrowserBackendError("Take a fresh snapshot.", "snapshot_required")
    )
    tool = _tool(runtime, session, manager)

    result = await tool(Params(action="snapshot"))

    assert isinstance(result, ToolError)
    assert "error_code: snapshot_required" in result.message
    assert "retryable: true" in result.message


async def test_timeouts_stay_undetermined(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(side_effect=PlaywrightTimeoutError("slow"))
    tool = _tool(runtime, session, manager)

    result = await tool(Params(action="snapshot"))

    assert isinstance(result, ToolError)
    assert "error_code: timeout_outcome_unknown" in result.message
    assert "retryable:" not in result.message


# ---------------------------------------------------------------------------
# select / upload / coordinate click
# ---------------------------------------------------------------------------


def _page_with_locator(page: Any, target: Any) -> Any:
    page.locator = MagicMock(return_value=SimpleNamespace(first=target))
    return page


async def test_select_returns_the_applied_options() -> None:
    page = _page()
    target = MagicMock()
    target.select_option = AsyncMock(return_value=["large"])
    _page_with_locator(page, target)
    manager = BrowserManager(BrowserConfig())

    result = await manager.select(_state(page), None, "select#size", None, ["large"])

    target.select_option.assert_awaited_once_with(["large"], timeout=manager.config.timeout_ms)
    assert result["selected"] == ["large"]


async def test_upload_confines_paths_to_the_workspace(tmp_path: Any) -> None:
    inside = tmp_path / "shot.png"
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    page = _page()
    target = MagicMock()
    target.set_input_files = AsyncMock()
    _page_with_locator(page, target)
    manager = BrowserManager(BrowserConfig())
    state = _state(page)

    result = await manager.upload(state, None, "input[type=file]", None, ["shot.png"], tmp_path)

    target.set_input_files.assert_awaited_once_with(
        [str(inside)], timeout=manager.config.timeout_ms
    )
    assert result["files"] == [str(inside)]

    with pytest.raises(BrowserBackendError) as caught:
        await manager.upload(state, None, "input[type=file]", None, ["../outside.txt"], tmp_path)
    assert caught.value.code == "upload_outside_workspace"
    assert caught.value.retryable is False

    with pytest.raises(BrowserBackendError) as caught:
        await manager.upload(state, None, "input[type=file]", None, ["missing.png"], tmp_path)
    assert caught.value.code == "invalid_upload_path"


async def test_upload_needs_a_workspace_and_a_bounded_file_count(tmp_path: Any) -> None:
    manager = BrowserManager(BrowserConfig())
    state = _state(_page())

    with pytest.raises(BrowserBackendError) as caught:
        await manager.upload(state, None, "input", None, ["a.txt"], None)
    assert caught.value.code == "upload_unavailable"

    paths = [f"f{index}.txt" for index in range(11)]
    with pytest.raises(BrowserBackendError) as caught:
        await manager.upload(state, None, "input", None, paths, tmp_path)
    assert caught.value.code == "invalid_upload_path"


async def test_coordinate_click_presses_and_releases_modifiers() -> None:
    page = _page()
    page.mouse.click = AsyncMock()
    page.keyboard.down = AsyncMock()
    page.keyboard.up = AsyncMock()
    manager = BrowserManager(BrowserConfig())

    await manager.click(_state(page), None, None, None, "left", 2, ["Control"], (10, 20))

    page.mouse.click.assert_awaited_once_with(10, 20, button="left", click_count=2)
    page.keyboard.down.assert_awaited_once_with("Control")
    page.keyboard.up.assert_awaited_once_with("Control")


async def test_coordinate_click_releases_modifiers_after_a_failure() -> None:
    page = _page()
    page.mouse.click = AsyncMock(side_effect=RuntimeError("gone"))
    page.keyboard.down = AsyncMock()
    page.keyboard.up = AsyncMock()
    manager = BrowserManager(BrowserConfig())

    with pytest.raises(RuntimeError):
        await manager.click(_state(page), None, None, None, "left", 1, ["Shift"], (1, 2))

    page.keyboard.up.assert_awaited_once_with("Shift")


async def test_element_clicks_still_use_the_playwright_target() -> None:
    page = _page()
    target = MagicMock()
    target.click = AsyncMock()
    _page_with_locator(page, target)
    manager = BrowserManager(BrowserConfig())

    await manager.click(_state(page), None, "#go", None, "left", 1, ["Control"])

    target.click.assert_awaited_once()
    page.mouse.click.assert_not_called()


# ---------------------------------------------------------------------------
# wait conditions
# ---------------------------------------------------------------------------


async def test_wait_matches_text_predicates_and_states() -> None:
    page = _page()
    text_waiter = MagicMock()
    text_waiter.wait_for = AsyncMock()
    selector_waiter = MagicMock()
    selector_waiter.wait_for = AsyncMock()
    page.get_by_text = MagicMock(return_value=SimpleNamespace(first=text_waiter))
    page.locator = MagicMock(return_value=SimpleNamespace(first=selector_waiter))
    page.wait_for_function = AsyncMock()
    manager = BrowserManager(BrowserConfig())
    state = _state(page)

    assert (await manager.wait(state, 1.0, text="Done", timeout_ms=5_000))["waited"] == "text"
    text_waiter.wait_for.assert_awaited_once_with(state="visible", timeout=5_000)

    assert (
        await manager.wait(state, 1.0, condition="() => true", timeout_ms=4_000)
    )["waited"] == "condition"
    page.wait_for_function.assert_awaited_once_with("() => true", timeout=4_000)

    result = await manager.wait(state, 1.0, "#ready", wait_state="hidden", timeout_ms=2_000)
    assert result["waited"] == "selector"
    selector_waiter.wait_for.assert_awaited_once_with(state="hidden", timeout=2_000)

    assert (await manager.wait(state, 0.01))["waited"] == "elapsed"


async def test_condition_waits_are_bounded_by_the_action_timeout(
    runtime: Any, session: Any
) -> None:
    manager = MagicMock()
    manager.config = BrowserConfig()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.wait = AsyncMock(return_value={})
    tool = _tool(runtime, session, manager)

    await tool(Params(action="wait", condition="() => true", timeout=5))

    assert manager.wait.await_args.kwargs["timeout_ms"] == 5_000


# ---------------------------------------------------------------------------
# extract: structured records, main content, HTML
# ---------------------------------------------------------------------------


async def test_extract_fields_returns_one_record_per_match() -> None:
    page = _page()
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(
        return_value={"values": [{"title": "A", "href": "/a"}], "truncated": False}
    )
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    result = await manager.extract(
        _state(page),
        ".card",
        None,
        True,
        10,
        fields={"title": "h2", "href": "a|href"},
    )

    assert result["records"] == [{"title": "A", "href": "/a"}]
    assert result["truncated"] is False
    script, payload = locator.evaluate_all.await_args.args
    assert payload == {"fields": {"title": "h2", "href": "a|href"}, "limit": 10}
    assert "querySelector" in script


async def test_extract_fields_requires_a_record_selector() -> None:
    manager = BrowserManager(BrowserConfig())

    with pytest.raises(BrowserBackendError) as caught:
        await manager.extract(_state(_page()), None, None, True, 10, fields={"t": "@self"})

    assert caught.value.code == "target_required"
    assert caught.value.retryable is True


async def test_extract_main_content_and_html_modes() -> None:
    page = _page()
    page.evaluate = AsyncMock(return_value={"text": "Body", "chars": 9, "container": True})
    manager = BrowserManager(BrowserConfig())
    state = _state(page)

    result = await manager.extract(state, None, None, False, 20, main_content=True)

    assert result["main_content"] == "Body"
    assert result["characters"] == 9
    assert result["truncated"] is True
    assert result["container"] is True

    page.locator = MagicMock(
        return_value=SimpleNamespace(
            first=SimpleNamespace(inner_html=AsyncMock(return_value="<p>x</p>"))
        )
    )
    html = await manager.extract(state, "main", None, False, 20, content_format="html")

    assert html["html"] == "<p>x</p>"


async def test_text_extraction_is_unchanged(runtime: Any) -> None:
    page = _page()
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(return_value={"values": ["one"], "truncated": False})
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    result = await manager.extract(_state(page), ".record", None, False, 20)

    assert result["values"] == ["one"]


# ---------------------------------------------------------------------------
# Element screenshots
# ---------------------------------------------------------------------------


async def test_element_screenshots_scope_the_capture() -> None:
    page = _page()
    target = MagicMock()
    target.screenshot = AsyncMock(return_value=b"png")
    _page_with_locator(page, target)
    page.screenshot = AsyncMock(return_value=b"full")
    manager = BrowserManager(BrowserConfig())
    state = _state(page)

    summary, data = await manager.screenshot(state, False, None, "#chart", None)

    assert data == b"png"
    assert summary["scope"] == "element"
    page.screenshot.assert_not_called()

    summary, data = await manager.screenshot(state, True)

    assert data == b"full"
    assert summary["scope"] == "full_page"


# ---------------------------------------------------------------------------
# Tool-layer wiring
# ---------------------------------------------------------------------------


def test_params_reject_ambiguous_click_addressing() -> None:
    with pytest.raises(ValidationError, match="exactly one of element or selector"):
        Params(action="click", element=1, selector="#a")
    with pytest.raises(ValidationError, match="both x and y"):
        Params(action="click", x=10)
    with pytest.raises(ValidationError, match="exactly one of element, selector, or both"):
        Params(action="click", element=1, x=5, y=6)
    with pytest.raises(ValidationError, match="exactly one of element, selector, or both"):
        Params(action="click")

    assert Params(action="click", x=5, y=6).x == 5


def test_params_require_action_specific_inputs() -> None:
    with pytest.raises(ValidationError, match="select requires values"):
        Params(action="select", selector="#size")
    with pytest.raises(ValidationError, match="upload requires paths"):
        Params(action="upload", selector="#file")
    with pytest.raises(ValidationError, match="fields extraction requires selector"):
        Params(action="extract", fields={"t": "@self"})

    assert Params(action="select", selector="#size", values=["s"]).values == ["s"]
    assert Params(action="upload", selector="#file", paths=["a.png"]).paths == ["a.png"]


async def test_click_dispatch_forwards_coordinates(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.click = AsyncMock(return_value={})
    tool = _tool(runtime, session, manager)

    result = await tool(Params(action="click", x=30, y=40))

    assert isinstance(result, ToolOk)
    assert manager.click.await_args.args[-1] == (30, 40)


async def test_select_and_upload_dispatch(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.select = AsyncMock(return_value={"selected": ["s"]})
    manager.upload = AsyncMock(return_value={"files": ["a.png"]})
    tool = _tool(runtime, session, manager)

    await tool(Params(action="select", selector="#size", values=["s"]))
    manager.select.assert_awaited_once()

    await tool(Params(action="upload", selector="#file", paths=["a.png"]))
    manager.upload.assert_awaited_once()
    assert manager.upload.await_args.args[4] == ["a.png"]
    assert manager.upload.await_args.args[5] == session.work_dir.unsafe_to_local_path()


async def test_extract_dispatch_forwards_record_options(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.extract = AsyncMock(return_value={})
    tool = _tool(runtime, session, manager)

    await tool(
        Params(
            action="extract",
            selector=".card",
            fields={"t": "@self"},
            main_content=True,
            format="html",
        )
    )

    kwargs = manager.extract.await_args.kwargs
    assert kwargs["fields"] == {"t": "@self"}
    assert kwargs["main_content"] is True
    assert kwargs["content_format"] == "html"


async def test_screenshot_dispatch_forwards_the_element_target(
    runtime: Any, session: Any
) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.screenshot = AsyncMock(return_value=({"scope": "element"}, b"png"))
    tool = _tool(runtime, session, manager)

    result = await tool(Params(action="screenshot", selector="#chart"))

    assert isinstance(result, ToolOk)
    assert manager.screenshot.await_args.args[1:] == (False, None, "#chart", None)


async def test_wait_with_a_predicate_needs_approval(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.wait = AsyncMock(return_value={})
    tool = _tool(runtime, session, manager)

    await tool(Params(action="wait", condition="() => true", timeout=5))

    tool._approval.request.assert_awaited_once()
    assert tool._approval.request.await_args.args[1] == "browser_use:wait"


async def test_plain_waits_need_no_approval(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.wait = AsyncMock(return_value={})
    tool = _tool(runtime, session, manager)

    await tool(Params(action="wait", seconds=1))

    tool._approval.request.assert_not_awaited()


async def test_blocked_origin_is_refused_before_navigation(
    runtime: Any, session: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("KIMI_ALLOW_PRIVATE_URLS", "true")
    manager = MagicMock()
    manager.config = BrowserConfig(blocked_origins=("http://127.0.0.1:8765",))
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.navigate = AsyncMock(return_value={})
    tool = _tool(runtime, session, manager)

    result = await tool(Params(action="navigate", url="http://127.0.0.1:8765/private"))

    assert isinstance(result, ToolError)
    assert "error_code: origin_blocked" in result.message
    assert "retryable: false" in result.message
    manager.get_session.assert_not_awaited()


async def test_allowed_origin_list_permits_matching_navigation(
    runtime: Any, session: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("KIMI_ALLOW_PRIVATE_URLS", "true")
    manager = MagicMock()
    manager.config = BrowserConfig(allowed_origins=("http://127.0.0.1:8765",))
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.navigate = AsyncMock(return_value={})
    tool = _tool(runtime, session, manager)

    result = await tool(Params(action="navigate", url="http://127.0.0.1:8765/ok"))

    assert isinstance(result, ToolOk)
    manager.navigate.assert_awaited_once()


# ---------------------------------------------------------------------------
# Configuration plumbing
# ---------------------------------------------------------------------------


def test_config_parses_the_new_keys(runtime: Any, session: Any) -> None:
    session.custom_config = {
        "config_json": {
            "browser_use": {
                "viewport_width": 1600,
                "viewport_height": 1000,
                "extra_args": ["--disable-blink-features=AutomationControlled"],
                "allowed_origins": ["*.example.com"],
                "blocked_origins": ["bank.test"],
                "console_events": 3,
                "reuse_default_context": True,
                "allow_remote_cdp": True,
                "storage_state": "state.json",
            }
        }
    }

    config = BrowserUse(runtime, _approval(), session)._config()

    assert config.viewport_width == 1600
    assert config.viewport_height == 1000
    assert config.extra_args == ("--disable-blink-features=AutomationControlled",)
    assert config.allowed_origins == ("*.example.com",)
    assert config.blocked_origins == ("bank.test",)
    assert config.console_events == 3
    assert config.reuse_default_context is True
    assert config.allow_remote_cdp is True
    assert config.storage_state == str(session.dir / "state.json")


def test_config_clamps_and_rejects_bad_values(runtime: Any, session: Any) -> None:
    session.custom_config = {
        "config_json": {
            "browser_use": {
                "viewport_width": 10,
                "viewport_height": 99_999,
                "console_events": 999,
                "extra_args": "not-a-list",
                "allowed_origins": ["ok.test", "", 3],
                "allow_remote_cdp": "true",
            }
        }
    }

    config = BrowserUse(runtime, _approval(), session)._config()

    assert config.viewport_width == 320
    assert config.viewport_height == 2160
    assert config.console_events == 50
    assert config.extra_args == ()
    assert config.allowed_origins == ("ok.test", "3")
    assert config.allow_remote_cdp is False


def test_remote_cdp_can_be_enabled_from_the_environment(
    runtime: Any, session: Any, monkeypatch: Any
) -> None:
    monkeypatch.setenv("KIMI_BROWSER_ALLOW_REMOTE_CDP", "1")
    session.custom_config = {}

    assert BrowserUse(runtime, _approval(), session)._config().allow_remote_cdp is True


def test_upload_paths_resolve_inside_the_workspace(tmp_path: Any) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    target = nested / "a.txt"
    target.write_text("x", encoding="utf-8")

    assert resolve_workspace_file("nested/a.txt", tmp_path) == target
    assert resolve_workspace_file(str(target), tmp_path) == target

    with pytest.raises(BrowserBackendError) as caught:
        resolve_workspace_file("", tmp_path)
    assert caught.value.code == "invalid_upload_path"

    with pytest.raises(BrowserBackendError) as caught:
        resolve_workspace_file("../escape.txt", tmp_path)
    assert caught.value.code == "invalid_upload_path"


async def test_session_state_starts_with_bounded_buffers() -> None:
    page = _page()
    context = MagicMock(pages=[page])
    context.new_page = AsyncMock(return_value=page)
    manager = BrowserManager(BrowserConfig(console_events=4))
    manager._ensure_browser = AsyncMock(return_value=_browser_with_context(context))
    manager._owns_browser = True

    state = await manager.get_session("default")

    assert state.console.maxlen == 4
    assert state.network_failures.maxlen == 4
    assert isinstance(state.console, deque)
    assert state.wired == [page]


def test_work_dir_is_reported_from_the_runtime(runtime: Any, session: Any) -> None:
    tool = BrowserUse(runtime, _approval(), session, manager=MagicMock())

    assert tool._work_dir() == session.work_dir.unsafe_to_local_path()


def test_work_dir_is_none_when_the_runtime_cannot_expose_it(
    runtime: Any, session: Any, monkeypatch: Any
) -> None:
    tool = BrowserUse(runtime, _approval(), session, manager=MagicMock())
    broken = MagicMock()
    broken.__str__ = MagicMock(side_effect=ValueError("no local path"))
    monkeypatch.setattr(runtime, "builtin_args", SimpleNamespace(KIMI_WORK_DIR=broken))

    assert tool._work_dir() is None
