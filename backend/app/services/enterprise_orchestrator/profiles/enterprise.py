"""``enterprise`` profile — the legacy business-data report shape.

Default profile. Section schema matches the 6-section ExecutiveReport
rendered by ``synthesizer.synthesize_report``. The facet_to_dimension
map is intentionally minimal — enterprise coverage is dimensional by
domain (operational / financial / sales / logistics), not by per-facet
count.
"""
from __future__ import annotations

from typing import Mapping

from . import Profile


_ENTERPRISE_FACET_TO_DIMENSION: Mapping[str, str] = {
    # Operational
    "kpi_summary":      "operational_kpis",
    "trend_recent":     "operational_kpis",
    "anomaly_alerts":   "operational_kpis",
    # Financial
    "revenue_breakdown": "financial_performance",
    "margin_walk":       "financial_performance",
    "cashflow_status":   "financial_performance",
    # Pipeline / sales
    "top_customers":     "sales_operations",
    "pipeline_health":   "sales_operations",
    # Logistics / supply
    "inventory_position": "logistics",
    "shipment_throughput": "logistics",
}


def build() -> Profile:
    return Profile(
        name="enterprise",
        label="Enterprise Business Data Report",
        facet_spec=(
            "operational_kpis",
            "financial_performance",
            "sales_operations",
            "logistics",
        ),
        section_schema=(
            "executive_summary",
            "key_findings",
            "primary_metric_breakdown",
            "segment_decomposition",
            "operational_drivers",
            "risk_section",
            "next_actions",
        ),
        profiler_prompt="",
        synthesizer_prompt="",
        facet_to_dimension=_ENTERPRISE_FACET_TO_DIMENSION,
    )
