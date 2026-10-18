"""Tests for the DeepSeek web search/fetch service support.

Covers:
- ``type`` field on ``SearchConfig`` / ``FetchConfig`` (default ``kimi``,
  explicit ``deepseek``) and the ``service_type`` hostname-inference helper.
- ``deepseek_endpoint_url`` normalization of the ``/v1/tools/<resource>`` suffix.
- ``DeepseekServiceProvider.search`` field mapping into the Hermes shape plus
  request payload / URL / header wiring.
- Search error paths: non-200, timeout, network error.
- ``fetch_url`` DeepSeek flavour (JSON ``{"title", "content"}``) and a Kimi
  regression test asserting the old raw-text body path still works.
- ``SearchWeb.__init__`` provider registration by service flavour.

All tests are offline; HTTP goes through the same fake ``new_client_session``
harness convention as ``test_web_providers.py`` / ``test_web_providers_extra.py``.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from pydantic import SecretStr

from kimi_cli.config import Config, FetchConfig, SearchConfig
from kimi_cli.tools.web.fetch import Params as FetchParams
from kimi_cli.tools.web.fetch import fetch_url
from kimi_cli.tools.web.providers import (
    DeepseekServiceProvider,
    KimiServiceProvider,
    _reset_for_tests,
    deepseek_endpoint_url,
    get_provider,
    list_providers,
    register_provider,
    service_type,
)
from kimi_cli.tools.web.search import SearchWeb
from tests.conftest import tool_call_context

# ---------------------------------------------------------------------------
# Fake aiohttp primitives (same convention as the other web provider tests)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Any = None, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload

    async def text(self) -> str:
        return self._text


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeSession:
    """Route ``(method, url)`` pairs to canned responses; record all calls."""

    def __init__(
        self,
        routes: dict[tuple[str, str], _FakeResponse] | None = None,
        *,
        fail: str | None = None,
    ) -> None:
        self._routes = routes or {}
        self._fail = fail
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeSession:
        if self._fail == "timeout":
            raise TimeoutError("timed out")
        if self._fail == "network":
            raise aiohttp.ClientError("connection refused")
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def post(self, url: str, **kwargs: Any) -> _FakeRequestContext:
        self.calls.append(("POST", url, kwargs))
        return _FakeRequestContext(self._routes[("POST", url)])


def _patch_providers_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(
        "kimi_cli.tools.web.providers.new_client_session", lambda **kw: session
    )


def _patch_utils_session(monkeypatch: pytest.MonkeyPatch, session: _FakeSession) -> None:
    monkeypatch.setattr(
        "kimi_cli.utils.aiohttp.new_client_session", lambda **kw: session
    )


@pytest.fixture(autouse=True)
def _safe_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the SSRF check so fetch tests reach the fake session."""

    async def _safe(url: str) -> bool:
        return True

    monkeypatch.setattr("kimi_cli.tools.web.url_safety.async_is_safe_url", _safe)


@pytest.fixture(autouse=True)
def _registry_isolation() -> Any:
    """Save/clear the built-in registry around each test."""
    before = list(list_providers())
    _reset_for_tests()
    yield
    _reset_for_tests()
    for provider in before:
        register_provider(provider)


# ---------------------------------------------------------------------------
# Config + service_type
# ---------------------------------------------------------------------------


class TestConfigType:
    def test_search_config_type_defaults_to_kimi(self) -> None:
        cfg = SearchConfig(base_url="https://api.kimi.com/v1/search", api_key=SecretStr("k"))
        assert cfg.type == "kimi"

    def test_fetch_config_type_defaults_to_kimi(self) -> None:
        cfg = FetchConfig(base_url="https://api.kimi.com/v1/fetch", api_key=SecretStr("k"))
        assert cfg.type == "kimi"

    def test_explicit_deepseek_type(self) -> None:
        search = SearchConfig(
            base_url="https://api.deepseek.com", api_key=SecretStr("k"), type="deepseek"
        )
        fetch = FetchConfig(
            base_url="https://api.deepseek.com", api_key=SecretStr("k"), type="deepseek"
        )
        assert search.type == "deepseek"
        assert fetch.type == "deepseek"
        assert service_type(search) == "deepseek"
        assert service_type(fetch) == "deepseek"


class TestServiceType:
    def test_hostname_inference_deepseek(self) -> None:
        # ``type`` left unset (defaults to "kimi") but the hostname says deepseek.
        cfg = SearchConfig(base_url="https://api.deepseek.com", api_key=SecretStr("k"))
        assert service_type(cfg) == "deepseek"

    def test_hostname_inference_case_insensitive_with_path(self) -> None:
        cfg = SearchConfig(base_url="https://API.DEEPSEEK.com/v1", api_key=SecretStr("k"))
        assert service_type(cfg) == "deepseek"

    def test_hostname_inference_fetch(self) -> None:
        cfg = FetchConfig(base_url="https://api.deepseek.com", api_key=SecretStr("k"))
        assert service_type(cfg) == "deepseek"

    def test_kimi_host_is_kimi(self) -> None:
        cfg = SearchConfig(base_url="https://api.moonshot.cn/v1/search", api_key=SecretStr("k"))
        assert service_type(cfg) == "kimi"

    def test_explicit_kimi_wins_over_deepseek_host(self) -> None:
        cfg = SearchConfig(
            base_url="https://api.deepseek.com", api_key=SecretStr("k"), type="kimi"
        )
        assert service_type(cfg) == "kimi"

    def test_malformed_url_falls_back_to_kimi(self) -> None:
        cfg = SearchConfig(base_url="::not a url::", api_key=SecretStr("k"))
        assert service_type(cfg) == "kimi"

    def test_non_pydantic_object(self) -> None:
        class _Obj:
            base_url = "https://api.deepseek.com"
            type = None

        assert service_type(_Obj()) == "deepseek"


class TestDeepseekEndpointUrl:
    @pytest.mark.parametrize(
        "base",
        [
            "https://api.deepseek.com",
            "https://api.deepseek.com/",
            "https://api.deepseek.com/v1",
            "https://api.deepseek.com/v1/",
            "https://api.deepseek.com/v1/tools/search",
        ],
    )
    def test_search_normalization(self, base: str) -> None:
        assert (
            deepseek_endpoint_url(base, "search")
            == "https://api.deepseek.com/v1/tools/search"
        )

    def test_fetch_normalization(self) -> None:
        assert (
            deepseek_endpoint_url("https://api.deepseek.com", "fetch")
            == "https://api.deepseek.com/v1/tools/fetch"
        )

    def test_fetch_idempotent_when_full(self) -> None:
        assert (
            deepseek_endpoint_url("https://api.deepseek.com/v1/tools/fetch", "fetch")
            == "https://api.deepseek.com/v1/tools/fetch"
        )


# ---------------------------------------------------------------------------
# DeepseekServiceProvider
# ---------------------------------------------------------------------------


def _deepseek_config() -> Config:
    config = Config()
    config.services.search = SearchConfig(
        base_url="https://api.deepseek.com", api_key=SecretStr("sk-test")
    )
    return config


class TestDeepseekServiceProvider:
    def test_name_and_capabilities(self) -> None:
        provider = DeepseekServiceProvider(_deepseek_config(), runtime=None)  # type: ignore[arg-type]
        assert provider.name == "deepseek"
        assert provider.display_name == "DeepSeek Search Service"
        assert provider.supports_search() is True
        assert provider.supports_extract() is False

    def test_is_available(self) -> None:
        provider = DeepseekServiceProvider(_deepseek_config(), runtime=None)  # type: ignore[arg-type]
        assert provider.is_available() is True

        empty = Config()
        assert DeepseekServiceProvider(empty, runtime=None).is_available() is False  # type: ignore[arg-type]

    @staticmethod
    def _payload() -> dict[str, Any]:
        return {
            "results": [
                {
                    "rerank_score": 2.4,
                    "title": "First Title",
                    "url": "https://a.example/",
                    "snippet": "First snippet",
                    "summary": "First summary",
                    "content": "First body",
                    "site_name": "Site A",
                    "site_icon": "https://a.example/icon.png",
                    "date": "2026-06-01T00:00:00+00:00",
                    "authority": 3,
                },
                {
                    # null optional fields + summary missing -> description falls
                    # back to snippet.
                    "rerank_score": 0.5,
                    "title": "Second Title",
                    "url": "https://b.example/",
                    "snippet": "Second snippet",
                    "content": "",
                    "site_name": None,
                    "site_icon": None,
                    "date": None,
                    "authority": -1,
                },
            ]
        }

    async def test_search_success_maps_fields(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _deepseek_config()
        provider = DeepseekServiceProvider(config, runtime)
        session = _FakeSession(
            {("POST", "https://api.deepseek.com/v1/tools/search"): _FakeResponse(200, self._payload())}
        )
        _patch_providers_session(monkeypatch, session)

        result = await provider.search("DeepSeek V4", 5)

        assert result["success"] is True
        web = result["data"]["web"]
        assert len(web) == 2

        first = web[0]
        assert first["title"] == "First Title"
        assert first["url"] == "https://a.example/"
        assert first["description"] == "First summary"  # summary preferred
        assert first["snippet"] == "First snippet"
        assert first["content"] == "First body"
        assert first["date"] == "2026-06-01T00:00:00+00:00"
        assert first["site_name"] == "Site A"
        assert first["icon"] == "https://a.example/icon.png"
        assert first["mime"] == "text/markdown"
        assert first["position"] == 1
        assert first["rerank_score"] == 2.4
        assert first["authority"] == 3

        second = web[1]
        assert second["description"] == "Second snippet"  # summary missing -> snippet
        assert second["date"] == ""  # null -> ""
        assert second["site_name"] == ""  # null -> ""
        assert second["icon"] == ""  # null site_icon -> ""
        assert second["position"] == 2
        assert second["rerank_score"] == 0.5
        assert second["authority"] == -1

    async def test_search_sends_count_and_need_content(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _deepseek_config()
        provider = DeepseekServiceProvider(config, runtime)
        session = _FakeSession(
            {("POST", "https://api.deepseek.com/v1/tools/search"): _FakeResponse(200, {"results": []})}
        )
        _patch_providers_session(monkeypatch, session)

        with tool_call_context("SearchWeb"):
            result = await provider.search("hello", 3, include_content=True)

        assert result["success"] is True
        assert len(session.calls) == 1
        _m, url, kwargs = session.calls[0]
        assert url == "https://api.deepseek.com/v1/tools/search"
        body = kwargs["json"]
        assert body["query"] == "hello"
        assert body["count"] == 3
        assert body["need_content"] is True
        headers = kwargs["headers"]
        assert headers["Authorization"] == "Bearer sk-test"
        # In a tool-call context the header is attached (optional, not required).
        assert headers.get("X-Msh-Tool-Call-Id") == "test"

    async def test_search_need_content_false_and_no_tool_call(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _deepseek_config()
        provider = DeepseekServiceProvider(config, runtime)
        session = _FakeSession(
            {("POST", "https://api.deepseek.com/v1/tools/search"): _FakeResponse(200, {"results": []})}
        )
        _patch_providers_session(monkeypatch, session)

        # No tool-call context: still works, X-Msh-Tool-Call-Id absent.
        result = await provider.search("hello", 5)

        assert result["success"] is True
        _m, _u, kwargs = session.calls[0]
        assert kwargs["json"]["need_content"] is False
        assert "X-Msh-Tool-Call-Id" not in kwargs["headers"]

    async def test_search_non_200_includes_status_and_excerpt(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _deepseek_config()
        provider = DeepseekServiceProvider(config, runtime)
        session = _FakeSession(
            {
                (
                    "POST",
                    "https://api.deepseek.com/v1/tools/search",
                ): _FakeResponse(429, text="rate limited hard")
            }
        )
        _patch_providers_session(monkeypatch, session)

        result = await provider.search("hello")

        assert result["success"] is False
        assert "Status: 429" in result["error"]
        assert "rate limited hard" in result["error"]

    async def test_search_timeout(self, runtime: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        config = _deepseek_config()
        provider = DeepseekServiceProvider(config, runtime)
        session = _FakeSession(fail="timeout")
        _patch_providers_session(monkeypatch, session)

        result = await provider.search("hello")

        assert result["success"] is False
        assert "timed out" in result["error"]

    async def test_search_network_error(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _deepseek_config()
        provider = DeepseekServiceProvider(config, runtime)
        session = _FakeSession(fail="network")
        _patch_providers_session(monkeypatch, session)

        result = await provider.search("hello")

        assert result["success"] is False
        assert "connection refused" in result["error"]

    async def test_search_not_configured(self) -> None:
        provider = DeepseekServiceProvider(Config(), runtime=None)  # type: ignore[arg-type]
        result = await provider.search("hello")
        assert result["success"] is False
        assert "not configured" in result["error"]


# ---------------------------------------------------------------------------
# fetch_url
# ---------------------------------------------------------------------------


def _make_fetch_config(base_url: str, **kwargs: Any) -> FetchConfig:
    return FetchConfig(base_url=base_url, api_key=SecretStr("sk-test"), **kwargs)


class TestFetchDeepseek:
    async def test_fetch_deepseek_parses_json(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime.config.services.fetch = _make_fetch_config("https://api.deepseek.com")
        tool = fetch_url(config=runtime.config, runtime=runtime)
        session = _FakeSession(
            {
                (
                    "POST",
                    "https://api.deepseek.com/v1/tools/fetch",
                ): _FakeResponse(200, payload={"title": "Doc Title", "content": "# Body\ncontent here"})
            }
        )
        _patch_utils_session(monkeypatch, session)

        result = await tool._fetch_with_service(FetchParams(url="https://example.com/page"))

        assert not result.is_error
        assert result.output == "# Doc Title\n\n# Body\ncontent here"
        assert result.message == "The returned content is the main content extracted from the page."
        _m, url, kwargs = session.calls[0]
        assert url == "https://api.deepseek.com/v1/tools/fetch"
        assert kwargs["json"] == {"url": "https://example.com/page"}
        assert kwargs["headers"]["Authorization"] == "Bearer sk-test"

    async def test_fetch_deepseek_title_optional(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime.config.services.fetch = _make_fetch_config("https://api.deepseek.com/v1")
        tool = fetch_url(config=runtime.config, runtime=runtime)
        session = _FakeSession(
            {
                (
                    "POST",
                    "https://api.deepseek.com/v1/tools/fetch",
                ): _FakeResponse(200, payload={"title": "", "content": "only body"})
            }
        )
        _patch_utils_session(monkeypatch, session)

        result = await tool._fetch_with_service(FetchParams(url="https://example.com"))

        assert not result.is_error
        assert result.output == "only body"

    async def test_fetch_deepseek_non_200(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime.config.services.fetch = _make_fetch_config("https://api.deepseek.com")
        tool = fetch_url(config=runtime.config, runtime=runtime)
        session = _FakeSession(
            {
                (
                    "POST",
                    "https://api.deepseek.com/v1/tools/fetch",
                ): _FakeResponse(503, text="service down")
            }
        )
        _patch_utils_session(monkeypatch, session)

        result = await tool._fetch_with_service(FetchParams(url="https://example.com"))

        assert result.is_error
        assert "Status: 503" in result.message

    async def test_fetch_deepseek_timeout(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime.config.services.fetch = _make_fetch_config("https://api.deepseek.com")
        tool = fetch_url(config=runtime.config, runtime=runtime)
        _patch_utils_session(monkeypatch, _FakeSession(fail="timeout"))

        result = await tool._fetch_with_service(FetchParams(url="https://example.com"))

        assert result.is_error
        assert "timed out" in result.message


class TestFetchKimiRegression:
    async def test_fetch_kimi_raw_text_body_unchanged(
        self, runtime: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kimi flavour still POSTs to base_url and uses the raw text body."""
        runtime.config.services.fetch = _make_fetch_config(
            "https://api.kimi.com/coding/v1/fetch"
        )
        tool = fetch_url(config=runtime.config, runtime=runtime)
        raw = "# Raw Service Content\n\nfrom the kimi fetch endpoint"
        session = _FakeSession(
            {("POST", "https://api.kimi.com/coding/v1/fetch"): _FakeResponse(200, text=raw)}
        )
        _patch_utils_session(monkeypatch, session)

        with tool_call_context("fetch_url"):
            result = await tool._fetch_with_service(FetchParams(url="https://example.com"))

        assert not result.is_error
        assert result.output == raw  # no markdown heading prepended
        assert result.message == "The returned content is the main content extracted from the page."


# ---------------------------------------------------------------------------
# SearchWeb registration
# ---------------------------------------------------------------------------


class TestSearchWebRegistration:
    def test_deepseek_config_registers_deepseek_provider(
        self, runtime: Any
    ) -> None:
        runtime.config.services.search = SearchConfig(
            base_url="https://api.deepseek.com", api_key=SecretStr("sk")
        )
        tool = SearchWeb(runtime.config, runtime)
        assert tool is not None
        assert isinstance(get_provider("deepseek"), DeepseekServiceProvider)
        assert get_provider("kimi") is None

    def test_deepseek_config_registers_explicit_type(
        self, runtime: Any
    ) -> None:
        runtime.config.services.search = SearchConfig(
            base_url="https://api.deepseek.com", api_key=SecretStr("sk"), type="deepseek"
        )
        SearchWeb(runtime.config, runtime)
        assert isinstance(get_provider("deepseek"), DeepseekServiceProvider)

    def test_kimi_config_registers_kimi_provider(self, config: Config, runtime: Any) -> None:
        # The default ``config`` fixture uses a Kimi search base_url.
        SearchWeb(config, runtime)
        assert isinstance(get_provider("kimi"), KimiServiceProvider)
        assert get_provider("deepseek") is None

    def test_legacy_preference_deepseek_before_kimi(self) -> None:
        from kimi_cli.tools.web.providers import _SEARCH_LEGACY_PREFERENCE

        assert _SEARCH_LEGACY_PREFERENCE.index("deepseek") < _SEARCH_LEGACY_PREFERENCE.index(
            "kimi"
        )
