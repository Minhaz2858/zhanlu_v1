"""News Ingestion Agent — background news scanner for market intelligence.

Periodically fetches news from RSS feeds and web search, extracts structured
events using the LLM, deduplicates, and stores them in the intelligence registry.

Ported from EDIA 5.1.2. Major adaptation: EDIA used raw ``threading.Thread``
with ``while True: sleep()``. Zhanlu uses the async ``_periodic_task`` pattern
from ``scheduled_tasks.py``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.intelligence.event_types import ExtractedEvent
from app.services.intelligence.event_registry import IntelligenceEventRegistry
from app.services.intelligence.event_deduplicator import EventDeduplicator
from app.services.intelligence.event_extractor import EventExtractor
from app.services.intelligence.rss_feed_service import get_rss_feed_service
from app.services.intelligence.web_search_service import get_web_search

logger = logging.getLogger(__name__)

_SCAN_CATEGORIES = [
    "crude_oil", "naphtha_petrochemical", "sinopec_cnpc",
    "geopolitical_shipping", "china_macro", "plant_operations",
]


class NewsIngestionAgent:
    """Background agent that scans news sources and extracts market events."""

    def __init__(
        self,
        db: AsyncSession,
        org_id: str = "default-org",
        llm_caller: Optional[Callable] = None,
    ):
        self._db = db
        self._org_id = org_id
        self._registry = IntelligenceEventRegistry(db)
        self._extractor = EventExtractor(llm_caller=llm_caller)
        self._deduplicator = EventDeduplicator(window_hours=48)
        self._rss = get_rss_feed_service()
        self._web_search = get_web_search()
        self._agent_name = "news_ingestion_agent"

    async def run_news_cycle(self, trigger: str = "scheduled") -> dict[str, Any]:
        """Run one news ingestion cycle. Returns a status report."""
        start_time = time.time()
        started_at = datetime.utcnow().isoformat()

        await self._registry.upsert_ingestion_status(
            self._agent_name,
            {"running": True, "cycle_in_progress": True, "last_started_at": started_at},
            org_id=self._org_id,
        )

        articles_seen = 0
        events_extracted = 0
        events_stored = 0
        errors = []

        try:
            articles = self._fetch_rss_articles()
            articles_seen += len(articles)

            web_articles = self._fetch_web_search_articles()
            articles_seen += len(web_articles)

            all_articles = articles + web_articles

            for article in all_articles:
                try:
                    event = await self._extractor.extract(
                        article_text=article.get("text", ""),
                        category=article.get("category", "general"),
                        source_url=article.get("url", ""),
                    )
                    if event:
                        events_extracted += 1
                        if not self._deduplicator.is_duplicate(event):
                            self._deduplicator.mark_seen(event)
                            await self._registry.upsert_event(event, org_id=self._org_id)
                            events_stored += 1
                except Exception as e:
                    errors.append(f"Article extraction failed: {e}")
                    logger.debug("Article extraction failed: %s", e)

            elapsed_ms = (time.time() - start_time) * 1000
            completed_at = datetime.utcnow().isoformat()

            status = {
                "running": False,
                "cycle_in_progress": False,
                "last_completed_at": completed_at,
                "last_success_at": completed_at,
                "last_error": "; ".join(errors[:3]) if errors else None,
                "last_cycle_duration_ms": round(elapsed_ms, 1),
                "last_articles_seen": articles_seen,
                "last_events_extracted": events_extracted,
                "last_events_stored": events_stored,
            }

            await self._registry.upsert_ingestion_status(
                self._agent_name, status, org_id=self._org_id
            )

            logger.info(
                "News ingestion cycle: %d articles, %d extracted, %d stored (%.1fs)",
                articles_seen, events_extracted, events_stored, elapsed_ms / 1000,
            )

            return {
                "success": True,
                "articles_seen": articles_seen,
                "events_extracted": events_extracted,
                "events_stored": events_stored,
                "duration_ms": round(elapsed_ms, 1),
                "errors": errors,
            }

        except Exception as e:
            elapsed_ms = (time.time() - start_time) * 1000
            error_msg = f"News ingestion cycle failed: {e}"
            logger.error(error_msg)
            await self._registry.upsert_ingestion_status(
                self._agent_name,
                {
                    "running": False, "cycle_in_progress": False,
                    "last_completed_at": datetime.utcnow().isoformat(),
                    "last_error": error_msg,
                    "last_cycle_duration_ms": round(elapsed_ms, 1),
                },
                org_id=self._org_id,
            )
            return {
                "success": False, "error": error_msg,
                "articles_seen": articles_seen, "events_extracted": events_extracted,
                "events_stored": events_stored,
            }

    def _fetch_rss_articles(self) -> list[dict[str, Any]]:
        """Fetch articles from RSS feeds across all categories."""
        articles = []
        for category in _SCAN_CATEGORIES:
            try:
                entries = self._rss.fetch_category(category, max_results=4)
                for entry in entries:
                    articles.append({
                        "text": f"{entry.title}. {entry.snippet}",
                        "url": entry.url,
                        "category": category,
                        "source": entry.source,
                        "published_date": entry.published_date,
                    })
            except Exception as e:
                logger.debug("RSS fetch failed for %s: %s", category, e)
        return articles

    def _fetch_web_search_articles(self) -> list[dict[str, Any]]:
        """Fetch articles via web search (if enabled)."""
        if self._web_search._is_disabled():
            return []
        articles = []
        queries = [
            "petrochemical C5 C9 market news",
            "naphtha crude oil price impact",
            "SINOPEC listing price change",
        ]
        try:
            for query in queries:
                results = self._web_search.search_news(query, max_results=3)
                for r in results:
                    articles.append({
                        "text": f"{r.title}. {r.snippet}",
                        "url": r.url,
                        "category": "general",
                        "source": r.source,
                        "published_date": r.published_date,
                    })
        except Exception as e:
            logger.debug("Web search fetch failed: %s", e)
        return articles
