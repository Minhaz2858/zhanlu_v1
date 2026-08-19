"""Tests for the Goal-Contract Architecture (app/services/goal_contract.py)."""

from __future__ import annotations

import pytest

from app.services.goal_contract import (
    GoalContract,
    build_goal_contract,
    distinct_values_feedback,
    extract_text_filters,
    normalize_deliverable_intent,
    pending_action_phrase,
)


# ── normalize_deliverable_intent ─────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "make me a dashboard",
        "build a Dashbord for sales",
        "dahsboard please",
        "I need a dash-board view",
        "做一个销售看板",
        "仪表盘",
    ],
)
def test_normalize_dashboard_typo_tolerant(text: str) -> None:
    assert normalize_deliverable_intent(text) == "dashboard"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("make a pptx deck", "pptx"),
        ("PowerPoint for the board", "pptx"),
        ("做一份销售总览PPT", "pptx"),
        ("a docx report please", "docx"),
        ("word document", "docx"),
        ("export to xlsx", "xlsx"),
        ("Excel workbook", "xlsx"),
        ("pdf export", "pdf"),
        ("markdown summary", "md"),
        ("an html page", "html"),
    ],
)
def test_normalize_other_deliverables(text: str, expected: str) -> None:
    assert normalize_deliverable_intent(text) == expected


@pytest.mark.parametrize(
    "text",
    ["hello how are you", "what were our sales last month", "", None],
)
def test_normalize_no_deliverable(text) -> None:
    assert normalize_deliverable_intent(text) is None


# ── build_goal_contract ──────────────────────────────────────────────────


def test_build_contract_plain_data_question() -> None:
    c = build_goal_contract(
        "what were our total shipments last month",
        agent_config={"tools": ["execute_query", "create_artifact"], "bound_kb_ids": [1]},
    )
    assert c.requires_data is True
    assert c.expects_rows is True
    assert c.deliverable is None
    assert c.pending_action_phrase is None


def test_build_contract_deliverable() -> None:
    c = build_goal_contract(
        "make me a sales dashboard",
        agent_config={"tools": ["create_fullstack_dashboard"]},
    )
    assert c.deliverable == "dashboard"
    assert c.requires_data is True


def test_build_contract_greeting_is_empty() -> None:
    c = build_goal_contract("hi", agent_config={"tools": ["execute_query"]})
    assert c.deliverable is None
    assert c.requires_data is False
    assert c.expects_rows is False
    assert c.satisfied() is True


def test_build_contract_pending_action() -> None:
    c = build_goal_contract(
        "Let me check the sales data first.",
        agent_config={"tools": ["execute_query"]},
    )
    assert c.pending_action_phrase is not None


# ── pending_action_phrase ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Let me check the data.",
        "I will look up the numbers.",
        "I'll build the dashboard.",
        "I'm going to run the query.",
        "Let's pull the report.",
        "I would like to examine sales.",
    ],
)
def test_pending_action_matches(text: str) -> None:
    assert pending_action_phrase(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "I checked the data already.",
        "The report was generated.",
        "We have finished the analysis.",
        "hello",
        "What was the total?",
        None,
        "",
    ],
)
def test_pending_action_rejects(text) -> None:
    assert pending_action_phrase(text) is None


# ── extract_text_filters ─────────────────────────────────────────────────


def test_extract_text_filters_like() -> None:
    sql = "SELECT * FROM sales WHERE FNAME LIKE '%ethylene%' LIMIT 10"
    filters = extract_text_filters(sql)
    assert ("fname", "ethylene") in filters


def test_extract_text_filters_equality() -> None:
    sql = "SELECT * FROM t WHERE partner_name = 'ACME'"
    filters = extract_text_filters(sql)
    assert ("partner_name", "ACME") in filters


def test_extract_text_filters_none() -> None:
    assert extract_text_filters("SELECT * FROM sales LIMIT 5") == []
    assert extract_text_filters("") == []
    assert extract_text_filters(None) == []


# ── distinct_values_feedback ─────────────────────────────────────────────


def test_distinct_values_uses_catalog_samples() -> None:
    catalog = {"fname": ["乙烯", "丙烯", "丁二烯"]}
    lines = distinct_values_feedback([("fname", "乙烯")], catalog_meta=catalog)
    assert lines
    assert "乙烯" in lines[0]


def test_distinct_values_falls_back_to_executor() -> None:
    seen = {}

    def executor(col: str):
        seen["col"] = col
        return ["value-a", "value-b"]

    lines = distinct_values_feedback(
        [("partner_name", "ACME")], catalog_meta=None, executor=executor,
    )
    assert seen.get("col") == "partner_name"
    assert lines and "value-a" in lines[0]


def test_distinct_values_cap_50() -> None:
    many = [f"v{i}" for i in range(200)]
    lines = distinct_values_feedback(
        [("col", "x")], catalog_meta={"col": many}, executor=None,
    )
    assert lines
    total_len = sum(len(l) for l in lines)
    assert total_len < 4000


# ── GoalContract.unmet / satisfied ───────────────────────────────────────


def test_unmet_deliverable_unproduced() -> None:
    c = GoalContract(deliverable="pptx", max_forces=3)
    unmet = c.unmet(granted_tools=["create_artifact"])
    assert len(unmet) == 1
    assert unmet[0].code == "deliverable"
    assert unmet[0].force_tool == "create_artifact"


def test_unmet_deliverable_satisfied_by_matching_artifact() -> None:
    c = GoalContract(deliverable="pptx", max_forces=3)
    c.record_artifact(kind="pptx", ok=True, rows=5)
    assert c.unmet(granted_tools=["create_artifact"]) == []
    assert c.satisfied(granted_tools=["create_artifact"]) is True


def test_unmet_report_card_does_not_satisfy_doc_deliverable() -> None:
    c = GoalContract(deliverable="docx", max_forces=3)
    c.record_artifact(kind="html_report", ok=True, rows=5)
    unmet = c.unmet(granted_tools=["create_artifact"])
    assert any(u.code == "deliverable" for u in unmet)


def test_unmet_deliverable_force_skipped_if_tool_not_granted() -> None:
    c = GoalContract(deliverable="pptx", max_forces=3)
    assert c.unmet(granted_tools=["execute_query"]) == []


def test_unmet_dashboard_prefers_fullstack() -> None:
    c = GoalContract(deliverable="dashboard", max_forces=3)
    unmet = c.unmet(granted_tools=["create_artifact", "create_fullstack_dashboard"])
    assert unmet and unmet[0].force_tool == "create_fullstack_dashboard"


def test_unmet_zero_rows_with_filters_forces_requery() -> None:
    c = GoalContract(requires_data=True, expects_rows=True, max_forces=3)
    c.record_query_result(rows=[], sql="SELECT * FROM t WHERE fname = 'zzz'")
    unmet = c.unmet(granted_tools=["execute_query"])
    assert unmet and unmet[0].code == "zero_rows"
    assert unmet[0].force_tool == "execute_query"
    assert "zzz" in unmet[0].message  # distinct values injected


def test_unmet_zero_rows_without_filters_no_force() -> None:
    c = GoalContract(requires_data=True, expects_rows=True, max_forces=3)
    c.record_query_result(rows=[], sql="SELECT * FROM t LIMIT 5")
    assert c.unmet(granted_tools=["execute_query"]) == []


def test_unmet_zero_rows_resolved_by_nonempty() -> None:
    c = GoalContract(requires_data=True, expects_rows=True, max_forces=3)
    c.record_query_result(rows=[], sql="SELECT * FROM t WHERE fname = 'zzz'")
    c.record_query_result(rows=[{"x": 1}], sql="SELECT * FROM t WHERE fname = 'zzz'")
    assert c.unmet(granted_tools=["execute_query"]) == []


def test_unmet_pending_action_forces_announced_tool() -> None:
    c = GoalContract(pending_action_phrase="Let me check the sales data", max_forces=3)
    unmet = c.unmet(granted_tools=["execute_query", "create_artifact"])
    assert unmet and unmet[0].code == "pending_action"
    assert unmet[0].force_tool == "execute_query"


def test_unmet_pending_action_skipped_if_tool_not_granted() -> None:
    c = GoalContract(pending_action_phrase="Let me check the sales data", max_forces=3)
    assert c.unmet(granted_tools=["create_artifact"]) == []


def test_force_budget_exhaustion_stops_forcing() -> None:
    c = GoalContract(deliverable="pptx", max_forces=2)
    c.record_force()
    c.record_force()
    assert c.unmet(granted_tools=["create_artifact"]) == []


def test_force_budget_consumes_one_per_unmet() -> None:
    c = GoalContract(deliverable="pptx", max_forces=3)
    first = c.unmet(granted_tools=["create_artifact"])
    assert first and first[0].code == "deliverable"
    c.record_force()
    second = c.unmet(granted_tools=["create_artifact"])
    assert second and second[0].code == "deliverable"
    c.record_force()
    third = c.unmet(granted_tools=["create_artifact"])
    assert third and third[0].code == "deliverable"
    c.record_force()
    assert c.unmet(granted_tools=["create_artifact"]) == []


def test_pending_action_cleared_after_announced_tool_runs() -> None:
    c = GoalContract(pending_action_phrase="Let me check the sales data", max_forces=3)
    c.record_tool_executed("execute_query")
    assert c.unmet(granted_tools=["execute_query"]) == []
