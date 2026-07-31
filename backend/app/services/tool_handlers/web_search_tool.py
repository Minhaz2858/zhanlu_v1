"""web_search tool — internet search via configurable provider.

Supports Tavily, Serper, DuckDuckGo, and Bing providers selected via
SEARCH_PROVIDER env var. Uses httpx.AsyncClient for async I/O.
If the configured provider fails (e.g. unreachable network), keyless
fallback providers are tried automatically.
"""

import logging
from abc import ABC, abstractmethod

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.tool_registry import registry
from app.services.tool_security import redact_secrets, truncate_output

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Search Provider Abstraction
# ---------------------------------------------------------------------------

class SearchProvider(ABC):
    """Abstract base for search providers."""

    @abstractmethod
    async def search(self, query: str, limit: int = 5) -> list[dict]:
        """Return list of {title, url, description} dicts."""
        ...


class TavilyProvider(SearchProvider):
    """Tavily AI-optimized search API."""

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.SEARCH_API_KEY,
                    "query": query,
                    "max_results": min(limit, 10),
                    "include_answer": False,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for r in data.get("results", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("content", "")[:500],
            })
        return results


class SerperProvider(SearchProvider):
    """Serper.dev Google search API."""

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": settings.SEARCH_API_KEY, "Content-Type": "application/json"},
                json={"q": query, "num": min(limit, 10)},
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        for r in data.get("organic", []):
            results.append({
                "title": r.get("title", ""),
                "url": r.get("link", ""),
                "description": r.get("snippet", "")[:500],
            })
        return results


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo search — no API key needed (uses HTML scraping)."""

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        from urllib.parse import quote_plus
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            )
            resp.raise_for_status()
            html = resp.text

        # Parse result links from DuckDuckGo HTML
        import re
        results = []
        # DuckDuckGo HTML format: <a class="result__a" href="...">title</a>
        # and <a class="result__snippet" ...>description</a>
        link_pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

        # Strip HTML tags from titles
        tag_re = re.compile(r'<[^>]+>')

        for i, (href, title) in enumerate(links[:limit]):
            title_clean = tag_re.sub("", title).strip()
            snippet = ""
            if i < len(snippets):
                snippet = tag_re.sub("", snippets[i]).strip()
            results.append({
                "title": title_clean,
                "url": href,
                "description": snippet[:500],
            })

        return results


class BingProvider(SearchProvider):
    """Bing search — no API key needed (HTML scraping).

    Useful on networks where DuckDuckGo is unreachable. Parses the
    organic ``<li class="b_algo">`` result blocks.
    """

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        from html import unescape
        from urllib.parse import quote_plus

        url = f"https://www.bing.com/search?q={quote_plus(query)}"
        # For ASCII (English) queries, hint the English market so results
        # match the query language instead of the server IP's geolocation.
        if query.isascii():
            url += "&setlang=en&mkt=en-US"

        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                    )
                },
            )
            resp.raise_for_status()
            page = resp.text

        import re

        tag_re = re.compile(r"<[^>]+>")
        results = []
        # Organic results live in <li class="b_algo"> blocks: the first
        # <h2><a> is the title link, the first <p> is the snippet.
        for block in page.split('<li class="b_algo"')[1:]:
            m = re.search(
                r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                block,
                re.DOTALL,
            )
            if not m:
                continue
            href, title_html = m.group(1), m.group(2)
            title = unescape(tag_re.sub("", title_html)).strip()
            snippet = ""
            p = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
            if p:
                snippet = unescape(tag_re.sub("", p.group(1))).strip()
            results.append({
                "title": title,
                "url": href,
                "description": snippet[:500],
            })
            if len(results) >= limit:
                break
        return results


class BochaProvider(SearchProvider):
    """Bocha (博查) Web Search API — China-native, AI-optimized.

    POST https://api.bochaai.com/v1/web-search with a Bearer key.
    Recommended for deployments on China networks where western search
    endpoints are blocked or geo-degraded. Get a key at open.bochaai.com.
    """

    async def search(self, query: str, limit: int = 5) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.bochaai.com/v1/web-search",
                headers={
                    "Authorization": f"Bearer {settings.SEARCH_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "summary": True,
                    "count": min(limit, 10),
                },
            )
            resp.raise_for_status()
            data = resp.json()

        results = []
        # Bing-compatible shape: data.webPages.value[] with name/url/snippet
        pages = (data.get("data") or {}).get("webPages") or {}
        for r in pages.get("value") or []:
            results.append({
                "title": r.get("name", ""),
                "url": r.get("url", ""),
                "description": (r.get("summary") or r.get("snippet") or "")[:500],
            })
        return results


def _provider_for(name: str) -> SearchProvider:
    """Instantiate a provider by name (defaults to DuckDuckGo)."""
    name = (name or "").lower()
    if name == "tavily":
        return TavilyProvider()
    if name == "serper":
        return SerperProvider()
    if name == "bocha":
        return BochaProvider()
    if name == "bing":
        return BingProvider()
    return DuckDuckGoProvider()


def get_search_provider() -> SearchProvider:
    """Factory: returns provider based on settings.SEARCH_PROVIDER."""
    return _provider_for(settings.SEARCH_PROVIDER)


def _providers_to_try() -> list[SearchProvider]:
    """Ordered provider chain: configured provider first, then fallbacks.

    Fallbacks cover the case where the configured provider is unreachable
    from this network (e.g. DuckDuckGo blocked) — keyless providers are
    always included, keyed ones only when SEARCH_API_KEY is set.
    """
    configured = settings.SEARCH_PROVIDER.lower()
    chain = [configured]
    for name in ("bing", "duckduckgo", "bocha", "tavily", "serper"):
        if name not in chain:
            chain.append(name)
    providers = []
    for name in chain:
        if name in ("tavily", "serper", "bocha") and not settings.SEARCH_API_KEY:
            continue  # keyed providers are unusable without an API key
        providers.append(_provider_for(name))
    return providers


# ---------------------------------------------------------------------------
# Tool Handler
# ---------------------------------------------------------------------------

async def _web_search(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    query = args.get("query", "").strip()
    limit = min(args.get("limit", 5), 10)

    if not query:
        return {"success": False, "error": "query is required"}

    if not settings.search_config_ok():
        return {
            "success": False,
            "error": f"Search provider '{settings.SEARCH_PROVIDER}' is not configured. "
                     f"Set SEARCH_API_KEY in .env or use SEARCH_PROVIDER=duckduckgo.",
        }

    results: list[dict] = []
    last_error: str | None = None
    used_fallback = False
    configured_name = type(get_search_provider()).__name__
    for provider in _providers_to_try():
        name = type(provider).__name__
        try:
            results = await provider.search(query, limit)
        except httpx.HTTPStatusError as e:
            logger.warning("web_search %s HTTP error: %s", name, e)
            last_error = f"Search API error: {e.response.status_code}"
            results = []
            continue
        except httpx.RequestError as e:
            logger.warning("web_search %s request error: %s", name, e)
            last_error = f"Search request failed: {e}"
            results = []
            continue
        if results:
            if name != configured_name:
                logger.info("web_search succeeded via fallback provider %s", name)
                used_fallback = True
            break
        last_error = "provider returned no results"

    if not results:
        return {
            "success": False,
            "error": f"web_search failed across providers: {last_error or 'unknown error'}",
        }

    # Redact secrets from results before returning
    for r in results:
        r["description"] = redact_secrets(r["description"])

    # Degraded-result detection: "200 OK but thin" — e.g. a provider returned
    # result rows but every snippet is empty (geo-blocking, parsing drift, or
    # a results page that loaded but contained no organic results). Flagging
    # this lets the agent escalate to agent_browser instead of trusting the
    # empty success.
    total_content = sum(len((r.get("description") or "").strip()) for r in results)
    all_snippets_empty = all(not (r.get("description") or "").strip() for r in results)
    degraded = bool(all_snippets_empty and len(results) > 0) or (len(results) > 0 and total_content < 50)
    degraded_reason = None
    if degraded:
        degraded_reason = (
            "search returned results but all snippets are empty or extremely "
            "short — provider may be geo-blocked or rate-limited. Consider "
            "agent_browser to load a search-engine results page and extract "
            "visible results directly."
        )

    resp = {
        "success": True,
        "query": query,
        "results": results,
        "count": len(results),
    }
    if used_fallback:
        resp["used_fallback_provider"] = True
    if degraded:
        resp["degraded"] = True
        resp["degraded_reason"] = degraded_reason
    return resp


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

WEB_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Returns a list of results "
            "with title, URL, and a short description for each. Use this to find "
            "facts, documentation, news, or any information not in your training data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of results (default 5, max 10)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

registry.register(
    name="web_search",
    schema=WEB_SEARCH_SCHEMA,
    handler=_web_search,
    category="web",
    enabled_by_default=True,
    requires_config=[],  # DuckDuckGo needs no config
    description="Search the web for current information.",
)
