"""Analyst brief service — cached LLM/template briefs on ForecastRun rows."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from app.models.market_dashboard import PRODUCT_LABELS
from app.services import market_dashboard_service as mds
from app.services.llm_service import chat_completion_json_sync
from app.services.forecasting.analyst.brief_writer import write_brief_llm
from app.services.forecasting.analyst.evidence_pack import UPSTREAM_MAP, build_pack
from app.services.forecasting.analyst.template_brief import render_template_brief

logger = logging.getLogger(__name__)


def _llm_enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "FORECAST_ANALYST_LLM_ENABLED", False))
    except Exception:
        return False


def _latest_run(product_id: str, db):
    from app.models.forecasting import ForecastRun, ForecastTarget
    target_key = mds.PRODUCT_FORECAST_TARGET_KEY.get(product_id)
    if not target_key:
        return None
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


def _build_pack_for(product_id: str, day: int, run) -> dict:
    labels = PRODUCT_LABELS.get(product_id, {})
    upstream_histories = {
        up: mds.read_product_history_rows(up)
        for up in UPSTREAM_MAP.get(product_id, [])
    }
    return build_pack(
        product_id=product_id,
        name_zh=labels.get("label_zh", product_id),
        day=day,
        history_rows=mds.read_product_history_rows(product_id),
        upstream_histories=upstream_histories,
        run_results=run.results or {},
        model_detail=run.model_detail,
        explanation=run.explanation or {},
        as_of_month=datetime.now().month,
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
        if cached:
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
