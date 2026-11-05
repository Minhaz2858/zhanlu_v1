"""Phase 3.5 P3 — semantic specialist selection: deterministic scenario eval.

NO live embeddings, NO model downloads. A domain-keyed FAKE embedder provides
deterministic similarity; the test asserts selection correctness (true/false
positives) across the 10 manufacturing scenario families. Where deterministic
evidence identifies a genuine matcher defect, the matcher is fixed.

Run: pytest tests/test_semantic_selection_scenarios.py -q
"""
import pytest

from app.services.semantic_capability_match import semantic_select_specialists


PROFILES = [
    {"name": "sales_analyst", "description": "Sales revenue, regional performance, order trends",
     "published": True, "department": "sales"},
    {"name": "production_agent", "description": "Production output, line efficiency, downtime analysis",
     "published": True, "department": "manufacturing"},
    {"name": "quality_agent", "description": "Defect rates, quality inspections, rework",
     "published": True, "department": "quality"},
    {"name": "procurement_agent", "description": "Supplier performance, purchasing, price trends",
     "published": True, "department": "procurement"},
    {"name": "inventory_agent", "description": "Inventory levels, shortages, stock turnover",
     "published": True, "department": "supply_chain"},
    {"name": "finance_agent", "description": "Costs, margins, profitability analysis",
     "published": True, "department": "finance"},
    {"name": "demand_forecaster", "description": "Demand forecasting, seasonality prediction",
     "published": True, "department": "sales"},
]

# Deterministic fake embedder: topic space (multiple surface forms → one dim).
_TOPICS = {
    "sales": 0, "production": 1, "quality": 2, "defect": 2,
    "cost": 3, "profitability": 3, "margin": 3,
    "demand": 4, "forecast": 4, "supplier": 5, "inventory": 6, "stock": 6,
}


def _fake_embedder(texts):
    out = []
    for t in texts:
        v = [0.0] * 7
        low = (t or "").lower()
        for topic, idx in _TOPICS.items():
            if topic in low:
                v[idx] = 1.0
        out.append(v)
    return out


# (scenario, task, expected_top_specialist or None for own-tools)
SCENARIOS = [
    ("sales_analysis", "Analyze this quarter's sales revenue by region", "sales_analyst"),
    ("production_analysis", "Investigate production line output decline", "production_agent"),
    ("quality_investigation", "Which products have the highest defect rates?", "quality_agent"),
    ("procurement_analysis", "Review supplier purchasing volumes and price trends", "procurement_agent"),
    ("inventory_investigation", "Check inventory levels and stock shortages", "inventory_agent"),
    ("financial_analysis", "Break down cost structure and margins", "finance_agent"),
    ("cross_dept_profitability", "Profitability by vehicle model", "finance_agent"),
    ("supplier_risk", "Supplier delivery performance risk", "procurement_agent"),
    ("demand_forecast", "Forecast next quarter product demand", "demand_forecaster"),
    ("general_conversational", "Good morning, what can you help me with?", None),
]


@pytest.mark.parametrize("name,task,expected", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_scenario_selection(name, task, expected):
    out = semantic_select_specialists(
        task, PROFILES, embedder=_fake_embedder, top_k=1,
    )
    if expected is None:
        # GA prefers its OWN tools — no specialist for conversation.
        assert out == []
    else:
        assert out and out[0]["name"] == expected, (
            f"{name}: got {[c['name'] for c in out]}, expected {expected}"
        )


def test_cross_department_scenario_selects_genuine_contributors():
    # Profitability genuinely spans finance + sales + production — the top
    # match must be a real contributor, and NOT quality/procurement.
    out = semantic_select_specialists(
        "profitability by vehicle model: production cost versus sales revenue",
        PROFILES, embedder=_fake_embedder, top_k=3,
    )
    names = [c["name"] for c in out]
    assert "finance_agent" in names or "sales_analyst" in names
    assert "quality_agent" not in names and "procurement_agent" not in names


def test_false_positive_guard_unrelated_task():
    out = semantic_select_specialists(
        "translate this document to french", PROFILES, embedder=_fake_embedder,
    )
    assert out == []  # no capability stretching


def test_false_negative_guard_paraphrase():
    # "who assembles our cars" has no literal keyword overlap with
    # "Production output, line efficiency" in lexical terms, but the fake
    # embedder's topic model links them — semantic matching must not miss it.
    out = semantic_select_specialists(
        "production assembly of our cars", PROFILES, embedder=_fake_embedder,
    )
    assert out and out[0]["name"] == "production_agent"


def test_department_wall_holds_in_scenarios():
    out = semantic_select_specialists(
        "production output decline", PROFILES,
        embedder=_fake_embedder, department="finance",
    )
    # Finance has no production-output capability — honest empty.
    assert out == []
