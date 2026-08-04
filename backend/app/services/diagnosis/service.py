"""Ecisco BI Diagnosis Service — Stage 2 causal chain analysis.

Port of EDIA 5.1.2 backend/src/service/diagnosis_service.py (§5).
Adapted: EDIA domain_graph → Zhanlu knowledge_graph, EDIA kb → skip (no RAG yet),
EDIA erp_kpi_service → stub, EDIA intelligence → stub.

Core logic preserved:
- Multi-hop upstream/downstream traversal on knowledge graph
- Driver ranking by 7d change + confidence scoring
- Market regime classification with numeric confidence
- Seasonal demand status (§5.2)
- Cross-product signal propagation (§5.3)
- Crude-naphtha divergence detection (§5.4)
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import logging

logger = logging.getLogger(__name__)

# ── Cross-product dependency graph (§5.3) ─────────────────────
PRODUCT_DEPENDENCY_GRAPH: dict[str, list[str]] = {
    "sis": ["isoprene"],
    "c5_resin": ["piperylene", "cracked_c5"],
    "dcpd": ["cracked_c5"],
    "blowing_agent": ["cracked_c5", "raffinate_c5"],
}

DOWNSTREAM_BULLISH_SIGNALS: list[re.Pattern] = [
    re.compile(r"SIS operating rate.*(6[0-9]|7[0-9])%", re.I),
    re.compile(r"hardener.*(?:production|plant).*restart", re.I),
    re.compile(r"C5 resin.*production.*support", re.I),
    re.compile(r"downstream.*(?:restart|resume)", re.I),
    re.compile(r"post.?holiday.*demand.*recover", re.I),
    re.compile(r"biased upward", re.I),
    re.compile(r"需求.*恢复|开工率.*回升|下游.*复工", re.I),
]

# ── Seasonal phases (§5.2) ────────────────────────────────────
PRODUCT_SEASONAL_PHASES: dict[str, dict[int, str]] = {
    "c5_resin": {1: "off_season", 2: "off_season", 3: "peak_for_infra", 4: "peak_for_infra",
                 5: "peak_for_infra", 6: "peak_for_infra", 7: "peak_for_infra", 8: "peak_for_infra",
                 9: "peak_for_infra", 11: "off_season", 12: "off_season"},
    "dcpd": {1: "off_season", 2: "off_season", 3: "peak", 4: "peak", 5: "peak",
             6: "peak", 7: "peak", 8: "peak", 11: "off_season", 12: "off_season"},
    "sis": {11: "off_season", 12: "off_season", 1: "off_season", 2: "off_season",
            3: "off_season", 9: "peak", 10: "peak"},
    "cracked_c5": {1: "post_holiday_lull", 2: "post_holiday_lull", 3: "post_holiday_restart",
                   4: "post_holiday_restart", 5: "post_holiday_restart"},
    "mixed_c5": {1: "post_holiday_lull", 2: "post_holiday_lull", 3: "post_holiday_restart",
                 4: "post_holiday_restart", 5: "post_holiday_restart"},
    "isoprene": {2: "sis_restart", 3: "sis_restart"},
}

_DEMAND_OVERRIDE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"demand recover|需求回暖|需求恢复|demand improving", re.I), "recovering"),
    (re.compile(r"inventory build.?up|库存累积|inventory pressure|库存压力", re.I), "weak_with_inventory_pressure"),
    (re.compile(r"biased upward|偏强", re.I), "improving"),
    (re.compile(r"demand weak|需求偏弱|demand slow", re.I), "weak"),
]

# ── Confidence helpers ────────────────────────────────────────

def _regime_confidence(change_7d_pct: float) -> float:
    abs_chg = abs(change_7d_pct)
    if abs_chg >= 5.0:
        return 1.0
    if abs_chg < 1.0:
        return 0.2
    return round(0.2 + (abs_chg - 1.0) * 0.2, 3)


def _driver_confidence(driver_change: float, focal_change: float, edge_weight: float = 1.0) -> float:
    if focal_change == 0:
        return 0.3
    alignment = 1.0 if (driver_change > 0 and focal_change > 0) or (driver_change < 0 and focal_change < 0) else 0.4
    magnitude = min(abs(driver_change) / 5.0, 1.0)
    return round(min(alignment * magnitude * edge_weight, 1.0), 3)


def _anomaly_boost(anomaly_data: dict) -> float:
    return 0.15 if anomaly_data.get("is_anomaly") else 0.0


def compute_demand_status(product: str, current_month: int, rag_text: str = "") -> dict:
    """Seasonal demand phase + RAG-based override (§5.2)."""
    base_phase = PRODUCT_SEASONAL_PHASES.get(product, {}).get(current_month, "normal")
    demand_override = None
    note = ""
    for pattern, label in _DEMAND_OVERRIDE_PATTERNS:
        if pattern.search(rag_text):
            demand_override = label
            break
    if base_phase == "peak_for_infra" and demand_override is None:
        note = "Pre-construction season. Hold price; do not over-discount."
    if not note:
        effective = demand_override or base_phase
        if effective == "recovering":
            note = "Demand recovering — hold or raise price."
        elif effective == "weak_with_inventory_pressure":
            note = "Weak demand + inventory pressure — consider accelerated shipments."
        elif effective == "off_season":
            note = "Off-season phase — standard seasonal discount may apply."
        elif effective == "improving":
            note = "Demand improving (biased upward) — lean toward higher range."
    return {
        "seasonal_phase": base_phase,
        "demand_override": demand_override,
        "effective_demand_status": demand_override or base_phase,
        "pricing_note": note,
    }


class DiagnosisService:
    """Stage 2 — causal chain traversal using Zhanlu's C5/C9 knowledge graph.

    Given a focal product + perception snapshot:
    - Multi-hop upstream/downstream traversal
    - Driver ranking with confidence scores
    - Market regime + net signal
    - Seasonal demand status
    - Crude-naphtha divergence detection
    """

    def __init__(self, db=None, org_id: str | None = None, llm_caller=None):
        self.db = db
        self.org_id = org_id
        self.llm = llm_caller

    def diagnose(
        self,
        product_id: str,
        perception_snapshot: dict,
        intelligence_context: dict | None = None,
    ) -> dict:
        """Run full diagnosis for a focal product."""
        from app.services.knowledge_graph.graph import build_c5_c9_graph
        graph = build_c5_c9_graph()
        products_data = perception_snapshot.get("products", {})

        def snap(pid: str) -> dict | None:
            return products_data.get(pid)

        # ── Multi-hop upstream / downstream (manual BFS, max 3 hops) ──
        upstream = self._multi_hop_traverse(graph, product_id, direction="up", max_hops=3)
        downstream = self._multi_hop_traverse(graph, product_id, direction="down", max_hops=3)

        focal = snap(product_id)
        focal_change = focal.get("change_7d_pct", 0) if focal else 0
        focal_anomaly = focal.get("anomaly", {}) if focal else {}

        # Hard fallback when focal product missing
        if not focal:
            return self._no_data_result(product_id)

        # ── Driver ranking ─────────────────────────────────────
        related: list[dict] = []
        for rel in upstream:
            s = snap(rel["id"])
            if s:
                conf = _driver_confidence(s.get("change_7d_pct", 0), focal_change, rel.get("edge_weight", 1))
                conf += _anomaly_boost(s.get("anomaly", {}))
                related.append({**s, "_role": "upstream", "_hop": rel["hop"], "_confidence": min(conf, 1.0)})
        for rel in downstream:
            s = snap(rel["id"])
            if s:
                conf = _driver_confidence(s.get("change_7d_pct", 0), focal_change, rel.get("edge_weight", 1))
                conf += _anomaly_boost(s.get("anomaly", {}))
                related.append({**s, "_role": "downstream", "_hop": rel["hop"], "_confidence": min(conf, 1.0)})

        related_sorted = sorted(related, key=lambda x: abs(x.get("change_7d_pct", 0)), reverse=True)
        primary = related_sorted[0] if related_sorted else None
        secondary = related_sorted[1:3]

        # ── Market regime ──────────────────────────────────────
        regime = "neutral"
        regime_conf = 0.2
        if focal_change >= 2:
            regime = "bullish"
        elif focal_change <= -2:
            regime = "bearish"
        regime_conf = round(min(_regime_confidence(focal_change) + _anomaly_boost(focal_anomaly), 1.0), 3)

        # ── Chain impact text ──────────────────────────────────
        focal_name = focal.get("name_en", product_id)
        chain_impact: list[str] = []
        for rel in upstream:
            s = snap(rel["id"])
            if not s:
                continue
            direction = "rising" if s.get("change_7d_pct", 0) > 0 else "falling"
            cost_dir = "pressure ▲" if direction == "rising" else "relief ▼"
            hop = f" (hop {rel['hop']})" if rel["hop"] > 1 else ""
            chain_impact.append(
                f"{s['name_en']} {direction} ({s['change_7d_pct']:+.1f}%w){hop} → {focal_name} input cost {cost_dir}"
            )

        direct_downstream = graph.downstream_of(product_id)
        for rel in direct_downstream:
            s = snap(rel.id)
            if not s:
                continue
            chain_impact.append(
                f"{focal_name} {'rising' if focal_change > 0 else 'falling'} → "
                f"{s['name_en']} demand {'softer ▼' if focal_change > 0 else 'firmer ▲'}"
            )

        # ── Evidence sources ───────────────────────────────────
        evidence_sources: list[dict] = []
        for r in related_sorted[:6]:
            evidence_sources.append({
                "text": f"{r['name_en']} ({r['_role']}) {r['change_7d_pct']:+.1f}%w",
                "score": round(r["_confidence"], 3),
            })

        # ── Net signal ─────────────────────────────────────────
        net_signal = "neutral"
        if regime == "bullish" and primary and primary.get("_confidence", 0) >= 0.5:
            net_signal = "bullish"
        elif regime == "bearish" and primary and primary.get("_confidence", 0) >= 0.5:
            net_signal = "bearish"

        # ── Seasonal demand status ─────────────────────────────
        current_month = datetime.now(timezone.utc).month
        rag_text = " ".join(ev.get("text", "") for ev in evidence_sources)
        demand_status = compute_demand_status(product_id, current_month, rag_text)

        # ── Crude-naphtha divergence ───────────────────────────
        divergence = self._detect_divergence(products_data)

        if divergence:
            chain_impact.insert(0, (
                f"Divergence: crude {divergence['crude_7d']:+.1f}%w vs "
                f"naphtha {divergence['naphtha_7d']:+.1f}%w (gap {divergence['gap']:+.1f}%)"
            ))
            evidence_sources.insert(0, {
                "text": divergence.get("explanation_hint", ""), "score": 0.78,
            })

        # ── Downstream signal propagation ──────────────────────
        downstream_signals = self._propagate_downstream(product_id)

        return {
            "focal_product": product_id,
            "market_regime": regime,
            "regime_confidence": regime_conf,
            "primary_driver": (
                {
                    "id": primary["id"],
                    "name_en": primary.get("name_en", ""),
                    "role": primary["_role"],
                    "change_7d_pct": primary.get("change_7d_pct", 0),
                    "confidence": primary["_confidence"],
                    "hop": primary.get("_hop", 1),
                    "hypothesis": f"{primary['_role']}_driver",
                    "hypothesis_confidence": primary["_confidence"],
                }
                if primary else None
            ),
            "secondary_drivers": [
                {
                    "id": d["id"], "name_en": d.get("name_en", ""),
                    "role": d["_role"], "change_7d_pct": d.get("change_7d_pct", 0),
                    "confidence": d["_confidence"], "hop": d.get("_hop", 1),
                }
                for d in secondary
            ],
            "chain_impact": chain_impact,
            "evidence_sources": evidence_sources,
            "contradicting_factors": [],
            "downstream_propagated_signals": downstream_signals,
            "net_signal": net_signal,
            "demand_status": demand_status,
            "intelligence_signal": {"active": False, "bias": "neutral", "confidence": 0.0,
                                      "net_price_pct": 0.0, "net_price_cny": 0.0},
            "erp_kpi_signal": {"active": False, "bias": "neutral",
                                 "adjustment_pct": {"3d": 0.0, "7d": 0.0, "30d": 0.0}, "summary": ""},
            "geopolitical_driver": None,
            "divergence_detected": divergence,
            "scored_hypotheses": [],
        }

    def _no_data_result(self, product_id: str) -> dict:
        return {
            "focal_product": product_id,
            "market_regime": "neutral", "regime_confidence": 0.0,
            "primary_driver": None, "secondary_drivers": [],
            "chain_impact": [], "evidence_sources": [],
            "contradicting_factors": [], "downstream_propagated_signals": {},
            "net_signal": "neutral",
            "demand_status": {"seasonal_phase": "unknown", "demand_override": None,
                               "effective_demand_status": "unknown", "pricing_note": ""},
            "intelligence_signal": {"active": False, "bias": "neutral", "confidence": 0.0,
                                      "net_price_pct": 0.0, "net_price_cny": 0.0},
            "erp_kpi_signal": {"active": False, "bias": "neutral",
                                 "adjustment_pct": {"3d": 0.0, "7d": 0.0, "30d": 0.0}, "summary": ""},
            "geopolitical_driver": None, "divergence_detected": None,
            "scored_hypotheses": [], "status": "insufficient_data",
        }

    def _detect_divergence(self, products_data: dict) -> dict | None:
        crude_7d = float((products_data.get("crude_oil") or {}).get("change_7d_pct", 0) or 0)
        naphtha_7d = float((products_data.get("naphtha") or {}).get("change_7d_pct", 0) or 0)
        gap = crude_7d - naphtha_7d
        if abs(crude_7d) <= 1.5 or abs(gap) <= 2.0:
            return None
        return {
            "crude_7d": round(crude_7d, 2), "naphtha_7d": round(naphtha_7d, 2),
            "gap": round(gap, 2),
            "explanation_hint": "Naphtha supply ample, olefin margins weak, crude rise not fully transmitted",
        }

    def _multi_hop_traverse(self, graph: Any, focal_id: str, direction: str, max_hops: int = 3) -> list[dict]:
        """BFS traversal of knowledge graph up to max_hops.

        Args:
            graph: KnowledgeGraph instance
            focal_id: starting product ID
            direction: "up" (upstream) or "down" (downstream)
            max_hops: max BFS depth

        Returns:
            [{id, hop, edge_weight, relationship_type}, ...]
        """
        result: list[dict] = []
        visited: set[str] = {focal_id}
        frontier: set[str] = {focal_id}

        for hop in range(1, max_hops + 1):
            next_frontier: set[str] = set()
            for node_id in frontier:
                if direction == "up":
                    neighbors = graph.upstream_of(node_id)
                else:
                    neighbors = graph.downstream_of(node_id)
                for nb in neighbors:
                    if nb.id not in visited:
                        visited.add(nb.id)
                        next_frontier.add(nb.id)
                        # Find edge weight from graph
                        weight = 1.0
                        for e in graph.edges:
                            if direction == "up" and e.target_id == node_id and e.source_id == nb.id:
                                weight = getattr(e, "weight", 1.0)
                                break
                            if direction == "down" and e.source_id == node_id and e.target_id == nb.id:
                                weight = getattr(e, "weight", 1.0)
                                break
                        result.append({"id": nb.id, "hop": hop, "edge_weight": weight})
            frontier = next_frontier
            if not frontier:
                break
        return result

    def _propagate_downstream(self, focal_id: str) -> dict[str, str]:
        signals: dict[str, str] = {}
        for downstream, upstreams in PRODUCT_DEPENDENCY_GRAPH.items():
            if focal_id in upstreams:
                signals[downstream] = "structural_dependency"
        return signals

    @staticmethod
    def to_prompt_text(diagnosis: dict) -> str:
        lines = [
            f"DIAGNOSIS (STAGE 2) — {diagnosis.get('focal_product', '?')}:",
            f"  Regime: {diagnosis.get('market_regime', 'neutral').upper()} "
            f"(conf: {diagnosis.get('regime_confidence', '?')})",
            f"  Net Signal: {diagnosis.get('net_signal', 'neutral').upper()}",
        ]
        pd = diagnosis.get("primary_driver")
        if pd:
            lines.append(
                f"  Primary Driver: {pd.get('name_en', '?')} ({pd.get('role', '?')}, "
                f"hop {pd.get('hop', 1)}, {pd.get('change_7d_pct', 0):+.1f}%w, "
                f"conf: {pd.get('confidence', '?')})"
            )
        for sd in diagnosis.get("secondary_drivers", []):
            lines.append(
                f"  Secondary: {sd.get('name_en', '?')} ({sd.get('role', '?')}, "
                f"{sd.get('change_7d_pct', 0):+.1f}%w)"
            )
        if diagnosis.get("chain_impact"):
            lines.append("  Chain Impact:")
            for ci in diagnosis["chain_impact"]:
                lines.append(f"    • {ci}")
        ds = diagnosis.get("demand_status", {})
        if ds.get("pricing_note"):
            lines.append(f"  Demand: {ds.get('pricing_note')}")
        div = diagnosis.get("divergence_detected") or {}
        if div:
            lines.append(f"  Divergence: crude {div.get('crude_7d', 0):+.1f}%w vs "
                         f"naphtha {div.get('naphtha_7d', 0):+.1f}%w (gap {div.get('gap', 0):+.1f}%)")
        sigs = diagnosis.get("downstream_propagated_signals", {})
        if sigs:
            lines.append("  Downstream Signals: " + ", ".join(f"{k}:{v}" for k, v in sigs.items()))
        return "\n".join(lines)
