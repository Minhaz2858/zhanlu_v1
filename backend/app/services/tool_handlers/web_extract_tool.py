"""web_extract tool — fetch and extract text content from URLs.

Fetches a URL via httpx, validates with SSRF protection first,
then extracts readable text from the HTML (strips scripts, styles, tags).
Output is truncated to protect the LLM context window.
"""

import logging
import re

import httpx
from sqlalchemy.orm import Session

from app.services.tool_registry import registry
from app.services.tool_security import is_safe_url, redact_secrets, truncate_output

logger = logging.getLogger(__name__)


# Inline base64 image payloads are token bombs — strip before returning.
_B64_IMG_RE = re.compile(
    r"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+"
)


def _strip_base64_images(text: str) -> str:
    """Replace base64 image data URIs with a compact placeholder."""
    return _B64_IMG_RE.sub("<base64 image omitted>", text)


def _html_to_text(html: str) -> str:
    """Convert HTML to plain text — strip scripts, styles, tags."""
    # Remove script and style blocks
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
    # Convert common block elements to newlines
    html = re.sub(r'<(br|p|div|h[1-6]|li|tr)[^>]*>', '\n', html, flags=re.IGNORECASE)
    # Strip all remaining tags
    html = re.sub(r'<[^>]+>', '', html)
    # Decode common HTML entities
    html = html.replace('&nbsp;', ' ').replace('&amp;', '&')
    html = html.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    html = html.replace('&#39;', "'")
    # Collapse excessive whitespace
    html = re.sub(r'\n{3,}', '\n\n', html)
    html = re.sub(r'[ \t]+', ' ', html)
    # Strip leading/trailing whitespace per line
    lines = [line.strip() for line in html.split('\n')]
    return '\n'.join(line for line in lines if line)


async def _web_extract(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    url = args.get("url", "").strip()
    max_chars = args.get("max_chars", 6000)

    if not url:
        return {"success": False, "error": "url is required"}

    # SSRF protection — block private/internal URLs
    if not is_safe_url(url):
        return {"success": False, "error": "URL blocked: targets a private or internal address"}

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ZhanluAgent/1.0)"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return {"success": False, "error": f"HTTP {e.response.status_code}: {e.response.reason_phrase}"}
    except httpx.RequestError as e:
        return {"success": False, "error": f"Request failed: {str(e)}"}

    content_type = resp.headers.get("content-type", "")

    # If it's not HTML, return raw text (truncated)
    if "text/html" not in content_type and "text/" not in content_type:
        raw = _strip_base64_images(resp.text)[:max_chars]
        content = redact_secrets(raw)
        resp_data = {
            "success": True,
            "url": str(resp.url),
            "content_type": content_type,
            "content": content,
        }
        if len(content.strip()) < 100:
            resp_data["degraded"] = True
            resp_data["degraded_reason"] = (
                "extracted content is very short — the resource may be empty, "
                "binary, or require authentication."
            )
        return resp_data

    # Extract text from HTML
    text = _html_to_text(resp.text)
    text = _strip_base64_images(text)
    text = redact_secrets(text)
    text = truncate_output(text, max_chars)

    resp_data = {
        "success": True,
        "url": str(resp.url),
        "title": _extract_title(resp.text),
        "content_type": content_type,
        "content": text,
    }
    if len(text.strip()) < 100:
        resp_data["degraded"] = True
        resp_data["degraded_reason"] = (
            "extracted text is very short despite a successful HTTP response — "
            "page may be JS-rendered, paywalled, or blocking bots. Consider "
            "using agent_browser to render the page and extract visible text."
        )
    return resp_data


def _extract_title(html: str) -> str:
    """Extract the <title> tag content from HTML."""
    match = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL | re.IGNORECASE)
    if match:
        return re.sub(r'\s+', ' ', match.group(1)).strip()
    return ""


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

WEB_EXTRACT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_extract",
        "description": (
            "Fetch a web page and extract its text content. "
            "Use this after web_search to read the full content of a result, "
            "or when you have a specific URL to read. "
            "Strips HTML, scripts, and styles — returns clean text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch (must be http or https)",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Maximum characters to return (default 6000)",
                    "default": 6000,
                },
            },
            "required": ["url"],
        },
    },
}

registry.register(
    name="web_extract",
    schema=WEB_EXTRACT_SCHEMA,
    handler=_web_extract,
    category="web",
    enabled_by_default=True,
    description="Extract text content from a web URL.",
)
