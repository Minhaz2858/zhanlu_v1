"""D4 (2026-08-20): deterministic category-subset coverage check.

When the user asks about a whole category/portfolio and the query results only
cover a PROPER SUBSET of the catalog's known members (per-product view instead
of the unified table), the verification gate must flag ``category_subset`` and
nudge the agent to re-query the unified table.
"""

from app.config import settings
from app.services.answer_verification import (
    _detect_category_subset,
    evaluate_answer,
)

CATALOG = {
    "FNAME": ["乙烯", "工业用裂解碳五", "双环戊二烯", "碳五石油树脂"],
    "region": ["east", "west", "north", "south"],
}


def _partial_product_results() -> list[dict]:
    """Simulate a query against a per-product view: only ONE product's rows."""
    return [
        {
            "tool": "execute_query",
            "query": "SELECT * FROM sale_erp_v_工业用裂解碳五_data",
            "columns": ["FNAME", "shipment_quantity"],
            "rows": [
                ["工业用裂解碳五", 100],
                ["工业用裂解碳五", 150],
            ],
            "row_count": 2,
            "empty": False,
            "text": "",
        }
    ]


def test_detect_category_subset_flags_partial_coverage(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", True)
    signals = _detect_category_subset(
        "Give me supply chain data for all products in the last 30 days",
        _partial_product_results(),
        "Here is the full supply chain picture.",
        catalog_meta=CATALOG,
    )
    assert signals == ["category_subset"]


def test_detect_category_subset_no_flag_full_coverage(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", True)
    results = [
        {
            "columns": ["FNAME", "shipment_quantity"],
            "rows": [
                ["乙烯", 10],
                ["工业用裂解碳五", 20],
                ["双环戊二烯", 30],
                ["碳五石油树脂", 40],
            ],
        }
    ]
    assert (
        _detect_category_subset(
            "Show me the portfolio overview for all products",
            results,
            "All four products covered.",
            catalog_meta=CATALOG,
        )
        == []
    )


def test_detect_category_subset_no_flag_without_category_intent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", True)
    assert (
        _detect_category_subset(
            "What is the shipment volume of 工业用裂解碳五?",
            _partial_product_results(),
            "Shipment volume is 250 tons.",
            catalog_meta=CATALOG,
        )
        == []
    )


def test_detect_category_subset_no_flag_without_catalog(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", True)
    assert (
        _detect_category_subset(
            "Give me supply chain data for all products",
            _partial_product_results(),
            "Here is the data.",
            catalog_meta=None,
        )
        == []
    )


def test_detect_category_subset_flag_gated_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", False)
    assert (
        _detect_category_subset(
            "Give me supply chain data for all products",
            _partial_product_results(),
            "Here is the data.",
            catalog_meta=CATALOG,
        )
        == []
    )


def test_detect_category_subset_no_flag_column_absent(monkeypatch) -> None:
    """The catalog member column must actually appear in the query results."""
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", True)
    results = [
        {
            "columns": ["shipment_date", "shipment_quantity"],
            "rows": [["2026-08-01", 100]],
        }
    ]
    assert (
        _detect_category_subset(
            "Give me supply chain data for all products",
            results,
            "Here is the data.",
            catalog_meta=CATALOG,
        )
        == []
    )


def test_detect_category_subset_single_member_column_skipped(monkeypatch) -> None:
    """Columns with <2 known members are not category enumerations."""
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", True)
    results = [
        {
            "columns": ["customer"],
            "rows": [["Acme Corp"]],
        }
    ]
    assert (
        _detect_category_subset(
            "Give me the portfolio overview for all products",
            results,
            "Here is the data.",
            catalog_meta={"customer": ["Acme Corp"]},
        )
        == []
    )


def test_evaluate_answer_wires_category_subset_signal(monkeypatch) -> None:
    """End-to-end through the gate: partial portfolio coverage -> INCOMPLETE
    with the ``category_subset`` signal and a unified-table nudge."""
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    res = evaluate_answer(
        "Give me supply chain data for all products in the last 30 days",
        _partial_product_results(),
        "Here is the full supply chain picture for all products.",
        attempts=0,
        budget_remaining=100,
        catalog_meta=CATALOG,
    )
    assert res.status == "INCOMPLETE"
    assert "category_subset" in res.signals
    assert "unified table" in res.suggested_fix.lower()


def test_evaluate_answer_category_subset_disabled(monkeypatch) -> None:
    """Flag off -> deterministic detectors do not fire on partial coverage."""
    monkeypatch.setattr(settings, "CATEGORY_SUBSET_CHECK_ENABLED", False)
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    res = evaluate_answer(
        "Give me supply chain data for all products in the last 30 days",
        _partial_product_results(),
        "Here is the full supply chain picture for all products.",
        attempts=0,
        budget_remaining=100,
        catalog_meta=CATALOG,
    )
    assert "category_subset" not in res.signals
