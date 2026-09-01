"""Deterministic synthesizer — assembles the 6-section Executive Report
payload from the executor's facet results.

Design spec §9 — NO LLM call. This module is pure Python: ranking,
share computation, QoQ delta estimation, concentration metrics, and a
fixed rule table for recommended actions.

The six sections (in this fixed order) are:

    1. Executive Summary            — short narrative built from KPIs
    2. Primary Metric Breakdown     — ranked rows + KPI block
    3. Segment Decomposition        — top-N segments, share ladder
    4. Operational Drivers          — domain-adaptive label, anomalies
    5. Domain-Specific Risk         — domain-adaptive label, risk ladder
    6. Recommended Actions          — fired by rule table

The labels for sections 4 and 5 are drawn from
``domain_labels.DOMAIN_SECTION_LABELS`` — deterministic mapping,
NOT chosen by an LLM.

Failure-mode contract:
    - Any facet returning ``available=False`` is replaced with
      ``rows=[]`` and the section is rendered as "(data unavailable)".
    - ``_validate_enterprise_payload`` rejects payloads where the
      primary metric has fewer than 2 rows OR the executive summary
      is empty, so renderers can short-circuit with a clear error.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.services.enterprise_orchestrator.claim_tracker import (
    ClaimTracker,
    make_claim,
)
from app.services.enterprise_orchestrator.domain_labels import get_section_labels
from app.services.enterprise_orchestrator.title_builder import build_enterprise_title

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recommended-actions rule table (deterministic; NO LLM)
# ---------------------------------------------------------------------------
RECOMMENDED_ACTION_RULES: list[dict] = [
    {
        "facet_id": "inventory_position",
        "condition_key": "days_of_stock",
        "threshold": 5.0,
        "comparison": "<",
        "action": (
            "Restock SKUs with sub-five-day supply before the next "
            "delivery window."
        ),
        "severity": "high",
    },
    {
        "facet_id": "top_customers",
        "condition_key": "top3_share_pct",
        "threshold": 60.0,
        "comparison": ">",
        "action": (
            "Diversify customer concentration: the top-three customers "
            "exceed 60% of revenue and pose single-buyer risk."
        ),
        "severity": "medium",
    },
    {
        "facet_id": "top_orders",
        "condition_key": "avg_days_to_delivery",
        "threshold": 5.0,
        "comparison": "<",
        "action": (
            "Triage orders with <5 days to planned delivery — confirm "
            "shipment readiness with logistics."
        ),
        "severity": "high",
    },
    {
        "facet_id": "sales_summary",
        "condition_key": "mom_decline_pct",
        "threshold": -10.0,
        "comparison": "<",
        "action": (
            "Investigate month-on-month volume decline >10%: review "
            "per-product contribution and pricing."
        ),
        "severity": "medium",
    },
]


# ---------------------------------------------------------------------------
# Pure-helper transforms
# ---------------------------------------------------------------------------
def _rank_rows(rows: list[dict], sort_key: str, descending: bool = True) -> list[dict]:
    """Stable descending/ascending sort by ``sort_key``; missing keys
    fall to the bottom (descending) or top (ascending). Each row gets
    a ``__rank`` field equal to its position (1 = top)."""
    def _k(r: dict) -> float:
        v = r.get(sort_key) if isinstance(r, dict) else None
        if isinstance(v, (int, float)):
            return float(v)
        return float("-inf") if descending else float("inf")

    out = sorted(rows, key=_k, reverse=descending)
    for i, r in enumerate(out):
        if isinstance(r, dict):
            r["__rank"] = i + 1
    return out


def _top_share(rows: list[dict], n: int = 3, key: str = "share_pct") -> float:
    if not rows:
        return 0.0
    sorted_rows = _rank_rows(rows, key, descending=True)
    return float(sum((r.get(key) or 0) for r in sorted_rows[:n]))


def _concentration(rows: list[dict], value_key: str) -> float:
    if not rows:
        return 0.0
    total = float(sum((r.get(value_key) or 0) for r in rows))
    if total <= 0:
        return 0.0
    sorted_rows = _rank_rows(rows, value_key, descending=True)
    top3 = sum((r.get(value_key) or 0) for r in sorted_rows[:3])
    return round(top3 / total * 100, 2)


def _margin_compression(
    primary: dict | None,
    prior: dict | None,
    key: str = "margin_pct",
) -> tuple[float | None, str | None]:
    """Returns (delta_pct_points, segment_label).

    ``delta < 0`` means margin COMPRESSION (primary lower than prior).
    When there's no compression (delta >= 0) or no data, returns
    ``(None, None)`` so callers can treat the check as a no-op.
    """
    if not primary or not prior:
        return None, None
    primary_rows = primary.get("rows") or []
    prior_rows = prior.get("rows") or []
    if not primary_rows or not prior_rows:
        return None, None
    primary_avg = sum(float(r.get(key) or 0) for r in primary_rows) / len(primary_rows)
    prior_avg = sum(float(r.get(key) or 0) for r in prior_rows) / len(prior_rows)
    delta = primary_avg - prior_avg
    if delta >= 0:
        # No compression — expand or flat.
        return None, None
    seg = f"avg {key} {round(primary_avg, 2)} vs prior {round(prior_avg, 2)}"
    return delta, seg


# ---------------------------------------------------------------------------
# Recommended-actions evaluator
# ---------------------------------------------------------------------------
def _evaluate_recommended_actions(facets: dict[str, dict]) -> list[dict]:
    actions: list[dict] = []
    for rule in RECOMMENDED_ACTION_RULES:
        facet = facets.get(rule["facet_id"])
        if not facet or not facet.get("available"):
            continue
        rows = facet.get("rows") or []
        if not rows:
            continue
        if rule["condition_key"] == "top3_share_pct":
            value = _top_share(rows, n=3)
        elif rule["condition_key"] == "avg_days_to_delivery":
            days_vals = [r.get("days_to_delivery") for r in rows
                         if isinstance(r.get("days_to_delivery"), (int, float))]
            value = round(sum(days_vals) / len(days_vals), 2) if days_vals else 999.0
        elif rule["condition_key"] == "mom_decline_pct":
            kpi = facet.get("kpi") if isinstance(facet.get("kpi"), dict) else {}
            value = float(kpi.get("mom_decline_pct", 0) or 0)
        else:
            numeric_vals = [r.get(rule["condition_key"]) for r in rows
                            if isinstance(r.get(rule["condition_key"]), (int, float))]
            if not numeric_vals:
                continue
            value = sum(numeric_vals) / len(numeric_vals)
        if _compare(value, rule["threshold"], rule["comparison"]):
            actions.append({
                "action": rule["action"],
                "severity": rule["severity"],
                "source_facet": rule["facet_id"],
                "metric_value": round(float(value), 2)
                if isinstance(value, (int, float)) else value,
                "threshold": rule["threshold"],
            })
    return actions


def _compare(value: float, threshold: float, op: str) -> bool:
    if op == "<":
        return value < threshold
    if op == ">":
        return value > threshold
    if op == "<=":
        return value <= threshold
    if op == ">=":
        return value >= threshold
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def synthesize_enterprise_report(
    intent: dict,
    facets: dict[str, dict],
    prior_period_facets: dict[str, dict] | None = None,
) -> dict:
    """Assemble the 6-section executive report payload."""
    period = intent.get("period")
    domain = intent.get("domain") or "generic"
    primary_metric = intent.get("primary_metric") or "performance"
    labels = get_section_labels(domain)

    primary = _select_primary(facets, intent)
    primary_rows = primary.get("rows") or []

    segment_facet = _select_segment_facet(facets, intent)
    customer_facet = facets.get("top_customers")
    inventory_facet = facets.get("inventory_position")
    orders_facet = facets.get("top_orders")

    kpi = _build_kpi_block(primary, segment_facet, customer_facet, inventory_facet, orders_facet)
    period_label = _format_period(period)
    title = build_enterprise_title(
        period_label=period_label,
        domain_label=_domain_label_for(domain),
    )
    exec_summary = _build_executive_summary(
        domain=domain, period=period, primary=primary, kpi=kpi,
        customer_facet=customer_facet,
    )

    breakdown_section = {
        "label": "Primary Metric Breakdown",
        "metric": primary_metric,
        "rows": primary_rows,
        "kpi": kpi.get("primary", {}),
        "available": bool(primary_rows),
        "unavailable_reason": primary.get("unavailable_reason") or "",
    }

    segment_section = _build_segment_section(segment_facet, customer_facet)

    drivers_section = _build_drivers_section(
        label=labels.get("drivers_section", "Operational Drivers & Anomalies"),
        primary=primary,
        inventory_facet=inventory_facet,
        orders_facet=orders_facet,
        prior_period_facets=prior_period_facets,
    )

    risk_section = _build_risk_section(
        label=labels.get("risk_section", "Risk Assessment"),
        customer_facet=customer_facet,
        inventory_facet=inventory_facet,
        orders_facet=orders_facet,
    )

    actions = _evaluate_recommended_actions(facets)
    if prior_period_facets:
        prior_primary = next(
            (v for v in prior_period_facets.values() if v.get("available")), None
        )
        if prior_primary:
            delta, seg = _margin_compression(primary, prior_primary)
            if delta is not None and delta < -2.0:
                actions.append({
                    "action": (
                        f"Margin compression of {round(abs(delta), 2)} pts "
                        f"({seg or ''}); review pricing + COGS."
                    ),
                    "severity": "high",
                    "source_facet": "margin_compression",
                    "metric_value": round(delta, 2),
                    "threshold": -2.0,
                })

    payload = {
        "enterprise_report_kind": "executive",
        "title": title,
        "domain": domain,
        "period": list(period) if period else None,
        "period_label": period_label,
        "primary_metric": primary_metric,
        "executive_summary": exec_summary,
        "primary_metric_breakdown": breakdown_section,
        "segment_decomposition": segment_section,
        "operational_drivers": drivers_section,
        "risk_section": risk_section,
        "recommended_actions": actions,
        "data_confidence": {
            "covered_facets": sorted(fid for fid, fr in facets.items() if fr.get("available")),
            "missing_facets": sorted(fid for fid, fr in facets.items() if not fr.get("available")),
            "missing_reasons": {
                fid: (fr.get("unavailable_reason") or "")[:160]
                for fid, fr in facets.items() if not fr.get("available")
            },
        },
        "claims": [],
        "created_at": datetime.utcnow().isoformat() + "Z",
    }

    tracker = ClaimTracker()
    primary_id = _find_primary_id(facets, intent)
    if primary_rows and primary.get("source_sql"):
        tracker.add(make_claim(
            claim_id="primary_kpi",
            text=(
                f"Total volume over the period: "
                f"{kpi.get('primary', {}).get('total_volume_tons', 'N/A')} tons / "
                f"{kpi.get('primary', {}).get('total_revenue', 'N/A')} revenue."
            ),
            source_facet=primary_id,
            source_row_ids=[str(r.get("material_name") or i)
                            for i, r in enumerate(primary_rows[:5])],
            source_sql=primary.get("source_sql") or "",
        ))
    payload["claims"] = list(tracker.claims)
    return payload


def _validate_enterprise_payload(payload: dict) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, "payload is not a dict"
    breakdown = payload.get("primary_metric_breakdown") or {}
    rows = breakdown.get("rows") or []
    if len(rows) < 2:
        return False, "primary_metric_breakdown has fewer than 2 rows"
    if not str(payload.get("executive_summary") or "").strip():
        return False, "executive_summary is empty"
    return True, ""


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------
def _select_primary(facets: dict[str, dict], intent: dict) -> dict:
    for fid, fr in facets.items():
        if fr.get("available") and (fr.get("purpose") == "primary"):
            return fr
    priority = [
        "sales_summary", "sales_summary_for_period",
        "inventory_position", "top_customers", "top_orders",
    ]
    for prio in priority:
        for fid, fr in facets.items():
            if prio in fid and fr.get("available"):
                return fr
    for fid, fr in facets.items():
        if fr.get("available"):
            return fr
    return {
        "rows": [], "kpi": {}, "available": False,
        "unavailable_reason": "no available primary facet",
        "source_sql": "",
    }


def _select_segment_facet(facets: dict[str, dict], intent: dict) -> dict | None:
    for fid, fr in facets.items():
        if "customer" in fid and fr.get("available"):
            return fr
    for fid, fr in facets.items():
        if fr.get("available") and fr.get("rows"):
            return fr
    return None


def _find_primary_id(facets: dict[str, dict], intent: dict) -> str:
    primary = _select_primary(facets, intent)
    for fid, fr in facets.items():
        if fr is primary:
            return fid
    return "primary"


def _build_kpi_block(
    primary: dict,
    segment_facet: dict | None,
    customer_facet: dict | None,
    inventory_facet: dict | None,
    orders_facet: dict | None,
) -> dict:
    primary_kpi = (primary.get("kpi") if isinstance(primary.get("kpi"), dict) else {})
    primary_rows = primary.get("rows") or []
    total_volume = sum(
        float(r.get("total_volume_tons") or 0) for r in primary_rows
    ) if primary_rows else primary_kpi.get("total_volume_tons", 0)
    total_revenue = sum(
        float(r.get("total_revenue") or 0) for r in primary_rows
    ) if primary_rows else primary_kpi.get("total_revenue", 0)
    return {
        "primary": {
            "total_volume_tons": round(float(total_volume or 0), 2),
            "total_revenue": round(float(total_revenue or 0), 2),
            "distinct_materials": (
                len(primary_rows)
                or primary_kpi.get("distinct_materials", 0)
            ),
            "period_days": primary_kpi.get("period_days", 0),
        },
        "customers": {
            "top3_share_pct": round(_top_share((customer_facet or {}).get("rows") or [], n=3), 2),
            "customer_count": (
                (customer_facet or {}).get("kpi", {}).get("customer_count", 0)
                if isinstance((customer_facet or {}).get("kpi"), dict) else 0
            ),
        },
        "inventory": {
            "low_stock_count": (
                (inventory_facet or {}).get("kpi", {}).get("low_stock_count", 0)
                if isinstance((inventory_facet or {}).get("kpi"), dict) else 0
            ),
        },
        "orders": {
            "open_order_count": (
                (orders_facet or {}).get("kpi", {}).get("open_order_count", 0)
                if isinstance((orders_facet or {}).get("kpi"), dict) else 0
            ),
            "total_remaining_qty": (
                (orders_facet or {}).get("kpi", {}).get("total_remaining_qty", 0)
                if isinstance((orders_facet or {}).get("kpi"), dict) else 0
            ),
        },
    }


def _format_period(period: Any) -> str:
    if not period:
        return ""
    if isinstance(period, (list, tuple)) and len(period) == 2:
        return f"{period[0]} to {period[1]}"
    if isinstance(period, str):
        return period
    return ""


def _domain_label_for(domain: str) -> str:
    mapping = {
        "supply_chain": "Supply Chain",
        "financial_performance": "Financial Performance",
        "logistics": "Logistics",
        "risk_management": "Risk Management",
        "sales_operations": "Sales Operations",
        "hr": "HR",
        "procurement": "Procurement",
        "generic": "Operational",
    }
    return mapping.get(domain or "generic", "Operational")


def _build_executive_summary(
    *, domain: str, period: Any, primary: dict,
    kpi: dict, customer_facet: dict | None,
) -> str:
    period_label = _format_period(period) or "the reporting period"
    pkpi = kpi.get("primary", {})
    if not primary.get("available"):
        return (
            f"{_domain_label_for(domain)} report for {period_label}. "
            "Primary data is unavailable; see the section breakdown for "
            "facets that did return."
        )
    bullets = []
    if pkpi.get("total_volume_tons"):
        bullets.append(f"{pkpi['total_volume_tons']:.2f} tons sold")
    if pkpi.get("total_revenue"):
        bullets.append(f"¥{pkpi['total_revenue']:.2f}M revenue")
    elif pkpi.get("total_revenue", 0) == 0:
        bullets.append("no revenue reported for this period")
    if pkpi.get("distinct_materials"):
        bullets.append(f"{int(pkpi['distinct_materials'])} active materials")
    cust_top3 = kpi.get("customers", {}).get("top3_share_pct", 0)
    if cust_top3:
        bullets.append(f"top-3 customer share {cust_top3:.1f}%")
    bullet_text = "; ".join(bullets) if bullets else "no headline metrics"
    return (
        f"{_domain_label_for(domain)} summary for {period_label}: "
        f"{bullet_text}."
    )


def _build_segment_section(segment_facet: dict | None, customer_facet: dict | None) -> dict:
    rows = (segment_facet or customer_facet or {}).get("rows") or []
    return {
        "label": "Segment Decomposition",
        "rows": rows,
        "available": bool(rows),
        "unavailable_reason": (
            (segment_facet or customer_facet or {}).get("unavailable_reason", "")
        ),
        "observations": _segment_observations(rows),
    }


def _segment_observations(rows: list[dict]) -> list[str]:
    obs = []
    if not rows:
        return obs
    sort_key = "total_revenue" if rows[0].get("total_revenue") else list(rows[0].keys())[0]
    sorted_rows = _rank_rows(rows, sort_key)
    if sorted_rows:
        top = sorted_rows[0]
        name = top.get("customer_name") or top.get("material_name") or "(unnamed)"
        rev = top.get("total_revenue") or 0
        vol = top.get("total_volume_tons") or 0
        obs.append(
            f"Top segment: {name} with {vol:.2f} tons / ¥{rev:.2f}M revenue."
        )
    if len(sorted_rows) >= 3:
        top3 = [r.get("customer_name") or r.get("material_name") for r in sorted_rows[:3]]
        obs.append("Top-3 ladder: " + ", ".join(filter(None, top3)) + ".")
    return obs


def _build_drivers_section(
    *, label: str, primary: dict, inventory_facet: dict | None,
    orders_facet: dict | None,
    prior_period_facets: dict[str, dict] | None,
) -> dict:
    drivers = []
    if primary.get("available"):
        drivers.append("Volume + revenue dominated by top 5 materials.")
    inv_rows = (inventory_facet or {}).get("rows") or []
    if inv_rows:
        low = [r for r in inv_rows if isinstance(r.get("days_of_stock"), (int, float))
               and r["days_of_stock"] < 5]
        if low:
            drivers.append(
                f"Inventory: {len(low)} SKUs below 5-day supply — see risk section."
            )
        else:
            drivers.append("Inventory: stock cover adequate for the reporting window.")
    if orders_facet and orders_facet.get("available"):
        rows = orders_facet.get("rows") or []
        if rows:
            urgent = [
                r for r in rows
                if isinstance(r.get("days_to_delivery"), (int, float))
                and r["days_to_delivery"] <= 5
            ]
            drivers.append(
                f"Pipeline: {len(rows)} open orders, {len(urgent)} planned within 5 days."
            )
    if prior_period_facets:
        prior_primary = next(
            (v for v in prior_period_facets.values() if v.get("available")), None
        )
        if prior_primary:
            delta, _ = _margin_compression(primary, prior_primary)
            if delta is not None:
                drivers.append(
                    f"QoQ delta: gross margin {'compressed' if delta < 0 else 'expanded'} "
                    f"by {round(abs(delta), 2)} pts."
                )
    return {
        "label": label,
        "available": any(
            f and f.get("available") for f in (primary, inventory_facet, orders_facet) if f
        ),
        "drivers": drivers,
    }


def _build_risk_section(
    *, label: str, customer_facet: dict | None,
    inventory_facet: dict | None, orders_facet: dict | None,
) -> dict:
    risks = []
    if customer_facet and customer_facet.get("available"):
        rows = customer_facet.get("rows") or []
        top3_share = _top_share(rows, n=3)
        if top3_share >= 60:
            risks.append(
                f"Customer concentration high: top-3 = {round(top3_share, 2)}%."
            )
    if inventory_facet and inventory_facet.get("available"):
        rows = inventory_facet.get("rows") or []
        low = [r for r in rows if isinstance(r.get("days_of_stock"), (int, float))
               and r["days_of_stock"] < 5]
        if low:
            names = ", ".join(
                r.get("material_name") for r in low if r.get("material_name")
            )
            risks.append(
                f"Stock-out risk for {len(low)} SKUs ({names[:120]})."
            )
    if orders_facet and orders_facet.get("available"):
        rows = orders_facet.get("rows") or []
        urgent = [
            r for r in rows
            if isinstance(r.get("days_to_delivery"), (int, float))
            and r["days_to_delivery"] <= 5
        ]
        if urgent:
            risks.append(
                f"Delivery risk: {len(urgent)} orders planned within 5 days."
            )
    return {
        "label": label,
        "available": any(
            f and f.get("available") for f in (customer_facet, inventory_facet, orders_facet) if f
        ),
        "risks": risks,
    }


# ---------------------------------------------------------------------------
# Institutional-grade market profile (2026-08-25). Profile-aware
# additions.
# ---------------------------------------------------------------------------

# 8 mandatory market dimensions from the institutional-grade spec.
MARKET_DIMENSIONS: tuple[str, ...] = (
    "core_metrics", "historical_trends", "cost_structure",
    "supply_side", "demand_side", "macro_context",
    "forward_indicators", "cross_segment_relationships",
)


def _coverage_dimensions_for(
    intent: dict,
    facets: dict[str, dict] | None,
) -> list[str]:
    """Derive the ``coverage_dimensions`` list from the facet results.

    Walks each facet; an available facet with non-empty rows contributes
    its canonical dimension (resolved via the profile's ``facet_to_dim``
    map with a prefix-match fallback for LLM variant facet IDs like
    ``core_metrics_brent``).

    Returns:
        Sorted list of dimension names that have available data. Empty
        when no facets produced rows.
    """
    from app.services.enterprise_orchestrator.profiles import (
        get_profile,
        resolve_dimension,
    )
    profile_name = (intent or {}).get("profile_name") or "enterprise"
    try:
        profile = get_profile(profile_name)
    except Exception:
        return []
    covered: set[str] = set()
    facets = facets or {}
    for fid, fdata in facets.items():
        if not isinstance(fdata, dict):
            continue
        if not fdata.get("available"):
            continue
        rows = fdata.get("rows") or []
        if not rows:
            continue
        dim = resolve_dimension(profile, fid)
        if dim:
            covered.add(dim)
    return sorted(covered)


def synthesize_market_report(
    intent: dict,
    facets: dict[str, dict],
) -> dict:
    """Assemble the institutional-grade market overview payload.

    Schema:
      {
        "enterprise_report_kind": "market_overview",
        "title": ...,
        "period_label": ...,
        "overview_dashboard": { items, sentiment, macro_narrative },
        "executive_summary": { summary, recommendations, risk_alerts },
        "entity_deep_dive": [ {entity_id, snapshot, market_analysis,
                                 supply_analysis, demand_analysis,
                                 short_term_outlook, medium_term_outlook,
                                 ai_decision, forecast_table}, ... ],
        "disclaimer": str,
        "coverage_dimensions": List[str],
      }
    """
    intent = intent or {}
    facets = facets or {}
    period = intent.get("period")
    domain = intent.get("domain") or "market"
    primary_metric = intent.get("primary_metric") or "value"

    # Coverage signal (drives the artifact-coverage gate).
    coverage = _coverage_dimensions_for(intent, facets)

    # Simple entity list — for each non-empty facet with rows, emit an
    # entity stub. Real entity deep-dive content requires LLM synthesis,
    # which happens via the normal flow downstream of this payload.
    entity_deep_dive: list[dict] = []
    seen_entities: set[str] = set()
    for fid, fdata in facets.items():
        rows = (fdata.get("rows") or []) if isinstance(fdata, dict) else []
        if not rows:
            continue
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            entity_id = (
                row.get("entity") or row.get("name") or row.get("id")
                or fid
            )
            if entity_id in seen_entities:
                continue
            seen_entities.add(entity_id)
            entity_deep_dive.append({
                "entity_id": entity_id,
                "snapshot": {
                    "current_value": row.get(primary_metric) or row.get("value"),
                    "primary_metric": primary_metric,
                },
                "facet_id": fid,
            })

    # Overview dashboard (placeholder — fills with counts / sentiment).
    items_covered = sum(
        1
        for fdata in facets.values()
        if isinstance(fdata, dict) and fdata.get("rows")
    )
    active = sum(
        1
        for row in (e for entity in entity_deep_dive for e in [entity])
        if isinstance(row, dict)
    )

    payload = {
        "enterprise_report_kind": "market_overview",
        "title": f"{domain.title()} Market Overview",
        "period_label": (
            _format_period(period) if period else "current period"
        ),
        "overview_dashboard": {
            "items_covered": items_covered,
            "active_count": active,
            "inactive_count": max(0, items_covered - active),
            "sentiment": {"positive": 0, "negative": 0, "neutral": items_covered},
            "macro_narrative": (
                "The dominant driver across the 8 institutional-grade "
                "dimensions is the prevailing supply-demand balance for "
                f"{domain}; sentiment distribution depends on which "
                "dimensions have available data."
            ),
        },
        "executive_summary": {
            "summary": (
                f"Across {items_covered} entities and {len(coverage)} of 8 "
                "institutional-grade dimensions, the analytical picture "
                "will be sharpened by the LLM synthesis step that follows "
                "this payload."
            ),
            "recommendations": [],
            "risk_alerts": [],
        },
        "entity_deep_dive": entity_deep_dive,
        "disclaimer": (
            "AI-generated, for reference only, not investment or "
            "business advice."
        ),
        "coverage_dimensions": coverage,
    }
    return payload
