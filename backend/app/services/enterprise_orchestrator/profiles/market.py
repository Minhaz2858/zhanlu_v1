"""``market`` profile — institutional-grade market overview PPT shape.

Triggered when a DB-bound agent receives a market-overview / weekly
digest / trend-report request. Activates the user's 8 mandatory data
dimensions:

    core_metrics, historical_trends, cost_structure, supply_side,
    demand_side, macro_context, forward_indicators, cross_segment_relationships

Each dimension maps from a small set of canonical facet_id prefixes
that the profiler is steered to emit. The synthesizer emits
``coverage_dimensions`` listing the dimensions actually covered — the
artifact-coverage gate in ``artifact_tool.py`` enforces a minimum
size of this list.

Profile name: ``"market"``.
Gate flag:    ``settings.COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED``.
"""
from __future__ import annotations

from typing import Mapping

from . import Profile


# ---------------------------------------------------------------------------
# 8 mandatory dimensions (from the user's institutional-grade spec).
# Keep ORDER in sync with section_schema — the synthesizer renders the
# report in this order.
# ---------------------------------------------------------------------------
MARKET_DIMENSIONS: tuple[str, ...] = (
    "core_metrics",
    "historical_trends",
    "cost_structure",
    "supply_side",
    "demand_side",
    "macro_context",
    "forward_indicators",
    "cross_segment_relationships",
)

MARKET_SECTION_SCHEMA: tuple[str, ...] = (
    "overview_dashboard",
    "executive_summary",
    "entity_deep_dive",
    "disclaimer",
)


# Map a canonical facet_id prefix to the dimension it contributes to.
# The profiler LLM is steered (via MARKET_PROFILER_PROMPT) to use one
# of these ids, but the mapping is also tolerant of LLM-emitted
# variants: anything starting with the prefix counts.
_MARKET_FACET_TO_DIMENSION: Mapping[str, str] = {
    "core_metrics":              "core_metrics",
    "current_price":             "core_metrics",
    "short_term_change":         "core_metrics",
    "range_52w":                 "core_metrics",

    "historical_trends":         "historical_trends",
    "recent_trajectory":         "historical_trends",
    "volatility":                "historical_trends",
    "support_resistance":        "historical_trends",

    "cost_structure":            "cost_structure",
    "input_cost":                "cost_structure",
    "raw_material":              "cost_structure",
    "margin_spread":             "cost_structure",
    "energy_cost":               "cost_structure",

    "supply_side":               "supply_side",
    "capacity_utilization":      "supply_side",
    "inventory_level":           "supply_side",
    "maintenance":               "supply_side",
    "import_export":             "supply_side",
    "supply_bottleneck":         "supply_side",

    "demand_side":               "demand_side",
    "downstream_consumption":    "demand_side",
    "seasonal":                  "demand_side",
    "contract_spot":             "demand_side",
    "geographic_demand":         "demand_side",
    "restocking":                "demand_side",

    "macro_context":             "macro_context",
    "broad_market_index":        "macro_context",
    "fx_rate":                   "macro_context",
    "policy":                    "macro_context",
    "geopolitical":              "macro_context",
    "freight_cost":              "macro_context",

    "forward_indicators":        "forward_indicators",
    "futures_curve":             "forward_indicators",
    "backlog":                   "forward_indicators",
    "lead_time":                 "forward_indicators",
    "contango_backwardation":    "forward_indicators",

    "cross_segment":             "cross_segment_relationships",
    "substitute":                "cross_segment_relationships",
    "complementary":             "cross_segment_relationships",
    "upstream_downstream":       "cross_segment_relationships",
}


def dimension_for_market_facet(facet_id: str) -> str | None:
    """Return the dimension a market facet belongs to.

    Falls back to ``"cross_segment_relationships"`` when the LLM emits
    an unexpected id — better to over-cover than to drop dimensions.
    """
    fid = (facet_id or "").strip()
    if not fid:
        return None
    if fid in _MARKET_FACET_TO_DIMENSION:
        return _MARKET_FACET_TO_DIMENSION[fid]
    # Prefix match — handles LLM variants like "core_metrics_brent"
    for prefix, dim in _MARKET_FACET_TO_DIMENSION.items():
        if fid.startswith(prefix):
            return dim
    return None


def build() -> Profile:
    return Profile(
        name="market",
        label="Institutional-Grade Market Overview / Trend Report / Weekly Digest",
        facet_spec=MARKET_DIMENSIONS,
        section_schema=MARKET_SECTION_SCHEMA,
        profiler_prompt="",        # resolved at runtime from prompts.py
        synthesizer_prompt="",     # resolved at runtime from prompts.py
        facet_to_dimension=_MARKET_FACET_TO_DIMENSION,
    )
