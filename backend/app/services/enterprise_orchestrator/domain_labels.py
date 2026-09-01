"""Domain → report-section label mapping (deterministic, NOT LLM-chosen).

The 6-section report STRUCTURE is fixed:
  1. Executive Summary
  2. Primary Metric Breakdown
  3. Segment Decomposition
  4. Operational Drivers & Anomalies
  5. {Domain-Specific Risk Section}
  6. Strategic Recommendations

Only the LABELS of sections 4 and 5 adapt by domain. Labels are table-driven
here so a reasoning LLM cannot drift section titles per query.

Design spec reference: §7 Domain → Section Label Mapping.
"""
from __future__ import annotations

from typing import TypedDict


class SectionLabels(TypedDict):
    """Adaptive labels for the two domain-sensitive sections."""

    risk_section: str
    drivers_section: str


DOMAIN_SECTION_LABELS: dict[str, SectionLabels] = {
    "supply_chain": {
        "risk_section": "Transmission Risk",
        "drivers_section": "Supply-Demand Drivers",
    },
    "financial_performance": {
        "risk_section": "Cash-Flow Exposure",
        "drivers_section": "Margin & Cost Drivers",
    },
    "logistics": {
        "risk_section": "Service-Level Exceptions",
        "drivers_section": "Throughput & Carrier Drivers",
    },
    "risk_management": {
        "risk_section": "Concentration & Counterparty",
        "drivers_section": "Exposure Decomposition",
    },
    "sales_operations": {
        "risk_section": "Pipeline & Forecast Risk",
        "drivers_section": "Funnel & Conversion Drivers",
    },
    "hr": {
        "risk_section": "Attrition & Capacity Risk",
        "drivers_section": "Workforce Composition",
    },
    "procurement": {
        "risk_section": "Supplier & Lead-Time Risk",
        "drivers_section": "Spend & Category Drivers",
    },
    "generic": {
        "risk_section": "Risk Assessment",
        "drivers_section": "Operational Drivers",
    },
}

# Fallback used for any unknown/undefined domain.
_GENERIC_LABELS: SectionLabels = DOMAIN_SECTION_LABELS["generic"]


def get_section_labels(domain: str | None) -> SectionLabels:
    """Return the adaptive labels for a domain, falling back to generic."""
    if not domain:
        return _GENERIC_LABELS
    return DOMAIN_SECTION_LABELS.get(domain, _GENERIC_LABELS)


def known_domains() -> tuple[str, ...]:
    """All registered domain keys (excludes nothing; generic is included)."""
    return tuple(DOMAIN_SECTION_LABELS.keys())
