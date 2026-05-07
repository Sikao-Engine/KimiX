import asyncio
import re
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
                pass
            await page.wait_for_timeout(2000)
            html = await page.content()
        finally:
            await browser.close()
    return html


async def _fetch_html_http(url: str, user_agent: str) -> str:
    """Fallback HTTP fetch when Playwright browser is unavailable."""
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None, lambda: urllib.request.urlopen(req, timeout=30)
        )
    except urllib.error.URLError as exc:
        raise RuntimeError(f"HTTP fallback failed for {url}: {exc}") from exc
    charset = response.headers.get_content_charset("utf-8")
    data = await loop.run_in_executor(None, response.read)
    return data.decode(charset, errors="replace")


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
