"""Navigation policy for the browser automation tool."""

from __future__ import annotations

from collections.abc import Sequence
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


_CDP_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_LOOPBACK_HOSTS = frozenset({"localhost", "localhost.", "::1", "0:0:0:0:0:0:0:1"})


def is_loopback_host(host: str) -> bool:
    """Return True only for names the local machine resolves to itself."""
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if not normalized:
        return False
    if normalized in _LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 is loopback in its entirety, not just 127.0.0.1.
    return normalized.startswith("127.")


def validate_cdp_url(url: str, *, allow_remote: bool = False) -> str:
    """Validate a CDP endpoint before connecting.

    A remote debugging port has **no authentication**: any client that can reach it
    can open pages, run script and read every cookie of every logged-in tab in that
    profile.  Loopback is therefore enforced unless the operator opted out, because
    a documentation warning cannot be enforced and this one can.
    """
    raw = url.strip()
    if not raw:
        raise BrowserPolicyError("cdp_url must not be empty.", "invalid_cdp_url")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname or ""
    except ValueError as exc:
        raise BrowserPolicyError(f"Invalid CDP endpoint: {exc}", "invalid_cdp_url") from exc
    if parsed.scheme.lower() not in _CDP_SCHEMES:
        raise BrowserPolicyError(
            "cdp_url must be an http(s):// or ws(s):// endpoint, for example "
            "http://127.0.0.1:9222.",
            "invalid_cdp_url",
        )
    if parsed.username is not None or parsed.password is not None:
        raise BrowserPolicyError(
            "CDP endpoints containing userinfo credentials are refused.", "secret_in_url"
        )
    if not host:
        raise BrowserPolicyError("cdp_url must include a host.", "invalid_cdp_url")
    if allow_remote or is_loopback_host(host):
        return raw
    raise BrowserPolicyError(
        f"CDP endpoint host {host!r} is not loopback. Remote debugging ports are "
        "unauthenticated: anyone who can reach the port can drive the browser and read every "
        "logged-in cookie in that profile. Publish the port on 127.0.0.1 and reach it through "
        "an SSH tunnel, or accept the risk explicitly with browser_use.allow_remote_cdp=true "
        "(environment: KIMI_BROWSER_ALLOW_REMOTE_CDP=1).",
        "cdp_remote_refused",
    )


_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def origin_of(url: str) -> str | None:
    """Return ``scheme://host[:port]`` with default ports elided, or None."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return None
    if not host:
        return None
    scheme = parsed.scheme.lower()
    port = parsed.port
    suffix = f":{port}" if port and port != _DEFAULT_PORTS.get(scheme) else ""
    return f"{scheme}://{host.lower()}{suffix}"


def origin_pattern_matches(pattern: str, url: str) -> bool:
    """Match one ``allowed_origins``/``blocked_origins`` entry against a URL.

    Accepted forms: ``*`` (everything), ``scheme://host[:port]`` (exact origin),
    ``*.example.com`` (apex plus subdomains) and ``example.com`` (exact host).
    """
    candidate = pattern.strip().lower()
    if not candidate:
        return False
    if candidate == "*":
        return True
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if "://" in candidate:
        # Normalise both sides so ":443" and a bare origin compare equal.
        return origin_of(url) == origin_of(candidate)
    if candidate.startswith("*."):
        base = candidate[2:]
        return host == base or host.endswith("." + base)
    return host == candidate


def enforce_origin_policy(
    url: str,
    *,
    allowed: Sequence[str] = (),
    blocked: Sequence[str] = (),
) -> None:
    """Apply the operator's origin lists to a top-level navigation target.

    These lists are a convenience filter, **not** a security boundary: they do not
    follow redirects, subresources or popups.  Real destination control comes from
    network egress policy and environment isolation.
    """
    destination = origin_of(url) or url
    for pattern in blocked:
        if origin_pattern_matches(pattern, url):
            raise BrowserPolicyError(
                f"{destination} is blocked by browser_use.blocked_origins ({pattern!r}).",
                "origin_blocked",
            )
    if not allowed:
        return
    if not any(origin_pattern_matches(pattern, url) for pattern in allowed):
        raise BrowserPolicyError(
            f"{destination} is not in browser_use.allowed_origins "
            f"({', '.join(allowed)}). Add the origin or clear the list.",
            "origin_blocked",
        )
