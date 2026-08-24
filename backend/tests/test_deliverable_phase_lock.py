"""Deliverable phase-lock gate (Bug 1/2 fix).

The mid-loop gate blocks create_artifact / run_sandbox_skill while the
contract requires data and no answer-tagged dataset exists. The deliverable
is then synthesized + finalized ONCE, post-loop, from the LAST answer dataset
(deferred single emission). These tests pin the deterministic predicates the
agents.py gate and the post-loop emission are thin wrappers around.
"""

from __future__ import annotations

from app.routers.agents import _strip_internal_references
from app.services.goal_contract import build_goal_contract


def _make_contract(question: str = "Give me shipment data for last 30 days"):
    # Use a question that genuinely arms requires_data=True ("shipment").
    # "Give me supply chain data…" is treated as a NEUTRAL question by
    # _DATA_QUESTION_RE (established by the pending-seq regression suite),
    # so it would make these gate predicates vacuously true.
    c = build_goal_contract(question)
    return c


def _record_answer(c, *, rows=None, tool_call_id="tc-answer", purpose="answer"):
    c.record_dataset(
        rows=rows if rows is not None else [{"product": "C5", "revenue": 1000}],
        sql="SELECT FNAME, revenue FROM erp_product_sales_details",
        source_name="erp_product_sales_details",
        source_id="erp_product_sales_details",
        purpose=purpose,
        tool_call_id=tool_call_id,
    )


# ── Gate condition: block while requires_data and no answer data ─────────


def test_gate_blocks_before_any_data():
    c = _make_contract()
    assert c.requires_data
    # Gate fires when: requires_data AND NOT has_answer_data().
    assert not c.has_answer_data()
    assert not c.collection_complete()


def test_gate_allows_after_answer_dataset():
    c = _make_contract()
    _record_answer(c)
    assert c.has_answer_data()
    assert c.collection_complete()


def test_probe_and_auxiliary_never_satisfy_gate():
    c = _make_contract()
    _record_answer(c, rows=[{"x": 1}], purpose="probe", tool_call_id="tc-probe")
    _record_answer(c, rows=[{"x": 2}], purpose="auxiliary", tool_call_id="tc-aux")
    assert not c.has_answer_data()
    assert not c.collection_complete()


def test_empty_answer_rows_do_not_satisfy_gate():
    c = _make_contract()
    _record_answer(c, rows=[], tool_call_id="tc-empty")
    assert not c.has_answer_data()
    assert not c.collection_complete()


# ── Deferred single-emission selection: LAST answer dataset wins ─────────


def test_deferred_selection_picks_last_answer_dataset():
    c = _make_contract()
    _record_answer(c, rows=[{"v": 1}], tool_call_id="tc-1")
    _record_answer(c, rows=[{"v": 2}], tool_call_id="tc-2")
    _record_answer(c, rows=[{"v": 3}], purpose="probe", tool_call_id="tc-probe")
    answers = c.answer_datasets()
    assert [d["tool_call_id"] for d in answers] == ["tc-1", "tc-2"]
    assert c.answer_datasets()[-1]["tool_call_id"] == "tc-2"
    assert c.collection_complete()


# ── Deterministic: never derived from model prose ────────────────────────


def test_collection_complete_ignores_model_announcements():
    # A model that ANNOUNCES a query but never executes it must NOT complete
    # the collection (the armed-but-unexecuted guard in collection_complete).
    c = _make_contract()
    c.refresh_pending_action("I'll query the live tables now.")
    _record_answer(c)  # even with data, an armed-but-unexecuted action blocks
    # NOTE: if data is already usable AND the action is a real query it would
    # normally be executed; here nothing executed it, so the contract keeps
    # the collection open (deterministic, not prose-derived).
    assert not c.collection_complete()


# ── Hygiene strip stays wired to the deferred pipeline ───────────────────


def test_hygiene_strip_clean_after_deferred_answer():
    text = (
        "Gross margin was 21.4% for the period. "
        "Let me re-query the live tables once more."
    )
    out = _strip_internal_references(text)
    assert "re-query" not in out
    assert out.strip().endswith("period.")
