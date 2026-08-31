"""Analyst brief service — cached LLM/template briefs on ForecastRun rows."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from app.config import settings
from app.services.llm_service import chat_completion_json_sync
from app.services.forecasting.analyst.brief_writer import write_brief_llm
from app.services.forecasting.analyst.evidence_pack import UPSTREAM_MAP, build_pack
from app.services.forecasting.analyst.template_brief import render_template_brief
from app.services.forecasting.features.operating_signal import (
    compute_operating_signal,
)
from app.services.forecasting.features.inventory_signal import (
    compute_inventory_signal,
)
from app.services.forecasting.features.import_parity_signal import (
    compute_import_parity_signal,
)
from app.services.forecasting.features.exogenous_loaders import (
    OperatingRateLoader, InventoryLoader, ImportPriceLoader,
)

logger = logging.getLogger(__name__)


def _target_key_for(product_id: str) -> str:
    """Map a dashboard-style product_id to its ForecastTarget key.

    The key namespace comes from the app's domain config
    (``forecast_key_prefix``, default "" — plain product_id). Apps
    without a domain config use no prefix.
    """
    from app.services.domain_config import get_domain_config

    prefix = get_domain_config("").get("forecast_key_prefix", "")
    return f"{prefix}{product_id.strip().lower()}"


def _llm_enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "FORECAST_ANALYST_LLM_ENABLED", False))
    except Exception:
        return False


def _latest_run(product_id: str, db):
    from app.models.forecasting import ForecastRun, ForecastTarget
    product_id = product_id.strip().lower()
    target_key = _target_key_for(product_id)
    tgt = db.query(ForecastTarget).filter(
        ForecastTarget.product_key == target_key,
        ForecastTarget.org_id == "default-org",
        ForecastTarget.is_deleted == False,  # noqa: E712
    ).first()
    if tgt is None:
        return None
    return db.query(ForecastRun).filter(
        ForecastRun.target_id == tgt.id,
        ForecastRun.is_deleted == False,  # noqa: E712
    ).order_by(ForecastRun.created_date.desc()).first()


def _read_history_rows(product_id: str) -> list:
    """Read price history rows [(date_str, price_float)] for a product.

    Uses the forecasting-native external data source (same MySQL mirror the
    engine reads), returning [] when the mirror is unreachable or the
    product has no target.
    """
    from app.services.forecasting.mysql_data_source import MysqlDataSource
    from app.models.forecasting import ForecastTarget
    from app.database import SessionLocal

    product_id = product_id.strip().lower()
    target_key = _target_key_for(product_id)
    db = SessionLocal()
    try:
        tgt = db.query(ForecastTarget).filter(
            ForecastTarget.product_key == target_key,
            ForecastTarget.org_id == "default-org",
            ForecastTarget.is_deleted == False,  # noqa: E712
        ).first()
        if tgt is None or not tgt.datasource:
            return []
        df = MysqlDataSource().read_history(tgt.datasource or {})
        return [
            (str(row["ds"]), float(row["y"]))
            for _, row in df.iterrows()
        ]
    except Exception as exc:
        logger.warning("[analyst] history read failed for %s: %s", product_id, exc)
        return []
    finally:
        db.close()


def _build_pack_for(product_id: str, day: int, run) -> dict:
    product_id = product_id.strip().lower()
    labels: dict = {}
    upstream_histories = {
        up: _read_history_rows(up)
        for up in UPSTREAM_MAP.get(product_id, [])
    }
    history_rows = _read_history_rows(product_id)

    # Wave 1: demand signal + supplier ladder (flag-gated, default OFF)
    demand_signal: dict | None = None
    supplier_ladder: dict | None = None
    if settings.FORECAST_DEMAND_SIGNAL_ENABLED:
        try:
            from app.services.forecasting.features.exogenous_loaders import (
                ErpVolumeLoader, SupplierDispersionLoader,
            )
            from app.services.forecasting.features.demand_signal import (
                compute_demand_signal, compute_supplier_ladder_signal,
            )

            # Load ERP volume and compute demand signal
            vol_loader = ErpVolumeLoader(product_id=product_id, lookback_days=365)
            volume_df = vol_loader.load()
            if not volume_df.empty and len(volume_df) >= 28:
                # Build price DataFrame from history_rows [(date_str, price), ...]
                if history_rows and len(history_rows) >= 28:
                    price_df = pd.DataFrame(
                        [{"date": d, "price": float(v)} for d, v in history_rows if v is not None]
                    )
                    ds = compute_demand_signal(
                        volume_df, price_df=price_df,
                        product_id=product_id, rolling_window=28, yoy_window=364,
                    )
                    if ds and ds.has_sufficient_data:
                        demand_signal = {
                            "rolling_4wk_vol": ds.rolling_4wk_vol,
                            "yoy_change_pct": ds.yoy_change_pct,
                            "vol_price_divergence": ds.vol_price_divergence,
                            "demand_trend": ds.demand_trend,
                            "recent_vol": ds.recent_vol,
                            "vol_momentum_4wk": ds.vol_momentum_4wk,
                            "has_sufficient_data": True,
                        }

            # Load supplier dispersion and compute ladder signal
            disp_loader = SupplierDispersionLoader(
                product_id=product_id, lookback_days=90,
            )
            dispersion_df = disp_loader.load()
            if not dispersion_df.empty:
                ladder = compute_supplier_ladder_signal(
                    dispersion_df, product_id=product_id, recent_days=30,
                )
                if ladder and ladder.get("has_data"):
                    supplier_ladder = ladder
        except Exception as exc:
            logger.warning("[analyst] demand-signal load failed for %s: %s",
                           product_id, exc)

    # Wave 3 T3.5: external-feed signals (operating rate / inventory /
    # import parity) — flag-gated, default OFF.
    downstream_utilization: dict | None = None
    inventory_pressure: dict | None = None
    import_pressure: dict | None = None
    if settings.FORECAST_EXTERNAL_SIGNAL_ENABLED:
        try:
            # Build a price_df from history_rows for divergence computations
            price_df = None
            if history_rows and len(history_rows) >= 28:
                price_df = pd.DataFrame(
                    [{"date": d, "price": float(v)}
                     for d, v in history_rows if v is not None]
                )

            # Operating rate
            op_df = OperatingRateLoader(
                product_id=product_id, lookback_days=365,
            ).load()
            if not op_df.empty:
                op_sig = compute_operating_signal(
                    op_df, price_df=price_df, product_id=product_id,
                )
                if op_sig.has_sufficient_data:
                    downstream_utilization = {
                        "rolling_4wk_op_rate": op_sig.rolling_4wk_op_rate,
                        "yoy_change_pct": op_sig.yoy_change_pct,
                        "op_rate_vs_price_divergence":
                            op_sig.op_rate_vs_price_divergence,
                        "utilization_regime": op_sig.utilization_regime,
                        "has_sufficient_data": True,
                    }

            # Inventory
            inv_df = InventoryLoader(
                product_id=product_id, lookback_days=365,
            ).load()
            if not inv_df.empty:
                inv_sig = compute_inventory_signal(
                    inv_df, price_df=price_df, product_id=product_id,
                )
                if inv_sig.has_sufficient_data:
                    inventory_pressure = {
                        "inventory_4wk_change_pct":
                            inv_sig.inventory_4wk_change_pct,
                        "inventory_vs_price_divergence":
                            inv_sig.inventory_vs_price_divergence,
                        "days_of_supply": inv_sig.days_of_supply,
                        "inventory_pressure": inv_sig.inventory_pressure,
                        "has_sufficient_data": True,
                    }

            # Import parity
            ip_df = ImportPriceLoader(
                product_id=product_id, lookback_days=365,
            ).load()
            if not ip_df.empty:
                ip_sig = compute_import_parity_signal(
                    ip_df, domestic_price_df=price_df,
                    product_id=product_id,
                )
                if ip_sig.has_sufficient_data:
                    import_pressure = {
                        "import_parity_gap": ip_sig.import_parity_gap,
                        "import_window_open": ip_sig.import_window_open,
                        "ceiling_pressure": ip_sig.ceiling_pressure,
                        "has_sufficient_data": True,
                    }
        except Exception as exc:
            logger.warning(
                "[analyst] external-signal load failed for %s: %s",
                product_id, exc,
            )

    return build_pack(
        product_id=product_id,
        name_zh=labels.get("label_zh", product_id),
        day=day,
        history_rows=history_rows,
        upstream_histories=upstream_histories,
        run_results=run.results or {},
        model_detail=run.model_detail,
        explanation=run.explanation or {},
        as_of_month=datetime.now().month,
        demand_signal=demand_signal,
        supplier_ladder=supplier_ladder,
        downstream_utilization=downstream_utilization,
        inventory_pressure=inventory_pressure,
        import_pressure=import_pressure,
    )


def get_analyst_brief(product_id: str, day: int = 7, db=None,
                      persist: bool = True) -> Optional[dict]:
    owns_session = db is None
    if owns_session:
        from app.database import SessionLocal
        db = SessionLocal()
    try:
        run = _latest_run(product_id, db)
        if run is None or not run.results:
            return None
        cached = ((run.explanation or {}).get("analyst_brief") or {}).get(str(day))
        # Skip stale caches from the old 5-field schema (pre-restructure).
        if cached and "market_update_zh" in cached:
            return cached

        pack = _build_pack_for(product_id, day, run)
        brief = render_template_brief(pack)
        if _llm_enabled():
            llm_brief = write_brief_llm(pack, chat_completion_json_sync)
            if llm_brief is not None:
                brief = llm_brief

        explanation = dict(run.explanation or {})
        briefs = dict(explanation.get("analyst_brief") or {})
        briefs[str(day)] = brief
        explanation["analyst_brief"] = briefs
        run.explanation = explanation
        if persist:
            try:
                db.commit()
            except Exception as exc:
                logger.warning("[analyst] brief persist failed: %s", exc)
                db.rollback()
        return brief
    except Exception as exc:
        logger.warning("[analyst] get_analyst_brief failed for %s: %s", product_id, exc)
        return None
    finally:
        if owns_session:
            db.close()


def prewarm_brief(product_id: str, day: int, db) -> None:
    """Engine-time pre-warm: build + attach brief in-session (caller commits)."""
    get_analyst_brief(product_id, day=day, db=db, persist=False)
