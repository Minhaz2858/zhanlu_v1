"""RSS Feed Service — free external news sources for market intelligence."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote_plus

import feedparser
import requests

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RSSFeedEntry:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    published_date: str = ""
    category: str = "general"


_DEFAULT_HEADERS = {
    "User-Agent": "EDIA-RSS-Ingestion/1.0 (+https://localhost)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
}


RSS_FEED_QUERIES: dict[str, list[str]] = {
    "crude_oil": [
        "Brent crude OPEC",
        "oil prices OPEC supply",
        "原油 布伦特 OPEC",
    ],
    "naphtha_petrochemical": [
        "naphtha market Asia",
        "petrochemical naphtha",
        "石脑油 碳五 碳九 裂解",
    ],
    "sinopec_cnpc": [
        "Sinopec petrochemical",
        "Sinopec China chemical",
        "中石化 中石油 挂牌价 炼化",
    ],
    "geopolitical_shipping": [
        "Hormuz shipping oil",
        "oil tanker disruption Middle East",
        "红海 油轮 航运 运费",
    ],
    "china_macro": [
        "China PMI manufacturing chemical demand",
        "China manufacturing chemical demand",
        "中国 PMI 化工 需求",
    ],
    "plant_operations": [
        "petrochemical plant shutdown restart force majeure",
        "naphtha cracker maintenance",
        "石化 装置 检修 停车 复产",
    ],
}


RSS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "crude_oil": (
        "brent",
        "crude",
        "oil",
        "opec",
        "wti",
        "refinery",
        "原油",
        "布伦特",
        "减产",
    ),
    "naphtha_petrochemical": (
        "naphtha",
        "petrochemical",
        "cracker",
        "feedstock",
        "c5",
        "c9",
        "olefin",
        "石脑油",
        "碳五",
        "碳九",
        "裂解",
    ),
    "sinopec_cnpc": (
        "sinopec",
        "cnpc",
        "china",
        "isoprene",
        "piperylene",
        "listing price",
        "c5",
        "中石化",
        "中石油",
        "挂牌价",
    ),
    "geopolitical_shipping": (
        "shipping",
        "tanker",
        "strait of hormuz",
        "red sea",
        "houthi",
        "sanctions",
        "红海",
        "航运",
        "油轮",
    ),
    "china_macro": (
        "china",
        "pmi",
        "manufacturing",
        "industrial",
        "export",
        "tariff",
        "chemical",
        "中国",
        "工业",
        "需求",
    ),
    "plant_operations": (
        "shutdown",
        "restart",
        "maintenance",
        "force majeure",
        "cracker",
        "plant",
        "capacity",
        "装置",
        "检修",
        "停车",
    ),
}


class RSSFeedService:
    """Fetch and filter relevant articles from free RSS/Atom feeds."""

    def __init__(self, timeout: int = 12):
        self.session = requests.Session()
        self.session.headers.update(_DEFAULT_HEADERS)
        raw_timeout = (os.getenv("RSS_FETCH_TIMEOUT_SECONDS", str(timeout)) or str(timeout)).strip()
        try:
            self.timeout = max(1.0, min(30.0, float(raw_timeout)))
        except ValueError:
            self.timeout = float(timeout)

    def fetch_category(
        self,
        category: str,
        max_results: int = 6,
    ) -> list[RSSFeedEntry]:
        feed_urls = self._feed_urls_for_category(category)
        keywords = RSS_KEYWORDS.get(category, ())
        entries: list[RSSFeedEntry] = []
        seen_urls: set[str] = set()

        for feed_url in feed_urls:
            for entry in self._fetch_feed(feed_url, category, keywords):
                if not entry.url or entry.url in seen_urls:
                    continue
                seen_urls.add(entry.url)
                entries.append(entry)
                if len(entries) >= max_results:
                    return entries

        return entries

    def _feed_urls_for_category(self, category: str) -> list[str]:
        queries = RSS_FEED_QUERIES.get(category, [])
        google_news_urls = [self._google_news_rss_url(query) for query in queries]

        trusted_feed_urls = []
        for raw_url in os.getenv("TRUSTED_SOURCE_URLS", "").split(","):
            url = raw_url.strip()
            if not url:
                continue
            lowered = url.lower()
            if lowered.endswith(".rss") or "/rss" in lowered or "/feed" in lowered:
                trusted_feed_urls.append(url)

        return list(dict.fromkeys(trusted_feed_urls + google_news_urls))

    def _google_news_rss_url(self, query: str) -> str:
        encoded = quote_plus(query)
        locale = (os.getenv("NEWS_RSS_LOCALE", "zh-CN") or "zh-CN").strip()
        if "-" in locale:
            lang, region = locale.split("-", 1)
            lang = lang.lower() or "zh"
            region = region.upper() or "CN"
        else:
            lang = locale.lower() or "zh"
            region = "CN"

        return (
            "https://news.google.com/rss/search"
            f"?q={encoded}&hl={lang}-{region}&gl={region}&ceid={region}:{lang}"
        )

    def _fetch_feed(
        self,
        feed_url: str,
        category: str,
        keywords: tuple[str, ...],
    ) -> list[RSSFeedEntry]:
        try:
            response = self.session.get(feed_url, timeout=self.timeout)
            response.raise_for_status()
        except Exception as e:
            logger.debug("RSS fetch failed for %s: %s", feed_url, e)
            return []

        try:
            parsed = feedparser.parse(response.content)
        except Exception as e:
            logger.debug("RSS parse failed for %s: %s", feed_url, e)
            return []

        source = parsed.feed.get("title", "") if getattr(parsed, "feed", None) else ""
        results: list[RSSFeedEntry] = []
        for item in getattr(parsed, "entries", []):
            entry = self._normalize_entry(item, source, category)
            if entry and self._is_relevant(entry, keywords):
                results.append(entry)

        if results:
            logger.info(
                "RSS feed matched %d entries for %s from %s",
                len(results),
                category,
                source or feed_url,
            )
        return results

    def _normalize_entry(
        self,
        item,
        source: str,
        category: str,
    ) -> Optional[RSSFeedEntry]:
        title = (item.get("title") or "").strip()
        url = (item.get("link") or "").strip()
        snippet = (
            item.get("summary") or item.get("description") or item.get("subtitle") or ""
        ).strip()
        published = (
            item.get("published") or item.get("updated") or item.get("pubDate") or ""
        )

        if not title or not url:
            return None

        return RSSFeedEntry(
            title=title,
            url=url,
            snippet=snippet,
            source=source,
            published_date=published,
            category=category,
        )

    def _is_relevant(self, entry: RSSFeedEntry, keywords: tuple[str, ...]) -> bool:
        haystack = f"{entry.title}\n{entry.snippet}".lower()
        return any(keyword.lower() in haystack for keyword in keywords)


_rss_feed_service: Optional[RSSFeedService] = None


def get_rss_feed_service() -> RSSFeedService:
    global _rss_feed_service
    if _rss_feed_service is None:
        _rss_feed_service = RSSFeedService()
    return _rss_feed_service
