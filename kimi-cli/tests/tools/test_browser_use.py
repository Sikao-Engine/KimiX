from __future__ import annotations

import asyncio
import io
from collections import deque
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest
from PIL import Image
from pydantic import ValidationError

from kimi_cli.soul.approval import ApprovalResult
from kimi_cli.tools.automation import MAX_RESULT_CHARS, json_text
from kimi_cli.tools.browser_use import BrowserUse, Params
from kimi_cli.tools.browser_use.backend import BrowserConfig, BrowserManager
from kimi_cli.tools.browser_use.policy import (
    BrowserPolicyError,
    url_for_output,
    validate_navigation_url,
)
from kimi_cli.wire.types import TextPart
from kosong.tooling import ToolError, ToolOk


def _approval() -> MagicMock:
    approval = MagicMock()
    approval.request = AsyncMock(return_value=ApprovalResult(True))
    return approval


def _tool(runtime: Any, session: Any, manager: Any) -> BrowserUse:
    # A mocked manager still carries the launch/origin policy the tool reads.
    if not isinstance(getattr(manager, "config", None), BrowserConfig):
        manager.config = BrowserConfig()
    return BrowserUse(runtime, _approval(), session, manager=manager)


def test_browser_schema_rejects_ambiguous_or_incomplete_actions() -> None:
    assert "snapshot or targeted extraction" in BrowserUse.description
    assert "not against the user's intended text" in BrowserUse.description
    assert (
        "Copy the requested text without adding whitespace"
        in Params.model_fields["text"].description
    )
    with pytest.raises(ValidationError, match="navigate requires url"):
        Params(action="navigate")
    with pytest.raises(ValidationError, match="exactly one"):
        Params(action="click", selector="#go", element=1)
    with pytest.raises(ValidationError, match="switch_tab requires"):
        Params(action="switch_tab")
    with pytest.raises(ValidationError, match="session must start"):
        Params(action="snapshot", session="../other")


async def test_navigation_policy_has_non_network_fixtures_and_hard_floors() -> None:
    assert await validate_navigation_url("about:blank") == "about:blank"
    data_url = "data:text/html,<title>Fixture</title><button>Go</button>"
    assert await validate_navigation_url(data_url) == data_url
    with pytest.raises(BrowserPolicyError, match="file/javascript"):
        await validate_navigation_url("file:///C:/Windows/win.ini")
    with pytest.raises(BrowserPolicyError) as caught:
        await validate_navigation_url("http://169.254.169.254/latest/meta-data")
    assert caught.value.code == "unsafe_url"
    with pytest.raises(BrowserPolicyError) as caught:
        await validate_navigation_url("https://example.com/?access_token=secret")
    assert caught.value.code == "secret_in_url"


def test_redirected_page_url_redacts_sensitive_query() -> None:
    assert (
        url_for_output("https://example.com/callback?access_token=secret&state=ok#fragment")
        == "https://example.com/callback?redacted=1"
    )
    assert url_for_output("https://example.com/search?q=browser") == (
        "https://example.com/search?q=browser"
    )
    assert url_for_output("https://user:password@example.com/private?q=1") == (
        "https://example.com/private"
    )


def test_data_url_output_is_compact_and_does_not_echo_markup() -> None:
    output = url_for_output(
        "data:text/html,<title>Fixture</title><input value='sensitive-form-value'>"
    )

    assert output.startswith("data:text/html,[")
    assert output.endswith(" characters omitted]")
    assert "sensitive-form-value" not in output
    assert len(output) < 60


def test_truncated_automation_output_remains_valid_json() -> None:
    result = json_text({"value": "x" * (MAX_RESULT_CHARS + 100)})
    payload = orjson.loads(result)
    assert payload["truncated"] is True
    assert payload["original_chars"] > MAX_RESULT_CHARS
    assert len(result) <= MAX_RESULT_CHARS


async def test_navigate_validates_once_then_uses_normalized_url(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    state = SimpleNamespace(last_used=0.0)
    manager.get_session = AsyncMock(return_value=state)
    manager.navigate = AsyncMock(return_value={"url": "about:blank", "title": ""})
    manager.invalidate_snapshot = AsyncMock()
    tool = _tool(runtime, session, manager)
    result = await tool(Params(action="navigate", url="about:blank"))
    assert isinstance(result, ToolOk)
    manager.navigate.assert_awaited_once_with(state, "about:blank", "domcontentloaded")
    tool._approval.request.assert_awaited_once()
    assert tool._approval.request.await_args.args[1] == "browser_use:navigate"


async def test_fill_forwards_literal_whitespace_without_trimming(
    runtime: Any, session: Any
) -> None:
    manager = MagicMock()
    state = SimpleNamespace(last_used=0.0)
    manager.get_session = AsyncMock(return_value=state)
    manager.fill = AsyncMock(return_value={"verified": True, "verdict": "done"})
    manager.invalidate_snapshot = AsyncMock()
    text = " Hello from browser_use "

    result = await _tool(runtime, session, manager)(
        Params(action="fill", selector="#message", text=text)
    )

    assert isinstance(result, ToolOk)
    manager.fill.assert_awaited_once_with(state, None, "#message", None, text)
    payload = orjson.loads(result.output)
    assert payload["verified"] is True
    assert "next_step" not in payload


async def test_unverified_fill_requires_fresh_state_before_repeating_input(
    runtime: Any, session: Any
) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.fill = AsyncMock(return_value={"verified": False, "verdict": "verify_fresh_state"})
    manager.invalidate_snapshot = AsyncMock()

    result = await _tool(runtime, session, manager)(
        Params(action="fill", selector="#message", text="expected")
    )

    assert isinstance(result, ToolOk)
    payload = orjson.loads(result.output)
    assert payload["verified"] is False
    assert payload["verdict"] == "verify_fresh_state"
    assert "snapshot or targeted extraction" in payload["next_step"]


async def test_verify_action_allows_targeted_extraction_as_fresh_state(
    runtime: Any, session: Any
) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.click = AsyncMock(return_value={})
    manager.invalidate_snapshot = AsyncMock()

    result = await _tool(runtime, session, manager)(Params(action="click", selector="#go"))

    assert isinstance(result, ToolOk)
    assert "snapshot or targeted extraction" in orjson.loads(result.output)["next_step"]


async def test_snapshot_is_read_only_and_does_not_request_approval(
    runtime: Any, session: Any
) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.snapshot = AsyncMock(return_value={"elements": [], "snapshot_id": "abc"})
    tool = _tool(runtime, session, manager)
    result = await tool(Params(action="snapshot"))
    assert isinstance(result, ToolOk)
    assert orjson.loads(result.output)["snapshot_id"] == "abc"
    tool._approval.request.assert_not_awaited()
    manager.invalidate_snapshot.assert_not_called()


async def test_close_does_not_start_a_browser(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.close_session = AsyncMock(return_value=True)
    tool = _tool(runtime, session, manager)
    result = await tool(Params(action="close", session="work"))
    assert isinstance(result, ToolOk)
    assert orjson.loads(result.output)["closed"] is True
    manager.get_session.assert_not_called()


async def test_screenshot_is_saved_and_attached(runtime: Any, session: Any) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (30, 20), "navy").save(buffer, format="PNG")
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))
    manager.screenshot = AsyncMock(return_value=({"title": "Fixture"}, buffer.getvalue()))
    result = await _tool(runtime, session, manager)(Params(action="screenshot"))
    assert isinstance(result, ToolOk)
    assert isinstance(result.output, list)
    text = next(part.text for part in result.output if isinstance(part, TextPart))
    payload = orjson.loads(text.splitlines()[0])
    assert Image.open(payload["screenshot_path"]).size == (30, 20)


async def test_timeout_is_unknown_and_action_is_not_replayed(runtime: Any, session: Any) -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=SimpleNamespace(last_used=0.0))

    async def slow_navigate(*args: Any) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {}

    manager.navigate = AsyncMock(side_effect=slow_navigate)
    manager.invalidate_snapshot = AsyncMock()
    result = await _tool(runtime, session, manager)(
        Params(action="navigate", url="about:blank", timeout=1)
    )
    assert isinstance(result, ToolError)
    assert "timeout_outcome_unknown" in result.message
    assert manager.navigate.await_count == 1
    manager.invalidate_snapshot.assert_awaited_once_with(manager.get_session.return_value)


def _browser_state(page: Any) -> Any:
    return SimpleNamespace(
        name="test",
        context=SimpleNamespace(pages=[page]),
        page=page,
        snapshot_id="",
        elements=[],
        borrowed_context=False,
        console=deque(maxlen=5),
        network_failures=deque(maxlen=5),
        wired=[],
    )


async def test_closed_active_tab_preserves_remaining_context() -> None:
    manager = BrowserManager(BrowserConfig())
    closed = MagicMock()
    closed.is_closed.return_value = True
    remaining = MagicMock()
    remaining.is_closed.return_value = False
    context = MagicMock()
    context.pages = [remaining]
    state = _browser_state(closed)
    state.context = context
    state.snapshot_id = "old"
    state.last_used = 0.0
    manager.sessions["test"] = state
    manager.close_idle_sessions = AsyncMock()
    browser = MagicMock()
    manager._ensure_browser = AsyncMock(return_value=browser)

    result = await manager.get_session("test")

    assert result is state
    assert state.page is remaining
    remaining.set_default_timeout.assert_called_once_with(manager.config.timeout_ms)
    context.close.assert_not_called()
    browser.new_context.assert_not_called()
    assert state.snapshot_id == ""


async def test_close_background_tab_preserves_active_page() -> None:
    active = MagicMock()
    active.is_closed.return_value = False
    active.title = AsyncMock(return_value="Active")
    active.url = "about:blank#active"
    background = MagicMock()
    background.is_closed.return_value = False
    context = MagicMock()
    context.pages = [active, background]

    async def close_background() -> None:
        background.is_closed.return_value = True
        context.pages = [active]

    background.close = AsyncMock(side_effect=close_background)
    state = _browser_state(active)
    state.context = context
    manager = BrowserManager(BrowserConfig())

    result = await manager.close_tab(state, 2)

    assert state.page is active
    assert result["title"] == "Active"
    active.set_default_timeout.assert_not_called()
    context.new_page.assert_not_called()


async def test_close_active_tab_selects_last_remaining_page() -> None:
    active = MagicMock()
    active.is_closed.return_value = False
    remaining = MagicMock()
    remaining.is_closed.return_value = False
    remaining.title = AsyncMock(return_value="Remaining")
    remaining.url = "about:blank#remaining"
    context = SimpleNamespace(pages=[active, remaining])

    async def close_active() -> None:
        active.is_closed.return_value = True
        context.pages = [remaining]

    active.close = AsyncMock(side_effect=close_active)
    state = _browser_state(active)
    state.context = context
    manager = BrowserManager(BrowserConfig())

    result = await manager.close_tab(state, 1)

    assert state.page is remaining
    assert result["title"] == "Remaining"
    remaining.set_default_timeout.assert_called_once_with(manager.config.timeout_ms)


async def test_snapshot_batches_dom_metadata_and_handle_discovery() -> None:
    page = MagicMock()
    page.url = "about:blank"
    page.title = AsyncMock(return_value="Fixture")
    root = MagicMock()
    nodes = MagicMock()
    items = MagicMock()
    total = MagicMock()
    first = MagicMock()
    second = MagicMock()
    length = MagicMock()
    length.dispose = AsyncMock()
    for handle in (root, nodes, items, total):
        handle.dispose = AsyncMock()
    root.get_properties = AsyncMock(return_value={"nodes": nodes, "items": items, "total": total})
    nodes.get_properties = AsyncMock(return_value={"0": first, "1": second, "length": length})
    items.json_value = AsyncMock(
        return_value=[
            {"tag": "button", "role": "button", "label": "One", "bounds": [1, 2, 3, 4]},
            {"tag": "input", "role": "text", "label": "Two", "bounds": [5, 6, 7, 8]},
        ]
    )
    total.json_value = AsyncMock(return_value=3)
    page.evaluate_handle = AsyncMock(return_value=root)
    state = _browser_state(page)
    manager = BrowserManager(BrowserConfig(max_elements=2))

    result = await manager.snapshot(state)

    assert result["element_count"] == 2
    assert result["elements"][1] == {
        "element": 2,
        "tag": "input",
        "role": "text",
        "label": "Two",
        "bounds": [5, 6, 7, 8],
    }
    assert result["truncated"] is True
    assert state.elements == [first, second]
    page.evaluate_handle.assert_awaited_once()
    page.locator.assert_not_called()
    for handle in (root, nodes, items, total, length):
        handle.dispose.assert_awaited_once()


async def test_extract_batches_matches_in_one_locator_evaluation() -> None:
    page = MagicMock()
    page.url = "about:blank"
    page.title = AsyncMock(return_value="Fixture")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(return_value={"values": ["one", "two"], "truncated": True})
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    result = await manager.extract(_browser_state(page), ".record", None, True, 2)

    assert result["values"] == ["one", "two"]
    assert result["truncated"] is True
    locator.evaluate_all.assert_awaited_once()
    locator.count.assert_not_called()
    locator.nth.assert_not_called()


async def test_extract_text_attribute_is_an_inner_text_alias() -> None:
    page = MagicMock()
    page.url = "about:blank"
    page.title = AsyncMock(return_value="Fixture")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(return_value={"values": ["Hello"], "truncated": False})
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    result = await manager.extract(_browser_state(page), "#result", "text", False, 20)

    assert result["values"] == ["Hello"]
    assert locator.evaluate_all.await_args.args[1]["attribute"] is None


async def test_clear_snapshot_invalidates_before_disposing_handles() -> None:
    page = MagicMock()
    state = _browser_state(page)
    started = asyncio.Event()

    async def dispose() -> None:
        started.set()
        await asyncio.sleep(0)

    handle = MagicMock()
    handle.dispose = AsyncMock(side_effect=dispose)
    state.elements = [handle]
    state.snapshot_id = "old"
    manager = BrowserManager(BrowserConfig())

    task = asyncio.create_task(manager._clear_snapshot(state))
    await started.wait()
    assert state.elements == []
    assert state.snapshot_id == ""
    await task
    handle.dispose.assert_awaited_once()
