import asyncio
import re
import ssl
from bs4 import BeautifulSoup
from markdownify import markdownify as md
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
_LOGIN_PATTERNS = re.compile(
    r"登录|密码登录|验证码登录|注册|Sign in|Log in|Login|Verification code|短信验证码",
    re.IGNORECASE,
)


async def _fetch_html(url: str, user_agent: str, viewport: dict, wait_until: str) -> str:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ],
        )
        html = ""
        try:
            context = await browser.new_context(
                user_agent=user_agent,
                viewport=viewport,
                locale="en-US",
            )
            page = await context.new_page()
            try:
                await page.goto(url, wait_until=wait_until, timeout=60000)
            except PWTimeoutError:
                # If networkidle times out, the DOM is usually ready enough.
                # But if the page never loaded at all, bail out so the caller
                # can try the HTTP fallback instead of returning empty HTML.
                if page.url == "about:blank" and not url.lower().startswith("about:"):
                    raise RuntimeError(
                        f"Browser navigation timed out without loading {url}"
                    )
                pass
            await page.wait_for_timeout(2000)
            html = await page.content()
        finally:
            try:
                await browser.close()
            except Exception:
                pass
    return html


async def _fetch_html_http(url: str, user_agent: str) -> str:
    """Fallback HTTP fetch when Playwright browser is unavailable."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    loop = asyncio.get_running_loop()

    def _urlopen(req):
        try:
            return urllib.request.urlopen(req, timeout=30)
        except Exception as exc:
            # Retry with relaxed SSL context on SSL-related failures (e.g. UNEXPECTED_EOF_WHILE_READING)
            is_ssl_err = (
                isinstance(exc, ssl.SSLError)
                or (hasattr(exc, "reason") and isinstance(exc.reason, ssl.SSLError))
                or "SSL" in str(exc)
            )
            if not is_ssl_err:
                raise
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return urllib.request.urlopen(req, context=ctx, timeout=30)

    response = await loop.run_in_executor(None, _urlopen, req)
    charset = response.headers.get_content_charset("utf-8")
    data = await loop.run_in_executor(None, response.read)
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _html_to_markdown(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag_name in [
        "script",
        "style",
        "noscript",
        "img",
        "video",
        "audio",
        "source",
        "track",
        "iframe",
        "embed",
        "object",
        "canvas",
        "svg",
        "picture",
        "figure",
        "nav",
        "aside",
        "footer",
        "header",
    ]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    main = soup.find("main") or soup.find(role="main")
    body = soup.find("body")
    if main and len(main.get_text(strip=True)) >= 500:
        target = main
    elif body:
        target = body
    else:
        target = soup

    markdown = md(str(target), heading_style="ATX")
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    return markdown.strip()


async def fetch_to_markdown(url: str, wait_until: str = "networkidle") -> str:
    """Fetch a URL using a headless browser, execute JS, and return extracted text as Markdown."""
    try:
        html = await _fetch_html(url, _DESKTOP_UA, {"width": 1920, "height": 1080}, wait_until)
    except Exception:
        html = await _fetch_html_http(url, _DESKTOP_UA)

    markdown = _html_to_markdown(html)

    # If the result looks like a login wall, retry with a mobile user agent.
    text_len = len(markdown.replace(" ", "").replace("\n", ""))
    if text_len < 300 or _LOGIN_PATTERNS.search(markdown):
        html_mobile = ""
        try:
            html_mobile = await _fetch_html(url, _MOBILE_UA, {"width": 390, "height": 844}, wait_until)
        except Exception:
            try:
                html_mobile = await _fetch_html_http(url, _MOBILE_UA)
            except Exception:
                pass
        if html_mobile:
            markdown_mobile = _html_to_markdown(html_mobile)
            mobile_text_len = len(markdown_mobile.replace(" ", "").replace("\n", ""))
            if mobile_text_len > text_len:
                markdown = markdown_mobile

    return markdown
