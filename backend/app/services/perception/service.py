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

    def _get_product_price(self, product_id: str, ref_date: str) -> dict | None:
        """Get latest price for a product from market_dashboard_service."""
        if not self.db:
            return None
        try:
            from app.services.market_dashboard.service import MarketDashboardService
            svc = MarketDashboardService(self.db)
            # Try to get product snapshot
            snap = getattr(svc, 'get_product_snapshot', None)
            if snap:
                result = snap(product_id)
                if result:
                    return {
                        "latest_price": result.get("price", 0),
                        "latest_date": result.get("date", ref_date),
                        "change_7d_pct": result.get("change_7d_pct", 0),
                        "name_en": result.get("name_en", product_id),
                        "name_cn": result.get("name_cn", product_id),
                        "real_data": True,
                    }
            # Fallback: use build_snapshot and extract
            full = svc.build_snapshot()
            if full and product_id in full:
                entry = full[product_id]
                return {
                    "latest_price": entry.get("latest_price", entry.get("price", 0)),
                    "latest_date": str(entry.get("latest_date", ref_date)),
                    "change_7d_pct": entry.get("change_7d_pct", 0),
                    "name_en": entry.get("name_en", product_id),
                    "name_cn": entry.get("name_cn", product_id),
                    "real_data": True,
                }
        except Exception as exc:
            logger.debug("Could not get price for %s: %s", product_id, exc)
        return None

    def _get_price_history(self, product_id: str) -> list[dict]:
        """Get 90-day price history for anomaly detection. Stub — returns empty."""
        # TODO: Wire to real historical price endpoint
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
