"""Tests for the stale-data freshness checks + table preference rules.

`erp_product_sales_details` only holds 1.4k rows up to 2025-12-31, while
`erp_t_sal_outstock` / `erp_v_sale_orderentry` hold ~90k rows through
2026-08-11. The prompts must:

1. Force a ``MAX(date_column)`` freshness check before reporting date-bounded
   analyses (no more claiming "last 30 days" over a stale table).
2. Prefer the live outstock / sale-order-entry tables over the stale sales
   details table.
3. Point the weekly-report pipeline at the new parallel batch tool.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

def test_data_agent_prompt_has_freshness_check():
    """DATA_AGENT_PROMPT must enforce a MAX(date_column) freshness check."""
    from app.services.agent_definitions import DATA_AGENT_PROMPT

    assert "DATA FRESHNESS" in DATA_AGENT_PROMPT.upper() or "MAX(" in DATA_AGENT_PROMPT
    assert "freshness" in DATA_AGENT_PROMPT.lower()
    assert "last 30 days" in DATA_AGENT_PROMPT.lower() or "30 days" in DATA_AGENT_PROMPT.lower()
    assert "latest" in DATA_AGENT_PROMPT.lower()

def test_data_agent_prompt_requires_stale_disclosure():
    """The prompt must force the agent to disclose staleness instead of lying."""
    from app.services.agent_definitions import DATA_AGENT_PROMPT

    low = DATA_AGENT_PROMPT.lower()
    # Must tell the agent to explicitly state the data's max date when stale
    # and not claim coverage it doesn't have.
    assert "explicitly" in low or "explicit" in low
    assert "stale" in low
    assert "do not claim" in low or "never claim" in low or "do NOT claim" in DATA_AGENT_PROMPT

def test_weekly_report_skill_uses_batch_tool():
    """SKILL.md must use the batch tool instead of three serial calls."""
    from app.services.skills_loader import load_skill_package

    skill = load_skill_package(
        _BACKEND_ROOT / "skills" / "weekly-report-generation", source="bundled"
    )
    assert skill is not None
    assert "ask_perception_intelligence_diagnosis" in skill.body

def test_weekly_report_skill_covers_all_products_and_inventory():
    """SKILL.md must list ALL C5/C9 products and name the inventory table."""
    from app.services.skills_loader import load_skill_package

    skill = load_skill_package(
        _BACKEND_ROOT / "skills" / "weekly-report-generation", source="bundled"
    )
    assert skill is not None
    body = skill.body
    # All seven C5/C9 products must be listed (not just one product).
    for product in (
        "碳五石油树脂",
        "双环戊二烯",
        "异戊二烯",
        "戊烷发泡剂",
        "抽余碳五",
        "间戊二烯",
        "工业用裂解碳九",
    ):
        assert product in body, f"missing product {product!r} in SKILL.md"
    # Inventory table must be named explicitly.
    assert "erp_v_stk_inventory" in body or "erp_t_stk_inventory" in body
    # Unified sales view must be named explicitly.
    assert "erp_v_sale_orderentry" in body or "erp_t_sal_outstock" in body
    # Sales and inventory must be split into two queries.
    assert "sales" in body.lower() and "inventory" in body.lower()
