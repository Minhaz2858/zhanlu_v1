"""web_search tool — internet search via configurable provider.

Supports Tavily, Serper, DuckDuckGo, and Bing providers selected via
SEARCH_PROVIDER env var. Uses httpx.AsyncClient for async I/O.
If the configured provider fails (e.g. unreachable network), keyless
fallback providers are tried automatically.
"""

import logging
import re
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
        # Market/language hint so results match the query language instead of
        # the server IP's geolocation. ASCII queries → English market; CJK
        # queries → zh-CN (without this, a Chinese petrochemical query on a
        # datacenter IP gets US-market mixed-language junk).
        if query.isascii():
            url += "&setlang=en&mkt=en-US"
        else:
            url += "&setlang=zh-hans&mkt=zh-CN"

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


# ---------------------------------------------------------------------------
# Relevance filter (2026-08-31)
# ---------------------------------------------------------------------------
# Poisoned-SERP failure mode: keyless HTML-scraped engines (Bing from a
# datacenter IP) return 200 OK with plausible-looking but IRRELEVANT rows —
# the C5/C9 petrochemical query got "C5驾驶证" (driver's license), CSGO item
# markets, and a military transport plane. The agent trusted them, spent a
# turn extracting the wrong pages, and fell back to internal data — the
# "not informative vs Kimi/Claude" gap. A result must share enough
# significant query tokens to survive; when nothing survives, the provider
# is treated as "no relevant results" (honest) instead of poisoning the
# agent's context.

# EN stopwords / generic market-report noise ignored when building tokens.
_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "with",
    "about", "market", "markets", "price", "prices", "trend", "trends",
    "report", "reports", "data", "china", "chinese", "current", "overview",
    "analysis", "make", "me", "please", "hello", "hi", "can", "you", "your",
    "this", "that", "from", "what", "how", "why", "new", "latest", "update",
    "info", "information", "2026", "2025", "2024", "the", "is", "are", "was",
})

def _significant_tokens(query: str) -> list[str]:
    """Meaningful tokens of a query for relevance matching.

    ASCII: words >= 3 letters plus short alphanumerics with a digit
    ("c5", "c9", "5g").  CJK: every 2-char bigram of each Chinese run
    ("裂解碳五" -> 裂解/解碳/碳五) — the standard Chinese relevance unit.
    """
    tokens: list[str] = []
    for m in re.finditer(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}", query.lower()):
        tok = m.group(0)
        if tok in _STOPWORDS:
            continue
        if tok.isascii():
            if len(tok) >= 3 or any(ch.isdigit() for ch in tok):
                tokens.append(tok)
        else:
            tokens.append(tok)
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", query.lower()):
        run = m.group(0)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _filter_relevant(results: list[dict], query: str) -> list[dict]:
    """Drop results sharing too few significant tokens with ``query``.

    ``required_overlap`` is 2 for queries with >= 4 significant tokens,
    1 otherwise (so a short query like "weather today" still works).
    Returns ``[]`` when every row is junk — the caller treats that as
    "provider returned no relevant results" and moves on.
    """
    if not results or not query:
        return results
    tokens = _significant_tokens(query)
    if not tokens:
        return results
    required = 2 if len(tokens) >= 4 else 1
    kept = []
    for r in results:
        hay = f"{r.get('title', '')} {r.get('description', '')}".lower()
        overlap = sum(1 for t in tokens if t in hay)
        if overlap >= required:
            kept.append(r)
    return kept


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

    # Usable provider chain: the configured provider first, then keyless
    # fallbacks (keyed providers are skipped when no SEARCH_API_KEY is set).
    # Hard-fail only when NO provider is usable at all (e.g. configured =
    # bocha/tavily/serper with no key AND no keyless fallback reachable) —
    # previously a keyed-but-unkeyed configured provider aborted the whole
    # tool even though bing/duckduckgo fallbacks existed.
    providers = _providers_to_try()
    if not providers:
        return {
            "success": False,
            "error": f"Search provider '{settings.SEARCH_PROVIDER}' is not configured. "
                     f"Set SEARCH_API_KEY in .env or use SEARCH_PROVIDER=duckduckgo/bing.",
        }

    results: list[dict] = []
    last_error: str | None = None
    used_fallback = False
    relevance_filtered = False
    configured_name = type(get_search_provider()).__name__
    for provider in providers:
        name = type(provider).__name__
        try:
            raw_results = await provider.search(query, limit)
        except httpx.HTTPStatusError as e:
            logger.warning("web_search %s HTTP error: %s", name, e)
            last_error = f"Search API error: {e.response.status_code}"
            raw_results = []
            continue
        except httpx.RequestError as e:
            logger.warning("web_search %s request error: %s", name, e)
            last_error = f"Search request failed: {e}"
            raw_results = []
            continue
        # Relevance filter: keyless scraped engines can return 200 OK with
        # plausible-looking but irrelevant rows (C5驾驶证 for a C5/C9 resin
        # query). Drop junk before trusting the provider; when everything is
        # junk, treat the provider as empty so the chain moves on (or the
        # turn fails honestly instead of extracting the wrong pages).
        results = _filter_relevant(raw_results, query)
        if name == configured_name and raw_results and not results:
            relevance_filtered = True
            logger.info(
                "web_search %s returned %d rows but none relevant to query — filtered",
                name, len(raw_results),
            )
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
    if relevance_filtered:
        resp["relevance_filtered"] = True
        resp["relevance_note"] = (
            "The configured provider returned rows but none shared significant "
            "query tokens — they were dropped. For higher-quality results set "
            "SEARCH_PROVIDER=tavily/serper/bocha with a SEARCH_API_KEY in .env."
        )
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
