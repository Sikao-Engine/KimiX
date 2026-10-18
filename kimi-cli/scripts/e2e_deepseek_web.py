"""End-to-end smoke test for the DeepSeek web search + fetch integration.

Reads the real API credentials and service endpoints from ``D:/ds_flash.json``
(at runtime, never committed), constructs the DeepSeek service configs, then:

1. Calls :class:`DeepseekServiceProvider.search` for a live query and prints the
   mapped Hermes-shape results.
2. Calls the DeepSeek fetch path (``fetch_url._fetch_with_service`` with the
   ``deepseek`` flavour) for one result URL and prints the title + a content
   excerpt.

Run with the project venv::

    cd kimi-cli && uv run python scripts/e2e_deepseek_web.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from kimi_cli.config import Config, FetchConfig, SearchConfig, Services
from kimi_cli.tools.web import url_safety
from kimi_cli.tools.web.fetch import Params as FetchParams
from kimi_cli.tools.web.fetch import fetch_url
from kimi_cli.tools.web.providers import (
    DeepseekServiceProvider,
    deepseek_endpoint_url,
    service_type,
)

CONFIG_FILE = Path("D:/ds_flash.json")
QUERY = "DeepSeek V4 release date"
FALLBACK_FETCH_URL = "https://api-docs.deepseek.com/zh-cn/api/search/"


async def _bypass_ssrf(url: str) -> bool:
    """SSRF bypass for the e2e run ONLY.

    This sandbox's DNS proxy answers every hostname inside the
    ``198.18.0.0/15`` benchmarking range, which ``is_safe_url`` treats as an
    internal address. That guard sits in front of ``fetch_url`` but is unrelated
    to the DeepSeek integration under test, so we short-circuit it (same
    convention as ``tests/tools/test_fetch_url.py``). The DeepSeek API call,
    auth and response parsing still run against the live service.
    """
    return True


class _FakeOAuth:
    """Minimal OAuth surface required by the providers/tools."""

    def resolve_api_key(self, api_key: Any, oauth: Any) -> str:
        # Ignore the OAuth ref; always return the configured secret.
        return api_key.get_secret_value()

    def common_headers(self) -> dict[str, str]:
        return {}


class _FakeRuntime:
    def __init__(self) -> None:
        self.oauth = _FakeOAuth()


def _from_dict(model: Any, data: dict[str, Any]) -> Any:
    """Build a pydantic sub-model, dropping keys it does not declare."""
    fields = set(model.model_fields)
    return model(**{k: v for k, v in data.items() if k in fields})


def _load_configs() -> tuple[Config, SearchConfig, FetchConfig]:
    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    services = raw.get("services", {})
    search_cfg = _from_dict(SearchConfig, services.get("search", {}))
    fetch_cfg = _from_dict(FetchConfig, services.get("fetch", {}))
    config = Config(services=Services(search=search_cfg, fetch=fetch_cfg))
    return config, search_cfg, fetch_cfg


async def main() -> int:
    config, search_cfg, fetch_cfg = _load_configs()

    print(f"[e2e] search service_type  = {service_type(search_cfg)}")
    print(f"[e2e] search endpoint      = {deepseek_endpoint_url(search_cfg.base_url, 'search')}")
    print(f"[e2e] fetch  service_type  = {service_type(fetch_cfg)}")
    print(f"[e2e] fetch  endpoint      = {deepseek_endpoint_url(fetch_cfg.base_url, 'fetch')}")

    runtime = _FakeRuntime()
    # See ``_bypass_ssrf``: neutralize the sandbox-specific SSRF guard.
    url_safety.async_is_safe_url = _bypass_ssrf

    # --- 1. Search ----------------------------------------------------------
    provider = DeepseekServiceProvider(config, runtime)  # type: ignore[arg-type]
    print(f"\n[e2e] searching: {QUERY!r} (limit=3)")
    result = await provider.search(QUERY, limit=3, include_content=False)
    if not result.get("success"):
        print(f"[e2e] search FAILED: {result.get('error')}")
        return 1

    web = result["data"]["web"]
    print(f"[e2e] search returned {len(web)} result(s):")
    for item in web:
        print(
            f"  [{item['position']}] {item['title']}\n"
            f"      url       : {item['url']}\n"
            f"      date      : {item['date']!r}\n"
            f"      site      : {item['site_name']!r}\n"
            f"      rerank    : {item['rerank_score']}   authority: {item['authority']}\n"
            f"      summary   : {(item['description'] or '')[:160]}"
        )

    fetch_target = web[0]["url"] if web else FALLBACK_FETCH_URL

    # --- 2. Fetch -----------------------------------------------------------
    print(f"\n[e2e] fetching (deepseek flavour): {fetch_target}")
    tool = fetch_url(config=config, runtime=runtime)  # type: ignore[arg-type]
    fetched = await tool._fetch_with_service(FetchParams(url=fetch_target))
    if fetched.is_error:
        print(f"[e2e] fetch FAILED: {fetched.message}")
        return 1

    output = fetched.output if isinstance(fetched.output, str) else str(fetched.output)
    title_line = output.splitlines()[0] if output else ""
    print(f"[e2e] fetch ok. message = {fetched.message!r}")
    print(f"[e2e] first line (title heading): {title_line!r}")
    print(f"[e2e] content preview:\n{output[:400]}")
    print("\n[e2e] DONE: DeepSeek search + fetch integration works.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
