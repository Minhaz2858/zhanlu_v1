"""Browser toolset — Playwright-backed web automation for the agent.

P2 (2026-08-29). Uses the system Chromium (/usr/bin/chromium) via the
Playwright ASYNC API (the tool handlers run inside the agent's asyncio
loop, where Playwright's sync API is forbidden). Each call opens a fresh
headless context (no persistent browser state — sub-agents and turns
stay isolated).

Guardrails (P2-2):
  1. Scheme allowlist — only ``http`` / ``https``. ``file://``, ``ftp://``,
     ``javascript:``, ``data:`` etc. are rejected outright.
  2. Domain allowlist — ``BROWSER_ALLOWED_DOMAINS`` (comma-separated env
     setting). Empty = any http(s) domain. Matching is by suffix so
     ``example.com`` also allows ``sub.example.com``.
  3. No credential fields — typing into ``input[type=password]`` is refused.
  4. Result caps — page text snapshot capped at 20k chars; screenshots
     returned as base64 data URLs (no disk writes).

Tools: browser_navigate, browser_click, browser_type, browser_snapshot,
browser_screenshot.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from urllib.parse import urlparse

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_BROWSER_RESULT_CAP = 20_000
_ALLOWED_SCHEMES = {"http", "https"}
_CREDENTIAL_SELECTOR_RE = re.compile(
    r"input\[[^\]]*type\s*=\s*['\"]password['\"]|type\s*=\s*['\"]password['\"]",
    re.IGNORECASE,
)


def _allowed_domains() -> list[str]:
    """Parse BROWSER_ALLOWED_DOMAINS ('a.com,b.com' → ['a.com', 'b.com'])."""
    raw = os.environ.get("BROWSER_ALLOWED_DOMAINS", "").strip()
    if not raw:
        return []
    return [d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip()]


def _validate_url(url: str) -> str:
    """Return the normalized URL or raise ValueError with a clear reason."""
    if not url or not isinstance(url, str):
        raise ValueError("url is required")
    url = url.strip()
    if len(url) > 4096:
        raise ValueError("url too long")
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"scheme '{parsed.scheme}' not allowed (only http/https)"
        )
    if not parsed.netloc:
        raise ValueError("url has no host")
    allowed = _allowed_domains()
    if allowed:
        hostname = (parsed.netloc or "").lower().split(":")[0]
        if not any(hostname == d or hostname.endswith("." + d) for d in allowed):
            raise ValueError(
                f"domain '{hostname}' not in BROWSER_ALLOWED_DOMAINS "
                f"({', '.join(allowed)})"
            )
    return url


def _browser_available() -> bool:
    """Playwright installed + a chromium binary resolvable."""
    try:
        import playwright.async_api  # noqa: F401
        return True
    except Exception:
        return False


async def _open_page(url: str | None = None):
    """Start async Playwright + Chromium, optionally navigate. Returns (pw, browser, page)."""
    from playwright.async_api import async_playwright

    candidates = ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]
    exe = next((c for c in candidates if os.path.exists(c)), None)
    pw = await async_playwright().start()
    if exe:
        browser = await pw.chromium.launch(executable_path=exe, headless=True)
    else:
        browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    if url:
        await page.goto(url, timeout=30_000, wait_until="domcontentloaded")
    return pw, browser, page


async def _browser_navigate(
    args: dict, db=None, user_id: str | None = None, context: dict | None = None
) -> dict:
    """Open a URL and return title + URL + page text preview."""
    try:
        url = _validate_url(args.get("url", ""))
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    try:
        pw, browser, page = await _open_page(url)
        try:
            return {
                "success": True,
                "url": page.url,
                "title": await page.title(),
                "text_preview": (await page.inner_text("body"))[:_BROWSER_RESULT_CAP],
            }
        finally:
            await browser.close()
            await pw.stop()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"browser error: {exc}"}


async def _browser_click(
    args: dict, db=None, user_id: str | None = None, context: dict | None = None
) -> dict:
    """Click a CSS selector on the current page."""
    selector = (args.get("selector") or "").strip()
    if not selector:
        return {"success": False, "error": "selector is required"}
    try:
        url = _validate_url(args.get("url")) if args.get("url") else None
        pw, browser, page = await _open_page(url)
        try:
            await page.click(selector, timeout=10_000)
            return {
                "success": True,
                "url": page.url,
                "text_preview": (await page.inner_text("body"))[:_BROWSER_RESULT_CAP],
            }
        finally:
            await browser.close()
            await pw.stop()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"browser error: {exc}"}


async def _browser_type(
    args: dict, db=None, user_id: str | None = None, context: dict | None = None
) -> dict:
    """Type text into a CSS selector. Refuses password fields."""
    selector = (args.get("selector") or "").strip()
    text = args.get("text")
    if not selector or text is None:
        return {"success": False, "error": "selector and text are required"}
    if _CREDENTIAL_SELECTOR_RE.search(selector):
        return {
            "success": False,
            "error": "typing into credential/password fields is not allowed",
        }
    try:
        url = _validate_url(args.get("url")) if args.get("url") else None
        pw, browser, page = await _open_page(url)
        try:
            await page.fill(selector, str(text), timeout=10_000)
            return {"success": True, "filled": selector}
        finally:
            await browser.close()
            await pw.stop()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"browser error: {exc}"}


async def _browser_snapshot(
    args: dict, db=None, user_id: str | None = None, context: dict | None = None
) -> dict:
    """Return the current page's title, URL, and text content."""
    try:
        url = _validate_url(args.get("url")) if args.get("url") else None
        pw, browser, page = await _open_page(url)
        try:
            return {
                "success": True,
                "url": page.url,
                "title": await page.title(),
                "text": (await page.inner_text("body"))[:_BROWSER_RESULT_CAP],
            }
        finally:
            await browser.close()
            await pw.stop()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"browser error: {exc}"}


async def _browser_screenshot(
    args: dict, db=None, user_id: str | None = None, context: dict | None = None
) -> dict:
    """Capture a screenshot as a base64 PNG data URL (no disk writes)."""
    try:
        url = _validate_url(args.get("url")) if args.get("url") else None
        pw, browser, page = await _open_page(url)
        try:
            png = await page.screenshot(timeout=15_000)
            b64 = base64.b64encode(png).decode("ascii")
            return {
                "success": True,
                "url": page.url,
                "screenshot_data_url": f"data:image/png;base64,{b64[:200_000]}",
            }
        finally:
            await browser.close()
            await pw.stop()
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": f"browser error: {exc}"}


def _register() -> None:
    registry.register(
        name="browser_navigate",
        schema={
            "type": "function",
            "function": {
                "name": "browser_navigate",
                "description": (
                    "Open a URL in a headless browser and return the page "
                    "title, final URL, and text preview. Only http/https; "
                    "subject to BROWSER_ALLOWED_DOMAINS."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            },
        },
        handler=_browser_navigate,
        category="browser",
        toolset="browser",
        check_fn=_browser_available,
        description="Open a URL in a headless browser and return page text.",
        emoji="🌐",
        max_result_size_chars=25_000,
    )
    registry.register(
        name="browser_click",
        schema={
            "type": "function",
            "function": {
                "name": "browser_click",
                "description": "Click a CSS selector on the current browser page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "selector": {"type": "string"},
                    },
                    "required": ["selector"],
                },
            },
        },
        handler=_browser_click,
        category="browser",
        toolset="browser",
        check_fn=_browser_available,
        description="Click a CSS selector in the headless browser.",
        emoji="🖱️",
        max_result_size_chars=25_000,
    )
    registry.register(
        name="browser_type",
        schema={
            "type": "function",
            "function": {
                "name": "browser_type",
                "description": "Type text into a CSS selector. Password fields are refused.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "selector": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["selector", "text"],
                },
            },
        },
        handler=_browser_type,
        category="browser",
        toolset="browser",
        check_fn=_browser_available,
        description="Type text into a browser input (no password fields).",
        emoji="⌨️",
        max_result_size_chars=5_000,
    )
    registry.register(
        name="browser_snapshot",
        schema={
            "type": "function",
            "function": {
                "name": "browser_snapshot",
                "description": "Return current page title, URL, and text content.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                },
            },
        },
        handler=_browser_snapshot,
        category="browser",
        toolset="browser",
        check_fn=_browser_available,
        description="Snapshot the current browser page as text.",
        emoji="📄",
        max_result_size_chars=25_000,
    )
    registry.register(
        name="browser_screenshot",
        schema={
            "type": "function",
            "function": {
                "name": "browser_screenshot",
                "description": "Capture the page as a base64 PNG data URL.",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                },
            },
        },
        handler=_browser_screenshot,
        category="browser",
        toolset="browser",
        check_fn=_browser_available,
        description="Screenshot the current browser page (base64 data URL).",
        emoji="📸",
        max_result_size_chars=210_000,
    )
    logger.info("browser toolset registered (5 tools, check_fn=playwright)")


_register()
