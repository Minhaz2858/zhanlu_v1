"""Phase 2.4 — capability-aware, permission-aware fallback selection (TDD).

Fallback selection must be AUTHORITY-aware (published only), CAPABILITY-aware
(keyword match against the failed objective), and must refuse destructive
rerouting outright.

Run: pytest tests/test_capability_fallback.py -q
"""
from app.services.capability_fallback import (
    fallback_permitted,
    select_alternatives,
)


CANDIDATES = [
    {"name": "sales_analyst", "description": "Analyzes vehicle sales revenue and trends", "published": True},
    {"name": "finance_agent", "description": "Production cost, logistics cost and margin analysis", "published": True},
    {"name": "inventory_agent", "description": "Warehouse inventory levels and stock", "published": True},
    {"name": "demand_forecaster", "description": "Forecasts demand from history", "published": False},  # DRAFT
    {"name": "hr_payroll", "description": "Payroll processing", "published": True},  # unrelated
]


def test_published_only():
    out = select_alternatives(CANDIDATES, required_keywords=("demand", "forecast"))
    names = [c["name"] for c in out]
    assert "demand_forecaster" not in names  # unpublished is NEVER a fallback


def test_keyword_match_is_capability_aware():
    out = select_alternatives(CANDIDATES, required_keywords=("cost", "margin"))
    assert [c["name"] for c in out] == ["finance_agent"]


def test_unrelated_candidates_never_match():
    out = select_alternatives(CANDIDATES, required_keywords=("translation", "localization"))
    assert out == []  # honest refusal — no capability-stretching


def test_failed_agent_excluded():
    out = select_alternatives(CANDIDATES, required_keywords=("sales", "revenue"), exclude={"sales_analyst"})
    assert all(c["name"] != "sales_analyst" for c in out)


def test_limit_bounded():
    out = select_alternatives(CANDIDATES, required_keywords=("analysis",), limit=2)
    assert len(out) <= 2


def test_rationale_names_authority():
    out = select_alternatives(CANDIDATES, required_keywords=("cost",))
    assert out and "published" in out[0]["rationale"].lower()


def test_empty_candidates_refuse_honestly():
    assert select_alternatives([], required_keywords=("cost",)) == []


def test_fallback_permitted_refuses_destructive():
    assert fallback_permitted("cronjob", {"action": "delete"}) is False
    assert fallback_permitted("read_file", {"path": "/tmp/x"}) is True
    # Unknown tools default to permitted (read-only risk resolution).
    assert fallback_permitted("list_data_sources", {}) is True
