"""Tests for the Goal-Contract Architecture (app/services/goal_contract.py)."""

from __future__ import annotations

import pytest

from app.services.goal_contract import (
    GoalContract,
    build_goal_contract,
    catalog_oracle_feedback,
    distinct_values_feedback,
    extract_tables_from_sql,
    extract_text_filters,
    is_effective_empty,
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


# ── is_effective_empty ───────────────────────────────────────────────────


def test_is_effective_empty_no_rows() -> None:
    assert is_effective_empty([]) is True
    assert is_effective_empty(None) is True


def test_is_effective_empty_all_null_numeric() -> None:
    # The "header-only snapshot": rows exist but every measure is null.
    rows = [
        {"product": None, "quantity": None, "revenue": None},
        {"product": None, "quantity": None, "revenue": None},
        {"product": None, "quantity": None, "revenue": None},
    ]
    assert is_effective_empty(rows) is True


def test_is_effective_empty_all_zero_numeric() -> None:
    rows = [
        {"product": "", "quantity": 0, "revenue": "0.0"},
        {"product": "", "quantity": 0, "revenue": 0},
    ]
    assert is_effective_empty(rows) is True


def test_is_effective_empty_partial_signal_is_not_empty() -> None:
    # ANY non-null/non-zero/non-empty value means the payload has signal.
    assert is_effective_empty([{"quantity": 0, "revenue": 5}]) is False
    assert is_effective_empty([{"quantity": None, "revenue": "乙烯"}]) is False
    assert is_effective_empty([{"quantity": 1}]) is False


# ── extract_tables_from_sql ──────────────────────────────────────────────


def test_extract_tables_from_sql_simple_from() -> None:
    assert extract_tables_from_sql("SELECT * FROM erp_t_sal_outstock LIMIT 5") == [
        "erp_t_sal_outstock"
    ]


def test_extract_tables_from_sql_with_join() -> None:
    sql = (
        "SELECT a.quantity, b.contract_price FROM erp_t_sal_outstock a "
        "JOIN erp_product_sales_details b ON a.material_id = b.material_id"
    )
    tables = extract_tables_from_sql(sql)
    assert "erp_t_sal_outstock" in tables
    assert "erp_product_sales_details" in tables


def test_extract_tables_from_sql_none() -> None:
    assert extract_tables_from_sql(None) == []
    assert extract_tables_from_sql("") == []


def test_record_query_result_records_candidate_tables() -> None:
    c = GoalContract(requires_data=True, expects_rows=True, max_forces=3)
    c.record_query_result(rows=[], sql="SELECT * FROM erp_v_sale_orderentry LIMIT 5")
    assert c.candidate_tables == ["erp_v_sale_orderentry"]


def test_record_query_result_all_null_numeric_counts_as_zero_row() -> None:
    c = GoalContract(requires_data=True, expects_rows=True, max_forces=3)
    header_only = [{"quantity": None, "revenue": None}, {"quantity": None, "revenue": None}]
    c.record_query_result(rows=header_only, sql="SELECT * FROM t LIMIT 5")
    assert c.zero_row_events == 1
    assert c.unmet(granted_tools=["execute_query"])  # remediation fires


# ── catalog_oracle_feedback ──────────────────────────────────────────────


def test_catalog_oracle_feedback_combines_filters_and_tables() -> None:
    def distinct_executor(col: str):
        return ["乙烯", "丙烯"]

    def table_executor(table: str) -> str:
        return f"{table}: last date 2026-07-31"

    lines = catalog_oracle_feedback(
        [("fname", "乙烯")],
        ["erp_t_sal_outstock"],
        distinct_executor=distinct_executor,
        table_executor=table_executor,
    )
    # Distinct feedback present...
    assert any("乙烯" in l for l in lines)
    # Table coverage is only emitted when there are NO filters.
    assert not any("erp_t_sal_outstock" in l for l in lines)

    lines_no_filter = catalog_oracle_feedback(
        [],
        ["erp_t_sal_outstock"],
        distinct_executor=distinct_executor,
        table_executor=table_executor,
    )
    assert any("erp_t_sal_outstock" in l for l in lines_no_filter)
    assert any("2026-07-31" in l for l in lines_no_filter)


def test_catalog_oracle_feedback_degrades_without_executors() -> None:
    assert catalog_oracle_feedback([], ["t1"]) == []


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


def test_unmet_zero_rows_without_filters_forces_requery() -> None:
    # P4: zero_rows now fires even WITHOUT a text filter — an all-null/all-zero
    # snapshot or empty result must trigger remediation, not dead-end.
    c = GoalContract(requires_data=True, expects_rows=True, max_forces=3)
    c.record_query_result(rows=[], sql="SELECT * FROM t LIMIT 5")
    unmet = c.unmet(granted_tools=["execute_query"])
    assert unmet and unmet[0].code == "zero_rows"
    assert unmet[0].force_tool == "execute_query"


def test_unmet_zero_rows_no_filters_with_table_coverage_forces_requery() -> None:
    # No text filters → table-coverage hint from table_executor must be injected.
    def table_executor(table: str) -> str:
        return f"MAX(date)={table}:2026-07-31"

    c = GoalContract(
        requires_data=True, expects_rows=True, max_forces=3,
        table_executor=table_executor,
    )
    c.record_query_result(rows=[], sql="SELECT * FROM erp_t_sal_outstock LIMIT 5")
    unmet = c.unmet(granted_tools=["execute_query"])
    assert unmet and unmet[0].code == "zero_rows"
    assert "erp_t_sal_outstock" in unmet[0].message
    assert "2026-07-31" in unmet[0].message  # live coverage injected


def test_unmet_zero_rows_resolved_by_nonempty() -> None:
    c = GoalContract(requires_data=True, expects_rows=True, max_forces=3)
    c.record_query_result(rows=[], sql="SELECT * FROM t WHERE fname = 'zzz'")
    c.record_query_result(rows=[{"x": 1}], sql="SELECT * FROM t WHERE fname = 'zzz'")
    assert c.unmet(granted_tools=["execute_query"]) == []


def test_unmet_pending_action_forces_announced_tool() -> None:
    # Seq-stamp API: user-armed via build_goal_contract (like agents.py does).
    c = build_goal_contract(
        "Let me check the sales data",
        agent_config={"tools": ["execute_query", "create_artifact"]},
    )
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


def test_pending_action_matches_live_tables_announcement() -> None:
    # The exact sentence from the failed turn: assistant announces it will
    # check the live tables but never calls a tool → contract must catch it.
    sentence = (
        "Let me check the live tables and confirm what data actually exists "
        "before building the deck."
    )
    assert pending_action_phrase(sentence) is not None

    c = build_goal_contract(sentence, agent_config={"tools": ["execute_query"]})
    unmet = c.unmet(granted_tools=["execute_query"])
    assert unmet and unmet[0].code == "pending_action"
    assert unmet[0].force_tool == "execute_query"


def test_pending_action_re_evaluates_from_assistant_text() -> None:
    # Regression guard for the agents.py hook: a fresh announcement in the
    # assistant's latest prose overrides the turn-start value.
    c = GoalContract(max_forces=3)
    assert c.pending_action_phrase is None
    c.refresh_pending_action(
        "Let me check the live tables and confirm what data actually exists."
    )
    assert c.pending_action_phrase is not None
    unmet = c.unmet(granted_tools=["execute_query"])
    assert unmet and unmet[0].code == "pending_action"


def test_pending_action_refresh_ignores_past_tense() -> None:
    c = build_goal_contract(
        "Let me check the sales data",
        agent_config={"tools": ["execute_query"]},
    )
    c.refresh_pending_action("I checked the live tables already.")
    # Past-tense closing does NOT clear the marker (user-armed is never
    # disarmed by prose) — the announcement is still unexecuted, so the
    # contract must keep forcing it.
    assert c.pending_action_phrase is not None
    assert c.unmet(granted_tools=["execute_query"])


# ── agents.py: _make_goal_contract_table_executor ─────────────────────────


class _FakeQueryService:
    """Minimal stand-in for QueryService with a scriptable execute()."""

    def __init__(self, results=None, exc=None):
        self._results = results if results is not None else [{"__gc_cnt": 0}]
        self._exc = exc
        self.calls = []

    def execute(self, kb_id, sql, max_rows=1000, timeout_s=10):
        self.calls.append({"kb_id": kb_id, "sql": sql})
        if self._exc is not None:
            raise self._exc
        return {"rows": list(self._results), "row_count": len(self._results)}


def test_table_executor_returns_none_without_single_bound_kb(monkeypatch) -> None:
    import app.routers.agents as agents_mod

    with monkeypatch.context() as m:
        m.setattr(
            "app.services.db.query_service.QueryService", _FakeQueryService,
        )
        assert agents_mod._make_goal_contract_table_executor(None, None) is None
        assert agents_mod._make_goal_contract_table_executor(None, []) is None
        assert (
            agents_mod._make_goal_contract_table_executor(None, ["kb1", "kb2"]) is None
        )
        assert agents_mod._make_goal_contract_table_executor(None, [""]) is None


def test_table_executor_probes_count_and_memoizes(monkeypatch) -> None:
    import app.routers.agents as agents_mod

    fake = _FakeQueryService(results=[{"__gc_cnt": 12}])
    with monkeypatch.context() as m:
        m.setattr("app.services.db.query_service.QueryService", lambda db: fake)
        probe = agents_mod._make_goal_contract_table_executor(object(), ["kb-1"])
        assert probe is not None
        assert probe("orders") == "12 row(s)"
        assert probe("orders") == "12 row(s)"  # memoized → still one query
        assert len(fake.calls) == 1
        assert "COUNT(*)" in fake.calls[0]["sql"] and "orders" in fake.calls[0]["sql"]
        assert fake.calls[0]["kb_id"] == "kb-1"


def test_table_executor_rejects_unsafe_identifiers(monkeypatch) -> None:
    import app.routers.agents as agents_mod

    fake = _FakeQueryService()
    with monkeypatch.context() as m:
        m.setattr("app.services.db.query_service.QueryService", lambda db: fake)
        probe = agents_mod._make_goal_contract_table_executor(object(), ["kb-1"])
        # Injection attempts must never reach the DB.
        for bad in ["orders; DROP TABLE x", "a--b", "`orders`", '"orders"']:
            assert probe(bad) == ""
        assert fake.calls == []


def test_table_executor_degrades_on_query_failure(monkeypatch) -> None:
    import app.routers.agents as agents_mod

    fake = _FakeQueryService(exc=RuntimeError("conn refused"))
    with monkeypatch.context() as m:
        m.setattr("app.services.db.query_service.QueryService", lambda db: fake)
        probe = agents_mod._make_goal_contract_table_executor(object(), ["kb-1"])
        assert probe is not None
        assert probe("orders") == ""
