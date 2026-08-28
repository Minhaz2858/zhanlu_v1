"""Task B2: market intent → market KB grounding (deterministic, no LLM).

The C5_C9 project binds two KBs after B1: the ERP warehouse
(``aipdp_data_warehouse_prod``) and the Market Research KB copy (name
contains 'Market'). When the user intent mentions market/industry keywords
(market / industry / research / 市场 / 行业 / 行情), the KB grounding
selection must prefer the Market Research KB over the first-bound ERP KB.

Selection must be pure and deterministic — plain keyword substring
matching, no LLM involved.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.synexia.capability_router import (
    _execute_nl2sql_node,
    _select_grounding_kb,
)

ERP_ID = "kb-erp-warehouse"
MARKET_ID = "kb-market-research"

KB_META = {
    ERP_ID: {"name": "aipdp_data_warehouse_prod", "description": "ERP warehouse"},
    MARKET_ID: {"name": "Market Research 2026", "description": "Industry market research data"},
}


# ── Pure selection helper ───────────────────────────────────────────────

def test_market_keyword_prefers_market_kb():
    """'market data' in the user message → market KB wins over first-bound ERP."""
    assert _select_grounding_kb([ERP_ID, MARKET_ID], KB_META, "pull market data for Q3") == MARKET_ID


def test_chinese_market_keyword_prefers_market_kb():
    """'市场行情' in the user message → market KB wins."""
    assert _select_grounding_kb([ERP_ID, MARKET_ID], KB_META, "帮我分析市场行情") == MARKET_ID


def test_industry_keyword_prefers_market_kb():
    assert _select_grounding_kb([ERP_ID, MARKET_ID], KB_META, "industry trends report") == MARKET_ID


def test_research_keyword_prefers_market_kb():
    assert _select_grounding_kb([ERP_ID, MARKET_ID], KB_META, "research data on market growth") == MARKET_ID


def test_no_market_keyword_keeps_first_bound():
    """No market keyword → existing first-bound behavior (ERP first)."""
    assert _select_grounding_kb([ERP_ID, MARKET_ID], KB_META, "show warehouse inventory levels") == ERP_ID


def test_none_message_keeps_first_bound():
    assert _select_grounding_kb([ERP_ID, MARKET_ID], KB_META, None) == ERP_ID


def test_empty_message_keeps_first_bound():
    assert _select_grounding_kb([ERP_ID, MARKET_ID], KB_META, "") == ERP_ID


def test_market_intent_without_market_kb_falls_back():
    """Market intent but no market KB bound → first bound (ERP)."""
    assert _select_grounding_kb([ERP_ID], KB_META, "market data please") == ERP_ID


def test_empty_bounds_returns_none():
    assert _select_grounding_kb([], KB_META, "market data") is None


def test_market_kb_detected_via_description_only():
    """A KB whose DESCRIPTION mentions market/research is still preferred."""
    meta = {
        ERP_ID: {"name": "aipdp_data_warehouse_prod", "description": "ERP warehouse"},
        MARKET_ID: {"name": "external_copy_1", "description": "Market research data warehouse"},
    }
    assert _select_grounding_kb([ERP_ID, MARKET_ID], meta, "market data") == MARKET_ID


# ── Node-level: _execute_nl2sql_node forwards the chosen KB ─────────────

def test_nl2sql_node_grounds_on_market_kb():
    """_execute_nl2sql_node passes the MARKET kb to NLAnswerService."""
    execution = SimpleNamespace(
        id="exec-1", conversation_id="c1", user_message="make a market data deck"
    )
    node = SimpleNamespace(name="Query", description="", inputs={"question": "market data"})

    with patch(
        "app.services.synexia.capability_router._load_bound_kb_meta", return_value=KB_META
    ), patch("app.services.db.NLAnswerService") as mock_svc_cls, patch(
        "app.services.synexia.capability_router._record_observation"
    ) as mock_record:
        mock_svc = mock_svc_cls.return_value
        mock_svc.answer = AsyncMock(
            return_value={"success": True, "rows": [], "sql": "SELECT 1", "answer": "ok"}
        )
        mock_record.return_value = SimpleNamespace(success=True)

        _execute_nl2sql_node(
            db=MagicMock(), execution=execution, node=node,
            data_ctx_extras={"bound_kb_ids": [ERP_ID, MARKET_ID]},
        )

    assert mock_svc.answer.await_args.args[0] == MARKET_ID


def test_nl2sql_node_keeps_first_bound_without_market_intent():
    """No market intent → _execute_nl2sql_node keeps the first bound KB (ERP)."""
    execution = SimpleNamespace(
        id="exec-2", conversation_id="c1", user_message="show inventory levels"
    )
    node = SimpleNamespace(name="Query", description="", inputs={"question": "inventory levels"})

    with patch(
        "app.services.synexia.capability_router._load_bound_kb_meta", return_value=KB_META
    ), patch("app.services.db.NLAnswerService") as mock_svc_cls, patch(
        "app.services.synexia.capability_router._record_observation"
    ) as mock_record:
        mock_svc = mock_svc_cls.return_value
        mock_svc.answer = AsyncMock(
            return_value={"success": True, "rows": [], "sql": "SELECT 1", "answer": "ok"}
        )
        mock_record.return_value = SimpleNamespace(success=True)

        _execute_nl2sql_node(
            db=MagicMock(), execution=execution, node=node,
            data_ctx_extras={"bound_kb_ids": [ERP_ID, MARKET_ID]},
        )

    assert mock_svc.answer.await_args.args[0] == ERP_ID
