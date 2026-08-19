"""Fix 1b — catalog validation gate for the dimension-coverage detector.

When ``catalog_meta`` (column name -> [sample values]) is available, candidate
dimension tokens that do NOT map to a real catalog column or to any sampled
value are treated as hallucinated/non-data words and silently dropped. This
kills the remaining phantom-dimension flags in the live trace (conv 86d8861c)
where the agent degraded a PPT build because a requested token did not appear
verbatim in the returned payload.
"""
import pytest

from app.config import settings
from app.services import answer_verification as av


@pytest.fixture(autouse=True)
def _enable_gate(monkeypatch):
    """Deterministic gate: SELF_EVAL on, LLM inspector off."""
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)


# ── unmappable tokens suppressed when catalog present ─────────────────────


def test_unmappable_token_suppressed_with_catalog():
    """A request token that maps to NO catalog column and NO sample value is
    silently dropped — it cannot be a real data dimension."""
    results = [{
        "tool": "execute_query",
        "columns": ["region", "sales_amount"],
        "rows": [{"region": "east", "sales_amount": 3200}],
    }]
    catalog_meta = {
        "region": ["east", "west", "north", "south"],
        "sales_amount": ["3200", "4100"],
    }
    missing = av._detect_dimension_coverage(
        "show sales by region and by zodiac", results,
        "Sales by region: east 3200.",
        catalog_meta=catalog_meta,
    )
    # 'zodiac' is unmappable → dropped; 'region' maps to a column → kept but
    # it IS present in the payload, so nothing is flagged.
    assert missing == []


def test_unmappable_token_suppressed_when_absent_from_payload():
    """Unmappable tokens are dropped even when they are absent from the
    payload — the catalog gate is what suppresses them, not the corpus."""
    results = [{
        "tool": "execute_query",
        "columns": ["region", "sales_amount"],
        "rows": [{"region": "east", "sales_amount": 3200}],
    }]
    catalog_meta = {
        "region": ["east", "west", "north", "south"],
        "sales_amount": ["3200", "4100"],
    }
    missing = av._detect_dimension_coverage(
        "break down by zodiac signs", results, "Total sales: 3200.",
        catalog_meta=catalog_meta,
    )
    assert missing == []


# ── mappable tokens still flag when absent ────────────────────────────────


def test_mappable_token_still_flags_when_absent():
    """A real catalog column ('region') that the user asked for but the
    payload lacks is STILL flagged — the catalog gate must not over-suppress."""
    results = [{
        "tool": "execute_query",
        "columns": ["sales_amount"],
        "rows": [{"sales_amount": 3200}],
    }]
    catalog_meta = {
        "region": ["east", "west", "north", "south"],
        "sales_amount": ["3200", "4100"],
    }
    missing = av._detect_dimension_coverage(
        "show me sales by region", results, "Total sales: 3200.",
        catalog_meta=catalog_meta,
    )
    assert missing == ["region"]


def test_value_sample_token_flags_when_absent():
    """A token that appears inside a column's value_samples is a genuine data
    vocabulary word and is treated as a mappable dimension."""
    results = [{
        "tool": "execute_query",
        "columns": ["channel", "sales_amount"],
        "rows": [{"channel": "retail", "sales_amount": 3200}],
    }]
    catalog_meta = {
        "channel": ["online", "offline", "retail"],
        "sales_amount": ["3200", "4100"],
    }
    missing = av._detect_dimension_coverage(
        "show online channel sales", results, "Total sales: 3200.",
        catalog_meta=catalog_meta,
    )
    # 'channel' is in the payload corpus; 'online' maps to a sample value of
    # 'channel' → kept, absent → flagged. Only 'online' remains.
    assert missing == ["online"]


# ── no catalog → legacy lexical behavior ──────────────────────────────────


def test_no_catalog_falls_back_to_lexical_mode():
    """Without catalog_meta the detector keeps legacy behavior: any non-stop
    word is a candidate dimension (pre-existing semantics preserved)."""
    results = [{"tool": "execute_query", "columns": ["sales_amount"],
                "rows": [{"sales_amount": 3200}]}]
    missing = av._detect_dimension_coverage(
        "show sales by zodiac", results, "Total sales: 3200.",
    )
    assert missing == ["zodiac"]


def test_case_insensitive_column_match():
    """Column-name matching is case-insensitive (catalog stores FNAME, users
    type fname / FName)."""
    results = [{
        "tool": "execute_query",
        "columns": ["sales_amount"],
        "rows": [{"sales_amount": 3200}],
    }]
    catalog_meta = {
        "FNAME": ["PVC", "C5"],
        "sales_amount": ["3200", "4100"],
    }
    missing = av._detect_dimension_coverage(
        "break down by fname", results, "Total sales: 3200.",
        catalog_meta=catalog_meta,
    )
    assert missing == ["fname"]


def test_evaluate_answer_accepts_catalog_meta_kwarg():
    """The public gate entry point accepts catalog_meta and passes it through
    to the dimension detector (smoke test for the plumbing)."""
    results = [{
        "tool": "execute_query",
        "columns": ["sales_amount"],
        "rows": [{"sales_amount": 3200}],
    }]
    result = av.evaluate_answer(
        "show sales by zodiac",
        results,
        "Total sales: 3200.",
        attempts=0,
        budget_remaining=10,
        catalog_meta={"region": ["east"], "sales_amount": ["3200"]},
    )
    # 'zodiac' unmappable → suppressed → COMPLETE (no nudge).
    assert result.status == "COMPLETE"
