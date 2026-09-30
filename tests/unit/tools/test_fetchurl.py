"""Tests for the fetch_url tool's HTTP-status handling.

The HTTP layer is fully mocked (no real network).  The headless-browser
entry point (``_fetch_html``) is patched to raise so the tests exercise the
httpx fallback path deterministically.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from kimi_agent_sdk import ToolError, ToolOk

from kimix.tools.web.fetch_url import Params, fetch_url
from kimix.tools.web.web_fetcher import fetcher

_URL = "https://example.com/page"


def _client_mock(status_code: int, text: str = "") -> MagicMock:
    """Build a mock httpx.AsyncClient returning a fixed-status response."""
    request = httpx.Request("GET", _URL)

    def _raise_for_status() -> None:
        if status_code >= 400:
            raise httpx.HTTPStatusError(
                f"Client error '{status_code}' for url '{_URL}'",
                request=request,
                response=httpx.Response(status_code, request=request),
            )

    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.raise_for_status = _raise_for_status

    client = MagicMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


@pytest.fixture
def fast_sleep() -> None:
    """Skip the fetcher's exponential-backoff sleeps (5xx retry path)."""
    with patch("asyncio.sleep", new=AsyncMock()):
        yield


@pytest.fixture
def no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the fetch down the httpx fallback path (no playwright)."""

    async def _fail(*args: object, **kwargs: object) -> str:
        raise RuntimeError("browser unavailable in tests")

    monkeypatch.setattr(fetcher, "_fetch_html", _fail)


class TestFetchHtmlHttpStatus:
    """Unit tests for the httpx fallback status handling."""

    async def test_404_raises_http_status_error(self, no_browser: None) -> None:
        with patch.object(
            fetcher.httpx, "AsyncClient", return_value=_client_mock(404, "")
        ):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await fetcher._fetch_html_http(_URL, "UA")
        assert "404" in str(exc_info.value)

    async def test_500_raises_http_status_error(self, no_browser: None, fast_sleep: None) -> None:
        with patch.object(
            fetcher.httpx, "AsyncClient", return_value=_client_mock(500, "boom")
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await fetcher._fetch_html_http(_URL, "UA")

    async def test_200_returns_text(self, no_browser: None) -> None:
        with patch.object(
            fetcher.httpx,
            "AsyncClient",
            return_value=_client_mock(200, "<html>ok</html>"),
        ):
            html = await fetcher._fetch_html_http(_URL, "UA")
        assert html == "<html>ok</html>"


class TestFetchUrlToolStatus:
    """End-to-end (HTTP mocked) tests for the fetch_url tool."""

    async def test_404_surfaces_error_with_status(self, no_browser: None) -> None:
        with patch.object(
            fetcher.httpx, "AsyncClient", return_value=_client_mock(404, "")
        ):
            result = await fetch_url()(Params(url=_URL))
        assert isinstance(result, ToolError)
        assert "404" in result.message
        assert _URL in result.message

    async def test_500_surfaces_error_with_status(self, no_browser: None, fast_sleep: None) -> None:
        with patch.object(
            fetcher.httpx, "AsyncClient", return_value=_client_mock(500, "")
        ):
            result = await fetch_url()(Params(url=_URL))
        assert isinstance(result, ToolError)
        assert "500" in result.message

    async def test_200_happy_path_unchanged(self, no_browser: None) -> None:
        html = (
            "<html><body><p>"
            + "hello world, this is a perfectly fine page. " * 20
            + "</p></body></html>"
        )
        with patch.object(
            fetcher.httpx, "AsyncClient", return_value=_client_mock(200, html)
        ):
            result = await fetch_url()(Params(url=_URL))
        assert isinstance(result, ToolOk)
        assert "hello world" in result.output

    async def test_browser_http_status_raises_and_surfaces_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 4xx/5xx reported by the headless browser must also surface."""

        class _FakeResponse:
            status = 404

        async def _fake_fetch_html(
            url: str, user_agent: str, viewport: dict, wait_until: str
        ) -> str:
            raise RuntimeError(f"HTTP {_FakeResponse.status} when loading {url}")

        monkeypatch.setattr(fetcher, "_fetch_html", _fake_fetch_html)
        with patch.object(
            fetcher.httpx, "AsyncClient", return_value=_client_mock(404, "")
        ):
            result = await fetch_url()(Params(url=_URL))
        assert isinstance(result, ToolError)
        assert "404" in result.message
        assert _URL in result.message
