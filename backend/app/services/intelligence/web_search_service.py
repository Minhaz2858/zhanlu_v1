"""Web Search Service — real-time web search for market intelligence.

Provides actual web search results using DuckDuckGo (free, no API key)
with optional Serper.dev (Google results) or Tavily fallback.

Backends (priority order):
  1. Serper.dev  — if SERPER_API_KEY is set  (~$0.001/search, Google results)
  2. Tavily      — if TAVILY_API_KEY is set  (AI-optimised snippets)
  3. DuckDuckGo  — always available, free, no key required

Trusted-site scraping:
  Set TRUSTED_SOURCE_URLS in .env (comma-separated RSS or HTML URLs).
  The system fetches these on every relevant query and injects their
  content into the AI context automatically.
"""

from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET
from html import unescape
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)


class SearchResult:
    """A single web search result."""

    __slots__ = ("title", "url", "snippet", "source", "published_date")

    def __init__(
        self,
        title: str,
        url: str,
        snippet: str,
        source: str = "",
        published_date: str = "",
    ):
        self.title = title
        self.url = url
        self.snippet = snippet
        self.source = source
        self.published_date = published_date

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "published_date": self.published_date,
        }

    def to_text(self) -> str:
        """Readable text for LLM context injection."""
        parts = [f"Title: {self.title}"]
        if self.source:
            parts.append(f"Source: {self.source}")
        if self.published_date:
            parts.append(f"Date: {self.published_date}")
        parts.append(f"Content: {self.snippet}")
        return "\n".join(parts)


class WebSearchService:
    """Unified web search: Serper > Tavily > DuckDuckGo (auto-selected by key presence)."""

    def __init__(self):
        self._serper_key = os.getenv("SERPER_API_KEY", "").strip()
        self._tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
        if self._serper_key:
            self._backend = "serper"
        elif self._tavily_key:
            self._backend = "tavily"
        else:
            self._backend = "duckduckgo"
        logger.info("WebSearchService initialized with backend: %s", self._backend)

    @staticmethod
    def _is_disabled() -> bool:
        mode = (os.getenv("WEB_SEARCH_MODE", "off") or "off").strip().lower()
        if mode in {"off", "0", "false", "disable", "disabled"}:
            return True
        hard_flag = (os.getenv("DISABLE_WEB_SEARCH", "false") or "false").strip().lower()
        return hard_flag in {"1", "true", "yes", "on"}

    def search(
        self,
        query: str,
        max_results: int = 8,
        region: str = "wt-wt",
        engine: str = "auto",
    ) -> list[SearchResult]:
        """General web search. Returns list of SearchResult."""
        if self._is_disabled():
            return []
        region = self._resolve_region_for_engine(engine, region)
        if self._backend == "serper" and self._serper_key:
            return self._serper_search(query, max_results)
        if self._backend == "tavily" and self._tavily_key:
            return self._tavily_search(query, max_results)
        return self._ddg_text_search(query, max_results, region)

    def search_news(
        self,
        query: str,
        max_results: int = 8,
        region: str = "wt-wt",
        timelimit: str = "w",
        engine: str = "auto",
    ) -> list[SearchResult]:
        """News-specific search (recent articles). timelimit: d=day, w=week, m=month."""
        if self._is_disabled():
            return []
        region = self._resolve_region_for_engine(engine, region)
        if self._backend == "serper" and self._serper_key:
            return self._serper_search(query, max_results, search_type="news")
        if self._backend == "tavily" and self._tavily_key:
            return self._tavily_search(query, max_results, search_depth="basic")
        return self._ddg_news_search(query, max_results, region, timelimit)

    def search_chinese(
        self,
        query: str,
        max_results: int = 8,
    ) -> list[SearchResult]:
        """Search with Chinese region bias for 隆众/卓创/金联创 sources."""
        return self.search(query, max_results, region="cn-zh")

    def multi_search(
        self,
        queries: list[str],
        max_per_query: int = 5,
    ) -> list[SearchResult]:
        """Run multiple search queries and aggregate results."""
        if self._is_disabled():
            return []
        all_results: list[SearchResult] = []
        seen_urls: set[str] = set()
        for q in queries:
            try:
                results = self.search(q, max_results=max_per_query)
                for r in results:
                    if r.url not in seen_urls:
                        seen_urls.add(r.url)
                        all_results.append(r)
            except Exception as e:
                logger.warning("Multi-search query failed for '%s': %s", q, e)
        return all_results

    def search_multi_engine(
        self,
        query: str,
        engines: list[str],
        max_results_per_engine: int = 5,
        include_news: bool = True,
        timelimit: str = "w",
    ) -> list[SearchResult]:
        """Aggregate results across logical engines via region-aware DDG/Tavily calls.

        Notes:
        - Engine names are treated as routing hints (CN vs global) and source bias.
        - We keep one backend implementation for reliability and lower dependency risk.
        """
        if self._is_disabled():
            return []
        unique_urls: set[str] = set()
        merged: list[SearchResult] = []

        for engine in engines:
            try:
                web_results = self.search(
                    query=query,
                    max_results=max_results_per_engine,
                    engine=engine,
                )
                for item in web_results:
                    if item.url and item.url not in unique_urls:
                        unique_urls.add(item.url)
                        merged.append(item)

                if include_news:
                    news_results = self.search_news(
                        query=query,
                        max_results=max(2, max_results_per_engine // 2),
                        timelimit=timelimit,
                        engine=engine,
                    )
                    for item in news_results:
                        if item.url and item.url not in unique_urls:
                            unique_urls.add(item.url)
                            merged.append(item)
            except Exception as exc:
                logger.warning("Engine search failed (%s): %s", engine, exc)

        return merged

    @staticmethod
    def _resolve_region_for_engine(engine: str, fallback_region: str) -> str:
        """Map logical engine names to DDG region hints."""
        raw = (engine or "auto").strip().lower()
        if raw in {"auto", ""}:
            return fallback_region

        cn_engines = {"baidu", "sogou", "360", "bing-cn", "wechat", "toutiao"}
        global_engines = {
            "google",
            "google-hk",
            "duckduckgo",
            "brave",
            "startpage",
            "qwant",
            "yahoo",
            "wolframalpha",
            "bing-int",
        }

        if raw in cn_engines:
            return "cn-zh"
        if raw in global_engines:
            return "wt-wt"
        return fallback_region

    # ── DuckDuckGo backend ────────────────────────────────────────────────

    def _ddg_text_search(
        self, query: str, max_results: int, region: str
    ) -> list[SearchResult]:
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS

            results: list[SearchResult] = []
            with DDGS() as ddgs:
                # Newer ddgs versions accept `region`; older ones may not.
                try:
                    iterator = ddgs.text(query, max_results=max_results, region=region)
                except TypeError:
                    iterator = ddgs.text(query, max_results=max_results)
                for r in iterator:
                    results.append(
                        SearchResult(
                            title=r.get("title", ""),
                            url=r.get("href", ""),
                            snippet=r.get("body", ""),
                            source=(
                                r.get("href", "").split("/")[2] if r.get("href") else ""
                            ),
                        )
                    )
            logger.info(
                "DuckDuckGo text search: %d results for '%s'", len(results), query[:60]
            )
            return results
        except ImportError:
            logger.error("ddgs not installed. Run: pip install ddgs")
            return []
        except Exception as e:
            logger.warning("DuckDuckGo text search failed: %s", e)
            return []

    def _ddg_news_search(
        self, query: str, max_results: int, region: str, timelimit: str
    ) -> list[SearchResult]:
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS

            results: list[SearchResult] = []
            with DDGS() as ddgs:
                # Newer ddgs versions accept `region`; older ones may not.
                try:
                    iterator = ddgs.news(
                        query,
                        max_results=max_results,
                        timelimit=timelimit,
                        region=region,
                    )
                except TypeError:
                    iterator = ddgs.news(
                        query, max_results=max_results, timelimit=timelimit
                    )

                for r in iterator:
                    results.append(
                        SearchResult(
                            title=r.get("title", ""),
                            url=r.get("url", ""),
                            snippet=r.get("body", ""),
                            source=r.get("source", ""),
                            published_date=r.get("date", ""),
                        )
                    )
            logger.info(
                "DuckDuckGo news search: %d results for '%s'", len(results), query[:60]
            )
            return results
        except ImportError:
            logger.error("ddgs not installed. Run: pip install ddgs")
            return []
        except Exception as e:
            logger.warning("DuckDuckGo news search failed: %s", e)
            return []

    # ── Tavily backend (optional, higher quality) ─────────────────────────

    def _tavily_search(
        self,
        query: str,
        max_results: int,
        search_depth: str = "basic",
    ) -> list[SearchResult]:
        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=self._tavily_key)
            response = client.search(
                query=query,
                max_results=max_results,
                search_depth=search_depth,
            )
            results: list[SearchResult] = []
            for r in response.get("results", []):
                results.append(
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("url", ""),
                        snippet=r.get("content", ""),
                        source=r.get("url", "").split("/")[2] if r.get("url") else "",
                        published_date=r.get("published_date", ""),
                    )
                )
            logger.info("Tavily search: %d results for '%s'", len(results), query[:60])
            return results
        except ImportError:
            logger.warning("tavily-python not installed, falling back to DuckDuckGo")
            return self._ddg_text_search(query, max_results, "wt-wt")
        except Exception as e:
            logger.warning("Tavily search failed: %s — falling back to DuckDuckGo", e)
            return self._ddg_text_search(query, max_results, "wt-wt")

    def _serper_search(
        self,
        query: str,
        max_results: int = 8,
        search_type: str = "search",  # "search" or "news"
    ) -> list[SearchResult]:
        """Google Search via Serper.dev (https://serper.dev)."""
        try:
            import requests
            url = "https://google.serper.dev/{}".format(search_type)
            headers = {"X-API-KEY": self._serper_key, "Content-Type": "application/json"}
            payload = {"q": query, "num": max_results}
            resp = requests.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            results = []
            for item in data.get("organic", []) + data.get("news", []):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source=item.get("source", "") or (item.get("link", "").split("/")[2] if item.get("link") else ""),
                    published_date=item.get("date", ""),
                ))
            return results
        except Exception as e:
            logger.warning("Serper search failed: %s", e)
            return []

    def scrape_trusted_sources(self, max_per_site: int = 5) -> list[SearchResult]:
        """Scrape all URLs in TRUSTED_SOURCE_URLS env var (RSS or HTML)."""
        scrape_enabled = (
            os.getenv("TRUSTED_SOURCE_SCRAPE_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if not scrape_enabled:
            return []
        urls = [url.strip() for url in os.getenv("TRUSTED_SOURCE_URLS", "").split(",") if url.strip()]
        raw_max_sites = (os.getenv("TRUSTED_SOURCE_MAX_SITES", "4") or "4").strip()
        try:
            max_sites = max(1, min(20, int(raw_max_sites)))
        except ValueError:
            max_sites = 4
        urls = urls[:max_sites]
        results = []
        for url in urls:
            try:
                if url.endswith(".rss") or "/rss" in url or "/feed" in url:
                    # RSS/Atom feed
                    resp = self._trusted_get(url)
                    root = ET.fromstring(resp.content)
                    for item in root.findall(".//item")[:max_per_site]:
                        title = self._normalize_text(item.findtext("title") or "")
                        link = item.findtext("link") or ""
                        desc = self._normalize_text(item.findtext("description") or "")
                        pub = item.findtext("pubDate") or ""
                        results.append(SearchResult(title=title, url=link, snippet=desc, published_date=pub))
                else:
                    # HTML homepage: prefer scraping linked article pages.
                    resp = self._trusted_get(url)
                    html = self._decode_response_text(resp)
                    source_host = (urlparse(url).hostname or "").lower()
                    added = 0

                    for article_url in self._extract_article_links(url, html, max_links=max_per_site):
                        try:
                            article_resp = self._trusted_get(article_url)
                            article_html = self._decode_response_text(article_resp)
                            title_text, snippet_text = self._extract_html_key_info(article_html)
                            if any(token in (title_text or "") for token in ("简介", "介绍", "about", "联系我们")):
                                continue
                            if not self._looks_useful_text(title_text) and not self._looks_useful_text(snippet_text):
                                continue
                            results.append(
                                SearchResult(
                                    title=title_text or article_url,
                                    url=article_url,
                                    snippet=snippet_text,
                                    source=source_host,
                                )
                            )
                            added += 1
                        except Exception as article_exc:
                            logger.debug("Trusted article scrape failed for %s: %s", article_url, article_exc)

                    if added == 0:
                        title_text, snippet_text = self._extract_html_key_info(html)
                        results.append(
                            SearchResult(
                                title=title_text or url,
                                url=url,
                                snippet=snippet_text,
                                source=source_host,
                            )
                        )
            except Exception as e:
                logger.warning(f"Trusted source scrape failed for {url}: {e}")
        return results

    @staticmethod
    def _extract_article_links(base_url: str, html: str, max_links: int = 5) -> list[str]:
        """Find likely article links from a homepage HTML document."""
        base_host = (urlparse(base_url).hostname or "").lower()
        scored: list[tuple[int, str]] = []
        seen: set[str] = set()

        for href in re.findall(r'<a[^>]+href=["\']([^"\']+)["\']', html, re.I):
            href = (href or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue

            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)
            if parsed.scheme not in {"http", "https"}:
                continue

            host = (parsed.hostname or "").lower()
            if base_host and host and host != base_host and not host.endswith(f".{base_host}"):
                continue

            path = (parsed.path or "").lower()
            looks_article = bool(
                re.search(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2}|\d{8}|article|news|content|detail|show|\.s?html$)", path)
            )
            if not looks_article:
                continue

            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if normalized in seen:
                continue

            seen.add(normalized)
            score = 0
            if "/xnews/content/" in path or "/content/" in path:
                score += 50
            date_match = re.search(r"(20\d{2})[-/]?(\d{2})[-/]?(\d{2})", path)
            if date_match:
                y, m, d = date_match.groups()
                score += int(f"{y}{m}{d}")
            elif re.search(r"20\d{2}", path):
                year_match = re.search(r"(20\d{2})", path)
                if year_match:
                    score += int(year_match.group(1)) * 10000
            scored.append((score, normalized))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [url for _, url in scored[: max(1, max_links)]]

    @staticmethod
    def _looks_useful_text(value: str) -> bool:
        text = (value or "").strip()
        if not text:
            return False
        if text.startswith(("http://", "https://")):
            return False
        return len(text) >= 6

    @staticmethod
    def _decode_response_text(resp: requests.Response) -> str:
        """Decode response body with explicit charset handling for CJK pages."""
        encoding = (resp.encoding or "").strip().lower()
        if not encoding or encoding == "iso-8859-1":
            encoding = (resp.apparent_encoding or "utf-8").strip().lower()

        try:
            text = resp.content.decode(encoding, errors="replace")
        except Exception:
            text = resp.text
        return WebSearchService._repair_mojibake(text)

    @staticmethod
    def _repair_mojibake(value: str) -> str:
        """Best-effort mojibake repair while preserving original markup."""
        text = (value or "").strip()
        repaired_candidates = [text]
        for enc in ("latin-1", "cp1252"):
            try:
                repaired_candidates.append(
                    text.encode(enc, errors="ignore").decode("utf-8", errors="ignore")
                )
            except Exception:
                continue

        def _score(s: str) -> int:
            cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
            mojibake = len(
                re.findall(
                    r"[ÃÂÄÅÆÇÐÑØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ]",
                    s,
                )
            )
            replacement = s.count("�")
            return cjk * 3 - mojibake * 2 - replacement * 4

        return max(repaired_candidates, key=_score)

    @staticmethod
    def _extract_html_key_info(html: str) -> tuple[str, str]:
        """Extract meaningful title/snippet from HTML for trusted-source events."""
        patterns_title = [
            r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:title["\']',
            r"<title>(.*?)</title>",
            r"<h1[^>]*>(.*?)</h1>",
        ]
        patterns_desc = [
            r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
            r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
            r"<p[^>]*>(.*?)</p>",
        ]

        def _first_match(patterns: list[str]) -> str:
            for p in patterns:
                m = re.search(p, html, re.I | re.S)
                if not m:
                    continue
                text = WebSearchService._normalize_text(m.group(1))
                if text and text not in {"/", "http://", "https://"}:
                    return text
            return ""

        title = _first_match(patterns_title)
        snippet = _first_match(patterns_desc)

        # If description is empty, derive a short body excerpt as fallback.
        if not snippet:
            body = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
            body = re.sub(r"<style[\s\S]*?</style>", " ", body, flags=re.I)
            body = re.sub(r"<[^>]+>", " ", body)
            body = WebSearchService._normalize_text(body)
            if body:
                snippet = body[:220]

        return title[:180], snippet[:280]

    @staticmethod
    def _normalize_text(value: str) -> str:
        text = unescape((value or "").strip())
        # Remove obvious HTML tags that occasionally leak from RSS descriptions.
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        # Attempt to repair common UTF-8/Latin mojibake like "ä¸­å½".
        repaired_candidates = [text]
        for enc in ("latin-1", "cp1252"):
            try:
                repaired_candidates.append(text.encode(enc, errors="ignore").decode("utf-8", errors="ignore"))
            except Exception:
                continue

        def _score(s: str) -> int:
            cjk = len(re.findall(r"[\u4e00-\u9fff]", s))
            mojibake = len(re.findall(r"[ÃÂÄÅÆÇÐÑØÙÚÛÜÝÞßàáâãäåæçèéêëìíîïðñòóôõöøùúûüýþÿ]", s))
            replacement = s.count("�")
            return cjk * 3 - mojibake * 2 - replacement * 4

        best = max(repaired_candidates, key=_score)
        return re.sub(r"\s+", " ", best).strip()

    @staticmethod
    def _trusted_get(url: str):
        """GET for trusted-source scraping with narrow SSL-cert fallback."""
        raw_timeout = (os.getenv("TRUSTED_SOURCE_TIMEOUT_SECONDS", "8") or "8").strip()
        try:
            timeout = max(1.0, min(30.0, float(raw_timeout)))
        except ValueError:
            timeout = 8.0
        try:
            return requests.get(url, timeout=timeout)
        except requests.exceptions.SSLError:
            logger.warning("Trusted source SSL verify failed for %s; retrying without verify", url)
            return requests.get(url, verify=False, timeout=timeout)


# ── Module-level singleton ────────────────────────────────────────────────
_web_search: Optional[WebSearchService] = None


def get_web_search() -> WebSearchService:
    """Get or create the singleton WebSearchService."""
    global _web_search
    if _web_search is None:
        _web_search = WebSearchService()
    return _web_search
