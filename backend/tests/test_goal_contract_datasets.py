"""Tests for GoalContract dataset collection (deferred deliverable pipeline).

The contract collects query results during the loop (tagged probe/auxiliary/
answer) and only builds the deliverable once post-loop, from the
answer-tagged set.  ``collection_complete()`` is fully deterministic:
data criteria met AND >= 1 answer-tagged dataset exists.  It never consults
the model's stated plans.
"""

from __future__ import annotations

import pytest

from app.services.goal_contract import GoalContract, build_goal_contract
from app.services.query_purpose import ANSWER, AUXILIARY, PROBE


def _contract(**kw) -> GoalContract:
    return GoalContract(deliverable="report", requires_data=True, expects_rows=True, **kw)


def _row(**kw) -> dict:
    base = {"shipment_date": "2026-08-01", "shipment_quantity": 42}
    base.update(kw)
    return base


def _dataset(purpose: str = ANSWER, tool_call_id: str = "tc-1") -> dict:
    return {
        "rows": [_row()],
        "sql": "SELECT * FROM erp_t_sal_outstock",
        "source_name": "erp_t_sal_outstock",
        "source_id": "kb-1",
        "purpose": purpose,
        "tool_call_id": tool_call_id,
    }


# ── record_dataset / answer_datasets ─────────────────────────────────────


def test_record_dataset_stores_purpose_and_metadata() -> None:
    c = _contract()
    c.record_dataset(**_dataset())
    assert len(c.collected_datasets) == 1
    d = c.collected_datasets[0]
    assert d["purpose"] == ANSWER
    assert d["tool_call_id"] == "tc-1"
    assert d["rows"] == [_row()]


def test_answer_datasets_filters_non_answer() -> None:
    c = _contract()
    c.record_dataset(**{**_dataset(PROBE, "tc-probe"), "rows": []})
    c.record_dataset(**{**_dataset(AUXILIARY, "tc-aux"), "sql": "SELECT FNAME FROM _ref"})
    c.record_dataset(**{**_dataset(ANSWER, "tc-answer"), "sql": "SELECT * FROM fact"})
    answers = c.answer_datasets()
    assert [d["tool_call_id"] for d in answers] == ["tc-answer"]
    assert c.has_answer_data() is True


def test_no_answer_data_means_has_answer_data_false() -> None:
    c = _contract()
    c.record_dataset(**{**_dataset(PROBE, "tc-probe"), "rows": []})
    c.record_dataset(**{**_dataset(AUXILIARY, "tc-aux"), "sql": "SELECT FNAME FROM _ref"})
    assert c.answer_datasets() == []
    assert c.has_answer_data() is False


# ── collection_complete (deterministic) ──────────────────────────────────


def test_collection_complete_false_without_answer_data() -> None:
    c = _contract()
    c.record_dataset(**{**_dataset(PROBE, "tc-probe"), "rows": []})
    assert c.collection_complete() is False


def test_collection_complete_true_with_answer_data() -> None:
    c = _contract()
    c.record_dataset(**{**_dataset(ANSWER, "tc-answer")})
    assert c.collection_complete() is True


def test_collection_complete_true_when_data_not_required() -> None:
    # Pure text turn: no data needed → complete even with zero datasets.
    c = GoalContract(deliverable=None, requires_data=False, expects_rows=False)
    assert c.collection_complete() is True


def test_collection_complete_false_on_zero_row_events() -> None:
    c = _contract()
    c.record_dataset(**{**_dataset(ANSWER, "tc-answer")})
    # A later zero-row probe degrades collection state.
    c.record_query_result([], "SELECT * FROM erp_t_sal_outstock WHERE 1=0")
    assert c.zero_row_events > 0
    assert c.collection_complete() is False


def test_collection_complete_false_on_metadata_only_events() -> None:
    c = _contract()
    c.record_dataset(**{**_dataset(ANSWER, "tc-answer")})
    c.record_query_result(
        [{"max(shipment_date)": "2026-08-21", "min(shipment_date)": "2026-01-01"}],
        "SELECT MIN(shipment_date), MAX(shipment_date) FROM erp_t_sal_outstock",
    )
    assert c.metadata_only_events > 0
    assert c.collection_complete() is False


def test_collection_complete_false_on_unexecuted_pending_action() -> None:
    c = _contract()
    c.record_dataset(**{**_dataset(ANSWER, "tc-answer")})
    # Model announced a future action but never executed it.
    c.pending_action_phrase = "let me verify the live data"
    c._seq += 1
    c._armed_seq = c._seq
    c._armed_by = "model"
    assert c.collection_complete() is False


def test_collection_complete_ignores_model_plans() -> None:
    # The model claiming "I will now build the report" must NOT make the
    # collection complete — determinism comes from data state only.
    c = _contract()
    c.record_dataset(**{**_dataset(ANSWER, "tc-answer")})
    # The phrase exists but the arm is satisfied by execution → still complete.
    c.record_tool_executed("create_artifact")
    assert c.collection_complete() is True


# ── integration with build_goal_contract ─────────────────────────────────


def test_build_contract_data_question_requires_collection() -> None:
    c = build_goal_contract("Give me shipment data for last 30 days")
    assert c.requires_data is True
    assert c.collection_complete() is False  # no datasets recorded yet
    c.record_dataset(**{**_dataset(ANSWER, "tc-answer")})
    assert c.collection_complete() is True
