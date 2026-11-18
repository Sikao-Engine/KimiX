"""Receipt-fidelity tests for ``browser_use``.

A corpus run against the live web showed receipts that could not be used as
evidence: ``press`` reported an empty ``title`` for a page that renders one,
``fill`` claimed ``verified: true`` without echoing the value it verified,
tab actions never said which tab was active, and a read-only ``evaluate``
asked for a fresh snapshot "before repeating input" it had not performed.
These tests pin the corrected behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import orjson
import pytest

from kimi_cli.soul.approval import ApprovalResult
from kimi_cli.tools.browser_use import BrowserUse, Params
from kimi_cli.tools.browser_use import backend as backend_module
from kimi_cli.tools.browser_use.backend import (
    BrowserConfig,
    BrowserManager,
    BrowserSession,
)


def _page(url: str = "about:blank", title: Any = "Fixture") -> Any:
    page = MagicMock()
    page.url = url
    page.title = AsyncMock(return_value=title) if not isinstance(title, AsyncMock) else title
    page.is_closed.return_value = False
    page.wait_for_load_state = AsyncMock()
    return page


def _with_target(page: Any, target: Any) -> Any:
    """Point a selector at *target* (and let the fast existence check pass)."""
    page.locator = MagicMock(
        return_value=SimpleNamespace(first=target, count=AsyncMock(return_value=1))
    )
    return page


def _state(*pages: Any, page: Any | None = None) -> BrowserSession:
    return BrowserSession(
        name="test",
        context=SimpleNamespace(pages=list(pages)),
        page=page if page is not None else pages[0],
    )


def _state_page(page: Any) -> BrowserSession:
    """A session whose context lists *page* (which may be a plain stub, not a Mock)."""
    return BrowserSession(name="test", context=SimpleNamespace(pages=[page]), page=page)


def _tool(manager: Any) -> BrowserUse:
    runtime = MagicMock()
    runtime.llm = None
    if not isinstance(getattr(manager, "config", None), BrowserConfig):
        manager.config = BrowserConfig()
    manager.invalidate_snapshot = AsyncMock()
    approval = MagicMock()
    approval.request = AsyncMock(return_value=ApprovalResult(True))
    return BrowserUse(runtime, approval, MagicMock(), manager=manager)


def _receipt(result: Any) -> dict[str, Any]:
    return orjson.loads(result.output)


# ---------------------------------------------------------------------------
# title: a receipt must describe the document its url names
# ---------------------------------------------------------------------------


async def test_receipt_title_settles_the_navigation_the_action_started() -> None:
    page = _page(url="https://en.wikipedia.org/wiki/Browser", title="")
    # The first read is the one that used to leak "": it happens before the new
    # document has parsed its <head>.
    page.title = AsyncMock(side_effect=["", "Browser - Wikipedia"])
    manager = BrowserManager(BrowserConfig())

    result = await manager.info(_state(page))

    assert result["title"] == "Browser - Wikipedia"
    assert result["url"] == "https://en.wikipedia.org/wiki/Browser"
    assert page.title.await_count >= 2  # the empty read was retried, not returned


async def test_receipt_title_does_not_wait_for_a_blank_page() -> None:
    page = _page(url="about:blank", title="")
    manager = BrowserManager(BrowserConfig())

    result = await manager.info(_state(page))

    assert result["title"] == ""
    assert page.title.await_count == 1  # nothing to wait for on a blank page


async def test_receipt_title_keeps_an_empty_title_when_the_page_never_has_one(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(backend_module, "_TITLE_SETTLE_TIMEOUT_MS", 60)  # do not sleep 2s
    page = _page(url="https://example.test/no-title", title="")
    manager = BrowserManager(BrowserConfig())

    result = await manager.info(_state(page))

    assert result["title"] == ""
    assert result["url"] == "https://example.test/no-title"


async def test_receipt_pair_is_never_taken_from_two_documents() -> None:
    """Live symptom: url still named the old page while the title came from the next one."""

    class _RacingPage:
        def __init__(self) -> None:
            self.urls = ["https://a.test/old", "https://a.test/new"]
            self.title = AsyncMock(return_value="Loading https://a.test/new")

        @property
        def url(self) -> str:
            return self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]

    manager = BrowserManager(BrowserConfig())

    receipt = await manager.info(_state_page(_RacingPage()))

    assert receipt["url"] == "https://a.test/new"
    assert receipt["title"] == "Loading https://a.test/new"


async def test_press_receipt_reports_the_title_of_the_page_it_navigated_to() -> None:
    page = _page(url="https://en.wikipedia.org/wiki/Browser", title="")
    page.title = AsyncMock(side_effect=["", "Browser - Wikipedia"])
    target = MagicMock()
    target.press = AsyncMock()
    _with_target(page, target)
    manager = BrowserManager(BrowserConfig())

    result = await manager.press(_state(page), None, "#search", None, "Enter")

    assert result["url"] == "https://en.wikipedia.org/wiki/Browser"
    assert result["title"] == "Browser - Wikipedia"


async def test_press_honors_the_requested_wait_until() -> None:
    page = _page(url="https://en.wikipedia.org/wiki/Browser", title="Browser - Wikipedia")
    target = MagicMock()
    target.press = AsyncMock()
    _with_target(page, target)
    manager = BrowserManager(BrowserConfig())

    await manager.press(_state(page), None, "#search", None, "Enter", wait_until="load")

    page.wait_for_load_state.assert_awaited_once()
    assert page.wait_for_load_state.await_args.args[0] == "load"
    assert page.wait_for_load_state.await_args.kwargs["timeout"] <= 3_000


async def test_press_survives_a_navigation_wait_that_never_settles() -> None:
    """Live regression: an unbounded wait turned a good press into a tool timeout."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = _page(url="https://en.wikipedia.org/wiki/Browser", title="Browser - Wikipedia")
    page.wait_for_load_state = AsyncMock(side_effect=PlaywrightTimeoutError("no navigation"))
    target = MagicMock()
    target.press = AsyncMock()
    _with_target(page, target)
    manager = BrowserManager(BrowserConfig())

    receipt = await manager.press(
        _state(page), None, "#searchInput", None, "Enter", wait_until="domcontentloaded"
    )

    assert receipt["title"] == "Browser - Wikipedia"
    assert target.press.await_count == 1


async def test_press_without_wait_until_does_not_block_on_the_navigation() -> None:
    page = _page(url="https://en.wikipedia.org/wiki/Browser", title="Browser - Wikipedia")
    target = MagicMock()
    target.press = AsyncMock()
    _with_target(page, target)
    manager = BrowserManager(BrowserConfig())

    await manager.press(_state(page), None, "#search", None, "Enter")

    page.wait_for_load_state.assert_not_awaited()


async def test_coordinate_click_honors_the_requested_wait_until() -> None:
    page = _page(url="https://canvas.test/", title="Canvas")
    page.mouse.click = AsyncMock()
    page.keyboard.down = AsyncMock()
    page.keyboard.up = AsyncMock()
    manager = BrowserManager(BrowserConfig())

    await manager.click(
        _state(page), None, None, None, "left", 1, [], (10, 20), wait_until="networkidle"
    )

    assert page.wait_for_load_state.await_args.args[0] == "networkidle"


# ---------------------------------------------------------------------------
# tab: every receipt says which tab it describes
# ---------------------------------------------------------------------------


async def test_receipt_names_the_active_tab_and_the_total_count() -> None:
    first = _page(url="https://a.test/", title="A")
    second = _page(url="https://b.test/", title="B")
    manager = BrowserManager(BrowserConfig())

    assert manager.page_info(_state(first, second))["tab"] == 1
    assert manager.page_info(_state(first, second, page=second))["tab"] == 2
    assert manager.page_info(_state(first, second, page=second))["tab_count"] == 2


async def test_switch_tab_receipt_needs_no_extra_list_tabs_call() -> None:
    first = _page(url="https://a.test/", title="A")
    second = _page(url="https://b.test/", title="B")
    first.bring_to_front = AsyncMock()
    state = _state(first, second, page=second)
    manager = BrowserManager(BrowserConfig())

    result = await manager.switch_tab(state, 1)

    assert result["tab"] == 1
    assert result["title"] == "A"
    assert result["url"] == "https://a.test/"
    assert result["tab_count"] == 2


async def test_close_tab_receipt_names_the_tab_that_takes_over() -> None:
    first = _page(url="https://a.test/", title="A")
    second = _page(url="https://b.test/", title="B")
    context = SimpleNamespace(pages=[first, second])

    async def close_second() -> None:
        second.is_closed.return_value = True
        context.pages = [first]

    second.close = AsyncMock(side_effect=close_second)
    state = _state(first, second, page=second)
    state.context = context
    manager = BrowserManager(BrowserConfig())

    result = await manager.close_tab(state, 2)

    assert result["tab"] == 1
    assert result["tab_count"] == 1


# ---------------------------------------------------------------------------
# fill: `verified` must be auditable from the receipt alone
# ---------------------------------------------------------------------------


async def test_fill_receipt_echoes_the_value_the_browser_holds() -> None:
    page = _page(url="https://example.test/form", title="Form")
    target = MagicMock()
    target.fill = AsyncMock()
    target.input_value = AsyncMock(return_value="Grüße 🧭 browser_use")
    _with_target(page, target)
    manager = BrowserManager(BrowserConfig())

    result = await manager.fill(_state(page), None, "#q", None, "Grüße 🧭 browser_use")

    assert result["value"] == "Grüße 🧭 browser_use"
    assert result["verified"] is True
    assert result["verdict"] == "done"
    assert "next_step" not in result


async def test_fill_receipt_shows_the_mismatch_instead_of_only_a_flag() -> None:
    page = _page(url="https://example.test/form", title="Form")
    target = MagicMock()
    target.fill = AsyncMock()
    target.input_value = AsyncMock(return_value="trimmed")
    _with_target(page, target)
    manager = BrowserManager(BrowserConfig())

    result = await manager.fill(_state(page), None, "#q", None, " padded ")

    assert result["value"] == "trimmed"
    assert result["verified"] is False
    assert result["verdict"] == "verify_fresh_state"


async def test_fill_receipt_reports_an_unreadable_value_as_unknown() -> None:
    page = _page(url="https://example.test/form", title="Form")
    target = MagicMock()
    target.fill = AsyncMock()
    target.input_value = AsyncMock(side_effect=RuntimeError("not an input"))
    _with_target(page, target)
    manager = BrowserManager(BrowserConfig())

    result = await manager.fill(_state(page), None, "div[contenteditable]", None, "text")

    assert result["value"] is None
    assert result["verified"] is None
    assert result["verdict"] == "verify_fresh_state"


# ---------------------------------------------------------------------------
# verdicts: only input actions ask for a fresh read-back
# ---------------------------------------------------------------------------


async def test_read_only_evaluate_does_not_demand_a_fresh_snapshot() -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=_state(_page()))
    manager.evaluate = AsyncMock(return_value={"value": {"inputValue": "kept"}})
    tool = _tool(manager)

    receipt = _receipt(await tool(Params(action="evaluate", script="document.title")))

    assert receipt["value"] == {"inputValue": "kept"}
    assert "verdict" not in receipt
    assert "next_step" not in receipt


async def test_input_actions_still_ask_for_a_fresh_read_back() -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=_state(_page()))
    manager.press = AsyncMock(return_value={"url": "https://example.test/next", "title": "Next"})
    tool = _tool(manager)

    receipt = _receipt(await tool(Params(action="press", selector="#q", key="Enter")))

    assert receipt["verdict"] == "verify_fresh_state"
    assert "snapshot or targeted extraction" in receipt["next_step"]


async def test_press_forwards_wait_until_and_navigate_keeps_its_default() -> None:
    manager = MagicMock()
    manager.get_session = AsyncMock(return_value=_state(_page()))
    manager.press = AsyncMock(return_value={"title": "Next"})
    manager.navigate = AsyncMock(return_value={"title": "Next"})
    tool = _tool(manager)

    await tool(Params(action="press", selector="#q", key="Enter", wait_until="load"))
    await tool(Params(action="press", selector="#q", key="Enter"))
    # A data URL passes the SSRF policy without a network lookup.
    await tool(Params(action="navigate", url="data:text/html,<title>Fixture</title>"))

    assert manager.press.await_args_list[0].kwargs["wait_until"] == "load"
    assert manager.press.await_args_list[1].kwargs["wait_until"] is None
    # navigate keeps waiting even though the parameter is now opt-in.
    assert manager.navigate.await_args.args[2] == "domcontentloaded"


async def test_evaluate_receipt_carries_no_verdict() -> None:
    page = _page(url="https://a.test/", title="A")
    page.evaluate = AsyncMock(return_value=800)
    manager = BrowserManager(BrowserConfig())

    receipt = await manager.evaluate(_state(page), "window.scrollY", None)

    assert receipt["value"] == 800
    assert "verdict" not in receipt
    assert "next_step" not in receipt


async def test_scroll_receipt_reports_the_offsets_it_reached() -> None:
    """A live run had to spend an extra `evaluate` because scroll returned no position."""
    page = _page(url="https://a.test/", title="A")
    page.evaluate = AsyncMock(return_value={"x": 0, "y": 800})
    page.mouse.wheel = AsyncMock()
    page.mouse.move = AsyncMock()
    page.viewport_size = {"width": 1280, "height": 900}
    manager = BrowserManager(BrowserConfig())

    receipt = await manager.scroll(_state(page), "down", 800, None, None, None)

    assert receipt["scroll"] == {"window": {"x": 0, "y": 800}}
    page.mouse.wheel.assert_awaited_once_with(0, 800)


async def test_scroll_survives_an_unreadable_position() -> None:
    from playwright.async_api import Error as PlaywrightError

    page = _page(url="https://a.test/", title="A")
    page.evaluate = AsyncMock(side_effect=PlaywrightError("no context"))
    page.mouse.wheel = AsyncMock()
    page.mouse.move = AsyncMock()
    page.viewport_size = {"width": 1280, "height": 900}
    manager = BrowserManager(BrowserConfig())

    receipt = await manager.scroll(_state(page), "down", 400, None, None, None)

    assert receipt["scroll"] == {}
    assert receipt["title"] == "A"


async def test_wait_receipt_reports_how_long_it_waited() -> None:
    page = _page(url="https://a.test/", title="A")
    manager = BrowserManager(BrowserConfig())

    receipt = await manager.wait(_state(page), 0.02)

    assert receipt["waited"] == "elapsed"
    assert receipt["elapsed"] >= 0.02


async def test_extract_receipt_separates_no_match_from_a_capped_list() -> None:
    """`values: []` alone could not tell "nothing matched" from "matched, empty"."""
    page = _page(url="https://a.test/", title="A")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(
        return_value={"values": [], "truncated": False, "matches": 0}
    )
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    empty = await manager.extract(_state(page), "h1", None, True, 10)

    assert empty["values"] == []
    assert empty["matches"] == 0
    assert empty["truncated"] is False

    locator.evaluate_all = AsyncMock(
        return_value={"values": ["a", "b"], "truncated": True, "matches": 7}
    )
    capped = await manager.extract(_state(page), ".card", None, True, 2)

    assert capped["matches"] == 7
    assert capped["truncated"] is True


async def test_extract_fields_receipt_reports_the_match_count() -> None:
    page = _page(url="https://a.test/", title="A")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(
        return_value={
            "values": [{"title": "A"}],
            "truncated": False,
            "matches": 4,
        }
    )
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    result = await manager.extract(
        _state(page), ".card", None, True, 10, fields={"title": "h2"}
    )

    assert result["records"] == [{"title": "A"}]
    assert result["matches"] == 4


async def test_extract_fields_all_null_is_an_error_not_an_empty_success() -> None:
    """Live corpus item #1: the worker pointed `selector` at the leaf element it
    wanted to read, so every field resolved to null and the call looked like a
    successful extract with no data - one wasted call, then a corrected one."""
    from kimi_cli.tools.browser_use.backend import BrowserBackendError

    page = _page(url="http://127.0.0.1:1/", title="Site")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(
        return_value={
            "values": [{"name": None, "slots": None}],
            "truncated": False,
            "matches": 1,
        }
    )
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    with pytest.raises(BrowserBackendError) as excinfo:
        await manager.extract(
            _state(page),
            "[data-testid='review-name']",
            None,
            False,
            10,
            fields={"name": "@self", "slots": "[data-testid='review-slots']"},
        )

    assert excinfo.value.code == "fields_not_found"
    assert excinfo.value.retryable is True
    assert "record container" in str(excinfo.value)


async def test_extract_fields_keeps_a_partially_null_record() -> None:
    page = _page(url="https://a.test/", title="A")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(
        return_value={
            "values": [{"name": "Ash Glaze", "slots": None}],
            "truncated": False,
            "matches": 1,
        }
    )
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    result = await manager.extract(
        _state(page), ".card", None, False, 10, fields={"name": "h2", "slots": ".slots"}
    )

    assert result["records"] == [{"name": "Ash Glaze", "slots": None}]


async def test_extract_fields_without_a_selector_uses_the_whole_document() -> None:
    """Live corpus item #3: the worker asked for several labelled values in one
    ``extract`` call with ``fields`` but no ``selector`` and lost the call to the
    old "fields extraction requires selector" validation error."""
    page = _page(url="https://a.test/", title="A")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(
        return_value={
            "values": [{"total": "7", "count": "4 of 7 segments"}],
            "truncated": False,
            "matches": 1,
        }
    )
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    result = await manager.extract(
        _state(page),
        None,
        None,
        False,
        10,
        fields={"total": '[data-testid="stat-total"]', "count": '[data-testid="queue-count"]'},
    )

    page.locator.assert_called_once_with("html")
    assert result["records"] == [{"total": "7", "count": "4 of 7 segments"}]


async def test_a_missing_selector_fails_fast_instead_of_timing_out() -> None:
    """Live: `select` against a page without a <select> burned ~45 s and returned
    `timeout_outcome_unknown` for a statically absent element."""
    from kimi_cli.tools.browser_use.backend import BrowserBackendError

    page = _page(url="https://a.test/", title="A")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=0)
    locator.first = MagicMock()
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    with pytest.raises(BrowserBackendError) as caught:
        await manager.click(_state(page), None, "select[name=size]", None, "left", 1, [])

    assert caught.value.code == "element_not_found"
    assert caught.value.retryable is True
    assert "take" in str(caught.value).lower()  # tells the model what to do next
    locator.first.click.assert_not_called()


async def test_scroll_parks_the_cursor_away_from_fixed_headers() -> None:
    page = _page(url="https://a.test/", title="A")
    page.evaluate = AsyncMock(return_value={"x": 0, "y": 600})
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()
    page.viewport_size = {"width": 1200, "height": 800}
    manager = BrowserManager(BrowserConfig())

    await manager.scroll(_state(page), "down", 600, None, None, None)

    page.mouse.move.assert_awaited_once_with(600.0, 400.0)
    page.mouse.wheel.assert_awaited_once_with(0, 600)


async def test_extract_attribute_value_reads_the_live_form_value() -> None:
    page = _page(url="https://a.test/", title="A")
    locator = MagicMock()
    locator.evaluate_all = AsyncMock(
        return_value={"values": ["medium"], "truncated": False, "matches": 1}
    )
    page.locator = MagicMock(return_value=locator)
    manager = BrowserManager(BrowserConfig())

    await manager.extract(_state(page), "input[name=size]", "value", True, 1)

    script = locator.evaluate_all.await_args.args[0]
    assert "element.value" in script  # property, not the default attribute


# ---------------------------------------------------------------------------
# description: what the model is told about receipts
# ---------------------------------------------------------------------------


def test_description_documents_receipts_without_repeating_itself() -> None:
    description = BrowserUse.description

    assert description.count("not against the user's intended text") == 1
    assert "After text is literal" not in description
    assert "tab (1-based index of the active tab)" in description
    assert "truncated:true means the" in description
    assert "echoes that read-back value" in description
    assert "snapshot or targeted extraction" in description
    assert "wait_until to wait out the navigation they trigger" in description
    assert "scroll reports the offsets it reached" in description
    assert "`element_total`" in description and "`matches`" in description
    assert "element_not_found" in description


def test_evaluate_is_not_in_the_input_verify_set() -> None:
    from kimi_cli.tools.browser_use import _SNAPSHOT_INVALIDATING_ACTIONS, _VERIFY_ACTIONS

    assert "evaluate" not in _VERIFY_ACTIONS
    # Still treated as potentially mutating for snapshot invalidation.
    assert "evaluate" in _SNAPSHOT_INVALIDATING_ACTIONS


def test_snapshot_script_and_description_expose_stable_selectors_when_present() -> None:
    script = backend_module._SNAPSHOT_SCRIPT
    assert "data-testid" in script
    assert "details.data_testid" in script
    assert "details.id" in script
    assert "details.disabled" in script
    assert "data_testid" in BrowserUse.description
    assert "disabled" in BrowserUse.description
