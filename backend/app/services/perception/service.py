"""Ecisco BI Perception Service — Stage 1 market snapshot.

Port of EDIA 5.1.2 backend/src/service/perception_service.py (§4.1).
Adapted: EDIA domain_graph → Zhanlu knowledge_graph, EDIA kb → skip (RAG not wired yet),
EDIA intelligence_event_registry → Zhanlu intelligence services.

Layers:
- Pull latest prices from market_dashboard_service (Phase 4)
- IsolationForest anomaly detection on 90-day price windows
- Compute macro regime (crude/naphtha trend, market phase)
- Compute alert level (NORMAL / WATCH / ALERT)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Single bridge to the EDIA MySQL price warehouse. The perception pipeline must
# ONLY resolve real prices through here (imported lazily to avoid a hard import
# cycle at module load). All other call sites (pricing/tool.py, decision/tool.py)
# reuse PerceptionService rather than re-importing this module.
from app.services import market_dashboard_service as mds  # noqa: E402

# IsolationForest config
_ANOMALY_WINDOW = 90
_CONTAMINATION = 0.05


def _detect_anomalies(price_history: list[dict], window: int = _ANOMALY_WINDOW) -> dict:
    """Run IsolationForest on price history; flag anomalous days."""
    from sklearn.ensemble import IsolationForest

    if len(price_history) < 10:
        return {"is_anomaly": False, "anomaly_score": 0.0, "anomalous_days": []}

    recent = price_history[-window:]
    prices = np.array([h["price"] for h in recent]).reshape(-1, 1)
    pct_changes = (
        np.diff(prices.flatten(), prepend=prices[0, 0])
        / np.maximum(prices.flatten(), 1) * 100
    )
    rolling_std = np.array([
        np.std(prices[max(0, i - 5) : i + 1]) if i >= 1 else 0.0
        for i in range(len(prices))
    ])
    features = np.column_stack([prices.flatten(), pct_changes, rolling_std])

    model = IsolationForest(contamination=_CONTAMINATION, random_state=42, n_estimators=100)
    labels = model.fit_predict(features)
    scores = model.decision_function(features)

    anomalous_days = [recent[i]["date"] for i in range(len(labels)) if labels[i] == -1]
    return {
        "is_anomaly": bool(labels[-1] == -1),
        "anomaly_score": round(float(scores[-1]), 4),
        "anomalous_days": anomalous_days[-5:],
    }


class PerceptionService:
    """Build market snapshot with anomaly detection for all C5/C9 products."""

    def __init__(self, db=None, org_id: str | None = None, llm_caller=None):
        self.db = db
        self.org_id = org_id
        self.llm = llm_caller

    def build_snapshot(self, cutoff_date: str | None = None) -> dict:
        """Build a full perception snapshot for all products.

        Returns:
            {reference_date, products: {id: {latest_price, change_7d_pct, ...}},
             anomalies, alert_level, crude_trend, naphtha_trend, market_phase}
        """
        products: dict[str, Any] = {}
        anomaly_labels: list[str] = []

        # Get all products from knowledge graph
        from app.services.knowledge_graph.graph import build_c5_c9_graph
        graph = build_c5_c9_graph()

        ref_date = cutoff_date or datetime.now().strftime("%Y-%m-%d")

        for node in graph.nodes:
            node_id = node.id
            # Try to get price from market_dashboard_service
            price_info = self._get_product_price(node_id, ref_date)
            if not price_info:
                continue

            history = self._get_price_history(node_id)
            anomaly = _detect_anomalies(history) if history else {
                "is_anomaly": False, "anomaly_score": 0.0, "anomalous_days": [],
            }

            if anomaly.get("is_anomaly"):
                anomaly_labels.append(f"{node_id}_price_anomaly")

            # Naphtha spike check
            if node_id == "naphtha" and abs(price_info.get("change_7d_pct", 0)) > 2.5:
                chg = price_info["change_7d_pct"]
                direction = "spike" if chg > 0 else "drop"
                anomaly_labels.append(f"naphtha_{direction}_{chg:+.1f}%")

            products[node_id] = {
                "id": node_id,
                "name_en": price_info.get("name_en", node_id),
                "name_cn": price_info.get("name_cn", node_id),
                "unit": "元/吨",
                "latest_price": price_info["latest_price"],
                "latest_date": price_info.get("latest_date", ref_date),
                "change_7d_pct": price_info.get("change_7d_pct", 0.0),
                "anomaly": anomaly,
                "data_source": "real" if price_info.get("real_data") else "missing",
            }

        crude_trend, naphtha_trend, market_phase = self._compute_macro_regime(products)
        alert_level = self._compute_alert_level(anomaly_labels, products)

        # Check for recent intelligence events
        intel_events = self._query_recent_intelligence_events(hours=4)
        if intel_events:
            alert_level = "ALERT"

        snapshot = {
            "reference_date": ref_date,
            "products": products,
            "anomalies": anomaly_labels,
            "alert_level": alert_level,
            "market_phase": market_phase,
            "crude_trend": crude_trend,
            "naphtha_trend": naphtha_trend,
        }
        if intel_events:
            snapshot["intelligence_events"] = intel_events
        return snapshot

    @staticmethod
    def _get_product_price(product_id: str, ref_date: str | None = None) -> dict | None:
        """Resolve real latest price + 7-day change for a product.

        Bridges the knowledge graph node to the EDIA MySQL price warehouse via
        ``market_dashboard_service.read_product_history_rows``. The 7-day change
        is computed against the real 7-day-old row (calendar-window based), NOT
        simply the previous-day row, matching ``get_market_snapshot``.

        This is a ``staticmethod`` because the warehouse call opens its own MySQL
        connection and needs no SQLAlchemy session — so callers (perception,
        pricing tool, decision tool) can invoke it without an instance.

        Returns ``None`` when no rows exist so callers skip the product instead
        of persisting a false zero. Any failure is logged at WARNING (not
        swallowed at debug) so a future regression surfaces at default log level.
        """
        try:
            from app.models.market_dashboard import PRODUCT_LABELS

            rows = mds.read_product_history_rows(product_id)
            if not rows:
                logger.warning("No price history for %s — skipping", product_id)
                return None

            # rows are [(date_str, price_float)] oldest→newest.
            last_date, last_price = rows[-1]
            labels = PRODUCT_LABELS.get(product_id, {})
            name_en = labels.get("label_en", product_id)
            name_cn = labels.get("label_zh", product_id)

            # 7-day change: pick the oldest row within the 7-day window before
            # the latest date (calendar-based, robust to gaps / non-daily data).
            from datetime import date as _date, timedelta as _td

            try:
                ref = _date.fromisoformat(last_date)
            except ValueError:
                ref = None

            weekly_ref_price = None
            if ref is not None:
                # 7-day-old reference = oldest row within [ref-7d, ref) window
                # (excludes the latest row itself).
                older = [p for d, p in rows
                         if _date.fromisoformat(d) < ref
                         and _date.fromisoformat(d) >= (ref - _td(days=7))]
                if older:
                    weekly_ref_price = older[0]

            change_7d_pct = 0.0
            if weekly_ref_price:
                try:
                    change_7d_pct = round(
                        (last_price - weekly_ref_price) / weekly_ref_price * 100, 2
                    )
                except ZeroDivisionError:
                    change_7d_pct = 0.0

            return {
                "product_id": product_id,
                "latest_price": last_price,
                "latest_date": str(last_date),
                "change_7d_pct": change_7d_pct,
                "name_en": name_en,
                "name_cn": name_cn,
                "real_data": True,
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not get price for %s: %s", product_id, exc)
            return None

    @staticmethod
    def _get_price_history(product_id: str) -> list[dict]:
        """Get 90-day price history for IsolationForest anomaly detection.

        Delegates to the real ``market_dashboard_service.get_price_history`` so
        the anomaly detector stops being a no-op. Returns [] on any failure so
        anomaly detection simply skips rather than crashing the snapshot.
        """
        try:
            rows = mds.get_price_history(product_id, days=90)
            return [
                {"date": d, "price": p}
                for d, p in rows
            ] if rows else []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not get price history for %s: %s", product_id, exc)
            return []

    def _compute_macro_regime(self, products: dict) -> tuple[str, str, str]:
        """Compute macro signals: crude_trend, naphtha_trend, market_phase."""
        # Check macro override first
        try:
            from app.services.pricing.macro_override import MacroOverrideService
            svc = MacroOverrideService()
            ov = svc.get_active_override()
            if ov:
                return (
                    ov.get("crude_trend", "flat"),
                    ov.get("naphtha_trend", "flat"),
                    ov.get("market_phase", "normal"),
                )
        except Exception:
            pass

        # Derive from quantitative data
        crude = products.get("crude_oil") or products.get("naphtha", {})
        naphtha = products.get("naphtha", {})
        crude_chg = crude.get("change_7d_pct", 0)
        naphtha_chg = naphtha.get("change_7d_pct", 0)

        crude_trend = "up" if crude_chg > 1 else ("down" if crude_chg < -1 else "flat")
        naphtha_trend = "up" if naphtha_chg > 1 else ("down" if naphtha_chg < -1 else "flat")

        month = datetime.now(timezone.utc).month
        if month in (1, 2):
            market_phase = "post_holiday_restart"
        elif month == 12:
            market_phase = "pre_holiday"
        else:
            market_phase = "normal"

        return crude_trend, naphtha_trend, market_phase

    def _compute_alert_level(self, anomaly_labels: list[str], products: dict) -> str:
        naphtha = products.get("naphtha", {})
        naphtha_chg = abs(naphtha.get("change_7d_pct", 0))
        anomaly_count = len(anomaly_labels)
        if anomaly_count >= 3 or naphtha_chg >= 5.0:
            return "ALERT"
        elif anomaly_count >= 1 or naphtha_chg >= 2.5:
            return "WATCH"
        return "NORMAL"

    def _query_recent_intelligence_events(self, hours: int = 4) -> list[dict]:
        """Stub — return empty until intelligence layer is fully wired."""
        return []

    @staticmethod
    def to_prompt_text(snapshot: dict) -> str:
        """Build a compact PERCEPTION block for LLM context."""
        ref = snapshot.get("reference_date", "?")
        alert = snapshot.get("alert_level", "NORMAL")
        crude = snapshot.get("crude_trend", "flat")
        naphtha = snapshot.get("naphtha_trend", "flat")
        phase = snapshot.get("market_phase", "normal")
        lines = [
            f"PERCEPTION (STAGE 1) — {ref}",
            f"Alert: {alert} | Phase: {phase} | Crude: {crude} | Naphtha: {naphtha}",
        ]
        products = snapshot.get("products", {})
        for pid, p in sorted(products.items()):
            anom = "⚠" if p.get("anomaly", {}).get("is_anomaly") else ""
            lines.append(
                f"  {pid:20s} {p.get('latest_price', 0):>7,} 元/吨  "
                f"{p.get('change_7d_pct', 0):+6.1f}%w  {anom}"
            )
        anomalies = snapshot.get("anomalies", [])
        if anomalies:
            lines.append("Anomalies: " + ", ".join(anomalies))
        return "\n".join(lines)


# Convenience
def build_perception_snapshot(db=None) -> dict:
    return PerceptionService(db=db).build_snapshot()
