"""Unified Forecasting Agent Service.

A thin facade over the four fragmented forecasting data paths so callers
(routers, tools, decision board, weekly digest) no longer need to know which
underlying service to import:

    * ForecastEngine                    — canonical compute (PostgreSQL)
    * market_dashboard_service (mds)    — MySQL warehouse price reads + ForecastRun reads
    * weekly_digest/generator           — weekly summary + LLM narration
    * decision_board_service            — composite decision board
    * forecasting/analyst/service       — per-product LLM analyst brief
    * forecasting/forecast_trust_tier   — trust-tier computation

Design notes
------------
* Session-per-call: the service holds a ``Session`` and every method is
  stateless beyond it, matching ``ForecastEngine(db)``.
* Zero logic moved: every method delegates to the existing implementation.
  Return types are therefore identical to the wrapped functions (no frontend
  impact).
* Lazy engine: ``ForecastEngine`` is constructed on first use.
* Lazy imports for ``decision_board_service`` and ``weekly_digest.generator``
  to avoid circular imports once those modules are redirected through this
  facade behind ``UNIFIED_FORECASTING_SERVICE_ENABLED``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.forecasting import ForecastRun, ForecastTarget
from app.models.weekly_digest import WeeklyReportCard, WeeklyReportProduct
from app.services.forecasting.engine import ForecastEngine
from app.services.forecasting import forecast_trust_tier as _trust_tier
from app.services.forecasting.analyst import service as _analyst
from app.services import market_dashboard_service as mds

logger = logging.getLogger(__name__)


class ForecastingAgentService:
    """Single API surface for all forecasting-related data access.

    All methods delegate to existing implementations. Construct with a DB
    session (session-per-call pattern, mirroring ``ForecastEngine(db)``).
    """

    def __init__(self, db: Optional[Session] = None) -> None:
        # ``db`` is optional because read-only market-data endpoints (which
        # query the MySQL warehouse through ``mds``) do not need a session.
        # Compute / weekly-digest paths always receive one from their caller.
        self._db = db
        self._engine: Optional[ForecastEngine] = None

    # ── Lazy engine ────────────────────────────────────────────────────

    @property
    def engine(self) -> ForecastEngine:
        """Lazily construct the heavy ForecastEngine (only when needed)."""
        if self._engine is None:
            self._engine = ForecastEngine(self._db)
        return self._engine

    @property
    def product_forecast_target_key(self) -> dict[str, str]:
        """Centralized access to the product_id → target_key mapping."""
        return mds.PRODUCT_FORECAST_TARGET_KEY

    # ── Compute (write) ────────────────────────────────────────────────

    def discover_products(
        self,
        kb_id: str,
        org_id: str = "default-org",
        app_id: str = "default-app",
        max_tables: int = 50,
    ) -> list[ForecastTarget]:
        """Delegate to ``ForecastEngine.discover_and_register``."""
        return self.engine.discover_and_register(
            kb_id, org_id=org_id, app_id=app_id, max_tables=max_tables
        )

    def run_forecast(
        self,
        target_id: str,
        horizons: Optional[list[int]] = None,
        seasonal_period: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> Optional[ForecastRun]:
        """Delegate to ``ForecastEngine.compute_target``.

        Only forwards the optional args that were explicitly provided so the
        engine's own defaults apply for the rest.
        """
        kwargs: dict[str, Any] = {}
        if horizons is not None:
            kwargs["horizons"] = horizons
        if seasonal_period is not None:
            kwargs["seasonal_period"] = seasonal_period
        if as_of is not None:
            kwargs["as_of"] = as_of
        return self.engine.compute_target(target_id, **kwargs)

    def run_forecast_batch(
        self,
        org_id: str,
        horizons: Optional[list[int]] = None,
        seasonal_period: Optional[int] = None,
    ) -> list[ForecastRun]:
        """Delegate to ``ForecastEngine.compute_all``."""
        kwargs: dict[str, Any] = {}
        if horizons is not None:
            kwargs["horizons"] = horizons
        if seasonal_period is not None:
            kwargs["seasonal_period"] = seasonal_period
        return self.engine.compute_all(org_id, **kwargs)

    # ── Engine-compatible methods (drop-in for ``ForecastEngine`` call-sites) ──

    def discover_and_register(
        self,
        kb_id: str,
        org_id: str = "default-org",
        app_id: str = "default-app",
        max_tables: int = 50,
    ) -> list[ForecastTarget]:
        """Engine-compatible alias of :meth:`discover_products`."""
        return self.discover_products(kb_id, org_id, app_id, max_tables)

    def compute_target_anchored(
        self,
        target_id: str,
        horizons: Optional[list[int]] = None,
        seasonal_period: Optional[int] = None,
        as_of: Optional[datetime] = None,
    ) -> Optional[dict]:
        """Delegate to ``ForecastEngine.compute_target_anchored``.

        Returns the anchored ``{run, anchor_rows, source_table, sample_size}``
        payload (not a bare ``ForecastRun``) — parity with the ``forecast_run``
        tool's target/sku paths.
        """
        kwargs: dict[str, Any] = {}
        if horizons is not None:
            kwargs["horizons"] = horizons
        if seasonal_period is not None:
            kwargs["seasonal_period"] = seasonal_period
        if as_of is not None:
            kwargs["as_of"] = as_of
        return self.engine.compute_target_anchored(target_id, **kwargs)

    def compute_all(
        self,
        org_id: str,
        horizons: Optional[list[int]] = None,
        seasonal_period: Optional[int] = None,
    ) -> list[ForecastRun]:
        """Engine-compatible alias of :meth:`run_forecast_batch`."""
        return self.run_forecast_batch(org_id, horizons, seasonal_period)

    def get_latest_run(self, target_id: str) -> Optional[ForecastRun]:
        """Delegate to ``ForecastEngine.get_latest_run`` (raw ORM row)."""
        return self.engine.get_latest_run(target_id)

    def get_accuracy(self, target_id: str) -> list[dict]:
        """Engine-compatible alias of :meth:`get_forecast_accuracy`."""
        return self.get_forecast_accuracy(target_id)

    # ── Read (cached results) ──────────────────────────────────────────

    def get_forecast(self, target_id: str) -> Optional[dict]:
        """Delegate to ``ForecastEngine.get_forecast``."""
        return self.engine.get_forecast(target_id)

    def get_forecast_batch(self, product_ids: list[str], day: int = 7):
        """Delegate to ``mds.get_forecast_base_batch_v2``."""
        return mds.get_forecast_base_batch_v2(product_ids, day=day, db=self._db)

    def get_forecast_base_batch_v2(self, product_ids: list[str], day: int = 7):
        """Alias of :meth:`get_forecast_batch` (parity with router call-site)."""
        return self.get_forecast_batch(product_ids, day=day)

    def get_forecast_accuracy(self, target_id: str) -> list[dict]:
        """Delegate to ``ForecastEngine.get_accuracy``."""
        return self.engine.get_accuracy(target_id)

    def get_forecast_chart(self, product_id: str, days: int = 30):
        """Delegate to ``mds.get_forecast_chart``."""
        return mds.get_forecast_chart(product_id, days=days, db=self._db)

    # ── Market data (MySQL warehouse) ──────────────────────────────────

    def get_market_snapshot(self, group: Optional[str] = None):
        """Delegate to ``mds.get_market_snapshot``."""
        return mds.get_market_snapshot(group)

    def get_regional_prices(self, product_id: str):
        """Delegate to ``mds.get_regional_prices``."""
        return mds.get_regional_prices(product_id)

    def get_regional_prices_batch(self, product_ids: list[str]):
        """Delegate to ``mds.get_regional_prices_batch``."""
        return mds.get_regional_prices_batch(product_ids)

    def get_price_history(self, product_id: str, days: int = 30):
        """Delegate to ``mds.get_price_history``."""
        return mds.get_price_history(product_id, days=days)

    def read_product_history_rows(self, product_id: str) -> list[tuple]:
        """Delegate to ``mds.read_product_history_rows`` (raw (date, price) tuples)."""
        return mds.read_product_history_rows(product_id)

    def get_news_events(self, group: Optional[str] = None, limit: int = 18):
        """Delegate to ``mds.get_news_events``."""
        return mds.get_news_events(group=group, limit=limit)

    def get_events_latest(
        self,
        hours: int = 168,
        review_status: str = "approved",
        min_usefulness_score: int = 0,
    ):
        """Delegate to ``mds.get_events_latest``."""
        return mds.get_events_latest(
            hours=hours,
            review_status=review_status,
            min_usefulness_score=min_usefulness_score,
        )

    def get_fx_rate(self):
        """Delegate to ``mds.get_fx_rate``."""
        return mds.get_fx_rate()

    def get_price_chain(self):
        """Delegate to ``mds.get_price_chain``."""
        return mds.get_price_chain(db=self._db)

    def invalidate_overview_cache(self) -> int:
        """Delegate to ``mds.invalidate_overview_cache``."""
        return mds.invalidate_overview_cache()

    def is_edia_enabled(self) -> bool:
        """Delegate to ``mds.is_edia_enabled``."""
        return mds.is_edia_enabled()

    def is_edia_reachable_cached(self) -> bool:
        """Delegate to ``mds.is_edia_reachable_cached``."""
        return mds.is_edia_reachable_cached()

    # ── Composite surfaces ─────────────────────────────────────────────

    def get_overview_bundle(self, bypass_cache: bool = False, forecast_day: int = 7):
        """Delegate to ``mds.get_overview_bundle``."""
        return mds.get_overview_bundle(bypass_cache=bypass_cache, forecast_day=forecast_day)

    def get_supply_chain(self, product_id: str):
        """Delegate to ``mds.get_supply_chain``."""
        return mds.get_supply_chain(product_id)

    def get_decision_board(self, group: str, day: int = 7):
        """Delegate to ``decision_board_service.build_decision_board``."""
        from app.services.decision_board_service import build_decision_board

        return build_decision_board(group, day=day, db=self._db)

    def get_forecast_brief(self, product_id: str, day: int = 7) -> Optional[dict]:
        """Delegate to ``analyst.service.get_analyst_brief``."""
        return _analyst.get_analyst_brief(product_id, day=day, db=self._db)

    def get_forecast_analysis(self, product_id: str, day: int = 7):
        """Delegate to ``mds.get_forecast_analysis`` (AI 分析依据 block)."""
        return mds.get_forecast_analysis(product_id, day=day, db=self._db)

    def get_forecast_trust_tier(
        self,
        product_id: str,
        *,
        below_naive: Optional[bool] = None,
        cadence_class: Optional[str] = None,
        mape: Optional[float] = None,
    ) -> dict[str, Any]:
        """Delegate to ``forecast_trust_tier.compute_forecast_trust_tier``."""
        return _trust_tier.compute_forecast_trust_tier(
            product_id,
            below_naive=below_naive,
            cadence_class=cadence_class,
            mape=mape,
        )

    # ── Weekly digest ──────────────────────────────────────────────────

    async def generate_weekly_digest(self, org_id: str = "default-org") -> WeeklyReportCard:
        """Delegate to ``weekly_digest.generator.generate_weekly_digest``."""
        from app.services.weekly_digest.generator import generate_weekly_digest

        return await generate_weekly_digest(self._db, org_id=org_id)

    async def generate_weekly_digest_parallel(
        self, org_id: str = "default-org"
    ) -> WeeklyReportCard:
        """Delegate to ``weekly_digest.generator.generate_weekly_digest_parallel``."""
        from app.services.weekly_digest.generator import generate_weekly_digest_parallel

        return await generate_weekly_digest_parallel(self._db, org_id=org_id)

    def get_weekly_forecast_payloads(self, product_key: str) -> Optional[dict]:
        """Delegate to ``weekly_digest.generator._read_forecast_for_product``.

        Centralizes the weekly-digest forecast read (``ecisco.`` prefix handling,
        terminal ``[-1]`` price extraction, confidence mapping) behind one facade
        entry point so the generator can drop its inline duplicate.
        """
        from app.services.weekly_digest.generator import _read_forecast_for_product

        return _read_forecast_for_product(product_key, self._db)

    def get_weekly_digest_list(
        self,
        org_id: str = "default-org",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Return a page of weekly report cards (newest first)."""
        query = (
            self._db.query(WeeklyReportCard)
            .filter(
                WeeklyReportCard.org_id == org_id,
                WeeklyReportCard.is_deleted == False,  # noqa: E712
            )
            .order_by(WeeklyReportCard.week_start.desc())
        )
        total = query.count()
        items = query.offset((page - 1) * page_size).limit(page_size).all()
        return {"items": items, "total": total, "page": page, "page_size": page_size}

    def get_weekly_digest_detail(
        self, report_id: str, org_id: str = "default-org"
    ) -> Optional[dict]:
        """Return a single report card with its products, or None."""
        card = (
            self._db.query(WeeklyReportCard)
            .filter(
                WeeklyReportCard.id == report_id,
                WeeklyReportCard.org_id == org_id,
                WeeklyReportCard.is_deleted == False,  # noqa: E712
            )
            .first()
        )
        if not card:
            return None
        products = (
            self._db.query(WeeklyReportProduct)
            .filter(
                WeeklyReportProduct.report_id == report_id,
                WeeklyReportProduct.is_deleted == False,  # noqa: E712
            )
            .order_by(WeeklyReportProduct.report_order)
            .all()
        )
        return {"card": card, "products": products}

    def get_weekly_digest_latest(self, org_id: str = "default-org") -> Optional[dict]:
        """Return the most recent ready report with its products, or None."""
        card = (
            self._db.query(WeeklyReportCard)
            .filter(
                WeeklyReportCard.org_id == org_id,
                WeeklyReportCard.is_deleted == False,  # noqa: E712
                WeeklyReportCard.status == "ready",
            )
            .order_by(WeeklyReportCard.week_start.desc())
            .first()
        )
        if not card:
            return None
        products = (
            self._db.query(WeeklyReportProduct)
            .filter(
                WeeklyReportProduct.report_id == card.id,
                WeeklyReportProduct.is_deleted == False,  # noqa: E712
            )
            .order_by(WeeklyReportProduct.report_order)
            .all()
        )
        return {"card": card, "products": products}
