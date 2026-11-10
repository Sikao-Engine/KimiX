"""Navigation policy for the browser automation tool."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit

from kimi_cli.tools.web.url_safety import (
    async_is_safe_url,
    normalize_url_for_request,
    sensitive_query_param_name,
    url_contains_secret,
)


def url_for_output(url: str) -> str:
    """Remove credential-bearing query/fragment data before reporting a page URL."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() == "data":
            media_type = parsed.path.partition(",")[0] or "text/plain"
            return f"data:{media_type},[{len(url)} characters omitted]"
        if "@" in parsed.netloc:
            return urlunsplit(
                (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", "")
            )
        if sensitive_query_param_name(url) is not None or url_contains_secret(url):
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "redacted=1", ""))
    except ValueError:
        return "[invalid URL redacted]"
    return url


class BrowserPolicyError(ValueError):
    """A URL refused before the browser receives it."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


async def validate_navigation_url(url: str) -> str:
    """Validate and normalize a top-level navigation URL.

    HTTP(S) uses the shared SSRF policy, including DNS resolution and the
    always-blocked metadata floor.  ``KIMI_ALLOW_PRIVATE_URLS=true`` explicitly
    permits loopback/LAN development sites but never permits cloud metadata.
    ``data:text/html`` and ``about:blank`` support self-contained automation
    fixtures without granting local-file access.
    """
    raw = url.strip()
    if not raw or len(raw) > 1_000_000:
        raise BrowserPolicyError(
            "URL must be non-empty and at most 1,000,000 characters.", "invalid_url"
        )
    if any(ord(character) < 32 for character in raw):
        raise BrowserPolicyError("URL contains control characters.", "invalid_url")
    if raw == "about:blank":
        return raw
    if raw.lower().startswith("data:"):
        if not raw.lower().startswith(("data:text/html", "data:text/plain")):
            raise BrowserPolicyError(
                "Only data:text/html and data:text/plain URLs are supported.",
                "unsupported_url_scheme",
            )
        return raw
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise BrowserPolicyError(f"Invalid URL: {exc}", "invalid_url") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise BrowserPolicyError(
            "Only HTTP(S), about:blank, and data:text/html/plain navigation is allowed; "
            "file/javascript/browser-internal schemes are refused.",
            "unsupported_url_scheme",
        )
    if parsed.username is not None or parsed.password is not None:
        raise BrowserPolicyError(
            "URLs containing userinfo credentials are refused.", "secret_in_url"
        )
    parameter = sensitive_query_param_name(raw)
    if parameter is not None:
        raise BrowserPolicyError(
            f"URL contains sensitive query parameter {parameter!r}; "
            "use a secret-safe authentication path.",
            "secret_in_url",
        )
    if url_contains_secret(raw) or url_contains_secret(unquote(raw)):
        raise BrowserPolicyError(
            "URL appears to contain a credential or API token.", "secret_in_url"
        )
    normalized = normalize_url_for_request(raw)
    if not await async_is_safe_url(normalized):
        raise BrowserPolicyError(
            "URL was refused by the network safety policy. For an intentional local development "
            "site, set KIMI_ALLOW_PRIVATE_URLS=true; cloud metadata endpoints remain blocked.",
            "unsafe_url",
        )
    return normalized
