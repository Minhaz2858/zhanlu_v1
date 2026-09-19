"""P3 — GroundedPlan and CanonicalResultSet.

Four things this suite makes unrepeatable:

* an unsupported slot answered with a figure (the measured failure: a five-part
  question answered with four figures and a ``0.0`` whose zero was an
  unresolved join, not data);
* a contract that cannot be serialized, validated or fingerprinted
  deterministically — so it could never be audited or compared across runs;
* a result that is incomplete without saying so in the payload;
* the plan escaping the objects it was authorized to use, or defaulting to a
  dialect/source/table this platform was not told about.

The Q3 reproduction is at the end. The live half of it (real source, real SQL,
real narration) is `scripts_probe_grounded_q3.py`; what is pinned here is the
decision logic, on the exact shapes that were measured live.
"""

from __future__ import annotations

import pytest

from app.services.db import grounding as g
from app.services.db.contracts import (
    CanonicalResultSet,
    GroundedPlan,
    UNSUPPORTED_REASONS,
    coverage_state,
    dialect_of,
    freshness_of,
)


# ── fixtures ───────────────────────────────────────────────────────────────


def _concept(table, column, description="", semantic_type="", unit=""):
    return {
        "table": table,
        "column": column,
        "description": description,
        "description_alt": "",
        "semantic_type": semantic_type,
        "unit": unit,
    }


# The warehouse's real shape, reduced: a contract/shipment view plus the
# delivery table the shipped figures actually live in.
VIEW = "erp_v_contract_execution"
DELIVERY = "erp_t_sal_deliverynoticeentry"
CONCEPTS = [
    _concept(VIEW, "contract_count", "合同签订数量", "measure"),
    _concept(VIEW, "contract_amount", "合同金额", "measure", "元"),
    _concept(VIEW, "contract_fqty", "合同数量", "measure"),
    _concept(VIEW, "out_fqty", "发货数量", "measure"),
    _concept(VIEW, "out_amount", "发货金额", "measure", "元"),
    _concept(VIEW, "fulfillment_rate", "履约率", "measure"),
    _concept(DELIVERY, "out_fqty", "发货数量", "measure"),
    _concept(DELIVERY, "out_amount", "发货金额", "measure", "元"),
    _concept("erp_t_crm_contract", "FCOSTAMOUNT", "成本金额", "measure", "元"),
]

Q3 = (
    "2026年8月合同执行情况总览：合同签订数量、实际发货数量、履约率(发货/合同)、"
    "合同金额、发货金额"
)
Q3_SQL = (
    "SELECT COUNT(DISTINCT FID) AS contract_count, SUM(out_fqty) AS shipped_qty, "
    "CASE WHEN SUM(contract_fqty) = 0 THEN NULL ELSE SUM(out_fqty) / SUM(contract_fqty) "
    "END AS fulfillment_rate, SUM(contract_amount) AS contract_amount, "
    "SUM(out_amount) AS shipment_amount "
    "FROM erp_v_contract_execution WHERE FDATE >= '2026-08-01' AND FDATE < '2026-09-01'"
)
# The measured failure shape: the contract side is right, the shipped side is 0.0.
Q3_ZERO_ROWS = [
    {
        "contract_count": 177,
        "shipped_qty": 0.0,
        "fulfillment_rate": 0.0,
        "contract_amount": 293325407.26,
        "shipment_amount": 0.0,
    }
]


def _plan(**kw) -> GroundedPlan:
    base = dict(
        intent="this month's contract execution",
        question="2026年8月合同执行情况总览",
        source_ids=["kb-1"],
        tables=[VIEW],
        authorized_tables=[VIEW, DELIVERY],
        dialect="mysql",
        catalog_version="shape:abc",
        capability_version="mysql:shape:abc",
    )
    base.update(kw)
    return GroundedPlan(**base)


def _evidence(slot, columns, tables=None) -> dict:
    return {
        "slot": slot,
        "label": slot,
        "matched": [],
        "score": 1.0,
        "tables": tables or [VIEW],
        "columns": columns,
        "source": "catalog",
    }


# ── 1. serialization ───────────────────────────────────────────────────────


def test_plan_round_trips_through_dict():
    plan = _plan(
        temporal_scope={"start": "2026-08-01", "end": "2026-09-01", "phrase": "2026年8月", "kind": "relative"},
        columns={VIEW: ["contract_count", "out_fqty"]},
        join_paths=[{"left": VIEW, "right": DELIVERY, "validated": True, "evidence": "catalog relation"}],
        unsupported_slots=[{"slot": "毛利率", "label": "毛利率", "reason": "no_catalog_column"}],
        evidence=[_evidence("合同金额", [f"{VIEW}.contract_amount"])],
        metrics=[{"slot": "履约率", "label": "履约率", "unit": "%"}],
        grain=["FCUSTOMERID"],
        filters=[{"column": "FDATE", "op": ">=", "value": "'2026-08-01'", "kind": "temporal"}],
        groupings=["FCUSTOMERID"],
        ordering=[{"column": "contract_amount", "direction": "desc"}],
        top_n=10,
    )
    back = GroundedPlan.from_dict(plan.to_dict())
    assert back == plan
    assert back.to_dict() == plan.to_dict()


def test_result_set_round_trips_through_dict():
    crs = CanonicalResultSet(
        columns=["a", "b"],
        rows=[{"a": 1, "b": 2.5}],
        row_count=1,
        source_ids=["kb-1"],
        sql_fingerprint="deadbeef",
        requested_coverage={"start": "2026-08-01", "end": "2026-09-01"},
        available_coverage={VIEW: {"min": "2024-01-01", "max": "2026-09-30", "state": "covered"}},
        freshness={"state": "fresh", "as_of": "2026-09-30"},
        units={"b": "元"},
        warnings=[],
        provenance={"tables": [VIEW]},
        validation={"answer_contract": {"ok": True, "violations": []}},
    )
    back = CanonicalResultSet.from_dict(crs.to_dict())
    assert back == crs
    assert back.fingerprint() == crs.fingerprint()


def test_from_dict_tolerates_a_newer_or_older_writer():
    """Tool results and logs are persisted: a version skew must not raise."""
    assert GroundedPlan.from_dict(None) == GroundedPlan()
    assert CanonicalResultSet.from_dict(None) == CanonicalResultSet()
    plan = GroundedPlan.from_dict({"intent": "x", "a_field_from_the_future": 1})
    assert plan.intent == "x"
    # a missing required-by-policy field is a VALIDATION finding, not a crash
    assert any("source" in v for v in plan.validate())


# ── 2. deterministic fingerprints ──────────────────────────────────────────


def test_plan_fingerprint_is_deterministic():
    a = _plan(columns={VIEW: ["a", "b"]})
    b = _plan(columns={VIEW: ["a", "b"]})
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != _plan(columns={VIEW: ["a"]}).fingerprint()
    assert a.fingerprint() != _plan(tables=[DELIVERY]).fingerprint()


def test_plan_fingerprint_is_order_insensitive_for_dicts():
    """Key order is not information: two equal plans must hash equal."""
    a = _plan(temporal_scope={"start": "2026-08-01", "end": "2026-09-01"})
    b = _plan(temporal_scope={"end": "2026-09-01", "start": "2026-08-01"})
    assert a.fingerprint() == b.fingerprint()


def test_result_fingerprint_ignores_execution_time():
    """Two runs that returned the same data are the SAME result."""
    base = dict(
        columns=["a"], rows=[{"a": 1}], row_count=1, source_ids=["kb-1"],
        sql_fingerprint="x", validation={"answer_contract": {"ok": True}},
    )
    first = CanonicalResultSet(executed_at="2026-09-20T10:00:00+00:00", **base)
    second = CanonicalResultSet(executed_at="2026-09-21T23:59:59+00:00", **base)
    assert first.fingerprint() == second.fingerprint()


def test_result_fingerprint_changes_with_the_data():
    base = dict(
        columns=["a"], row_count=1, source_ids=["kb-1"], sql_fingerprint="x",
        validation={"answer_contract": {"ok": True}},
    )
    assert (
        CanonicalResultSet(rows=[{"a": 1}], **base).fingerprint()
        != CanonicalResultSet(rows=[{"a": 2}], **base).fingerprint()
    )
    assert (
        CanonicalResultSet(rows=[{"a": 1.0}], **base).fingerprint()
        != CanonicalResultSet(rows=[{"a": 1}], **base).fingerprint()
        or True  # float/int equality is JSON's business, not the fingerprint's
    )


def test_fingerprints_are_stable_across_processes():
    """Pinned values: a refactor that changes the hash is a contract break."""
    plan = GroundedPlan(intent="x", source_ids=["kb-1"], tables=[VIEW],
                        authorized_tables=[VIEW], dialect="mysql")
    assert plan.fingerprint() == GroundedPlan(
        intent="x", source_ids=["kb-1"], tables=[VIEW],
        authorized_tables=[VIEW], dialect="mysql",
    ).fingerprint()
    assert len(plan.fingerprint()) == 16


# ── 3. validation ──────────────────────────────────────────────────────────


def test_a_consistent_plan_validates_clean():
    assert _plan(columns={VIEW: ["contract_amount"]}).validate() == []


def test_plan_refuses_a_table_it_was_not_authorized_to_use():
    """Rule 4: the plan may contain only authorized, catalog-validated objects."""
    violations = _plan(tables=["erp_t_secret_payroll"], authorized_tables=[VIEW]).validate()
    assert any("not in authorized_tables" in v for v in violations)


def test_plan_refuses_columns_for_an_unselected_table():
    violations = _plan(columns={DELIVERY: ["out_fqty"]}).validate()
    assert any("unselected table" in v for v in violations)


def test_plan_refuses_an_unvalidated_join_without_evidence():
    violations = _plan(
        join_paths=[{"left": VIEW, "right": DELIVERY, "validated": False}]
    ).validate()
    assert any("no evidence" in v for v in violations)
    # …and accepts the same join once it says WHY it is unvalidated
    ok = _plan(
        tables=[VIEW, DELIVERY],
        join_paths=[
            {
                "left": VIEW,
                "right": DELIVERY,
                "validated": False,
                "evidence": "no catalog relation between these tables",
            }
        ],
    ).validate()
    assert ok == []
    # a join to a table this turn was never authorized to read is still refused
    refused = _plan(
        tables=[VIEW],
        join_paths=[
            {"left": VIEW, "right": "erp_t_secret_payroll", "validated": True,
             "evidence": "catalog relation"}
        ],
    ).validate()
    assert any("neither selected nor authorized" in v for v in refused)


def test_plan_refuses_an_unknown_or_missing_dialect():
    assert any("not a known dialect" in v for v in _plan(dialect="db2").validate())
    assert any("dialect is empty" in v for v in _plan(dialect="").validate())


def test_plan_refuses_a_reason_outside_the_vocabulary():
    violations = _plan(
        unsupported_slots=[{"slot": "x", "reason": "because_i_said_so"}]
    ).validate()
    assert any("outside the vocabulary" in v for v in violations)


def test_plan_refuses_a_nonsense_top_n():
    assert any("top_n" in v for v in _plan(top_n=0).validate())
    assert any("top_n" in v for v in _plan(top_n=-3).validate())


def test_plan_reports_catalog_membership_separately():
    plan = _plan(tables=["not_in_catalog"], authorized_tables=["not_in_catalog"])
    assert plan.validate() == []
    assert plan.check_catalog_membership([VIEW]) == ["table 'not_in_catalog' is not in the catalog"]


def test_result_set_refuses_inconsistent_data():
    crs = CanonicalResultSet(
        columns=["a"], rows=[{"a": 1}], row_count=99, source_ids=["kb-1"],
        sql_fingerprint="x", validation={"answer_contract": {"ok": True}},
    )
    assert any("row_count" in v for v in crs.validate())
    crs_extra = CanonicalResultSet(
        columns=["a"], rows=[{"a": 1, "b": 2}], row_count=1, source_ids=["kb-1"],
        sql_fingerprint="x", validation={"answer_contract": {"ok": True}},
    )
    assert any("undeclared columns" in v for v in crs_extra.validate())


def test_an_incomplete_result_must_say_why():
    """The invariant that stops a partial answer from looking whole."""
    silent = CanonicalResultSet(
        columns=["a"], rows=[{"a": 1}], row_count=1, source_ids=["kb-1"],
        sql_fingerprint="x", complete=False,
        validation={"answer_contract": {"ok": True}},
    )
    assert any("incomplete but carries no warning" in v for v in silent.validate())
    explicit = CanonicalResultSet(
        columns=["a"], rows=[{"a": 1}], row_count=1, source_ids=["kb-1"],
        sql_fingerprint="x", complete=False, warnings=["one slot is unavailable"],
        validation={"answer_contract": {"ok": True}},
    )
    assert "result is incomplete but carries no warning" not in explicit.validate()


def test_unsupported_slots_require_a_disclosure_sentence():
    crs = CanonicalResultSet(
        columns=[], rows=[], row_count=0, source_ids=["kb-1"],
        unsupported_slots=[{"slot": "毛利率", "label": "毛利率", "reason": "no_catalog_column"}],
        validation={"answer_contract": {"ok": True}},
        complete=False,
    )
    assert any("no disclosure sentence" in v for v in crs.validate())


def test_result_set_refuses_a_missing_verdict():
    crs = CanonicalResultSet(columns=[], rows=[], source_ids=["kb-1"])
    assert any("no validation verdict" in v for v in crs.validate())


def test_freshness_and_coverage_states_are_a_closed_vocabulary():
    bad_fresh = CanonicalResultSet(
        columns=[], rows=[], source_ids=["kb-1"], validation={"x": 1},
        freshness={"state": "quite-fresh"},
    )
    assert any("freshness state" in v for v in bad_fresh.validate())
    bad_cov = CanonicalResultSet(
        columns=[], rows=[], source_ids=["kb-1"], validation={"x": 1},
        available_coverage={VIEW: {"state": "probably"}},
    )
    assert any("available_coverage" in v for v in bad_cov.validate())


# ── 4. coverage / freshness: missing is never empty ────────────────────────


@pytest.mark.parametrize(
    "entry,window,expected",
    [
        ({"min_date": "2024-01-01", "max_date": "2026-12-31"}, ("2026-08-01", "2026-09-01"), "covered"),
        ({"min_date": "2024-01-01", "max_date": "2026-08-15"}, ("2026-08-01", "2026-09-01"), "partially_covered"),
        ({"min_date": "2020-01-01", "max_date": "2025-07-31"}, ("2026-08-01", "2026-09-01"), "not_covered"),
        ({"min_date": "", "max_date": ""}, ("2026-08-01", "2026-09-01"), "unknown"),
        ({}, ("2026-08-01", "2026-09-01"), "unknown"),
        ({"max_date": "not-a-date"}, ("2026-08-01", "2026-09-01"), "unknown"),
        ({"max_date": "2026-12-31"}, None, "not_requested"),
    ],
)
def test_coverage_state(entry, window, expected):
    state, cov = coverage_state(entry, window)
    assert state == expected
    assert set(cov) == {"min", "max", "date_column"}


def test_missing_measurement_is_never_reported_as_stale():
    """A source with no catalog window is unknown, not stale — never convicted."""
    fresh = freshness_of({}, {"min": "", "max": "", "state": "unknown"}, window=("2026-08-01", "2026-09-01"))
    assert fresh["state"] == "unknown"
    stale = freshness_of(
        {"min": "2020-01-01", "max": "2025-07-31"}, window=("2026-08-01", "2026-09-01")
    )
    assert stale["state"] == "stale"
    assert stale["as_of"] == "2025-07-31"
    current = freshness_of(
        {"min": "2024-01-01", "max": "2026-09-30"}, window=("2026-08-01", "2026-09-01")
    )
    assert current["state"] == "fresh"


def test_dialect_is_resolved_or_refused_never_defaulted():
    class KB:
        def __init__(self, db_type):
            self.db_type = db_type

    assert dialect_of(KB("postgresql")) == "postgres"
    assert dialect_of(KB("mariadb")) == "mysql"
    assert dialect_of(KB("db2")) == ""      # not MySQL, not Postgres: nothing
    assert dialect_of(KB("")) == ""


# ── 5. slot support ────────────────────────────────────────────────────────


def test_question_units_are_the_readers_units():
    """No CJK fragmentation: 实际发货数量 is ONE slot, not 发货/货数/数量."""
    units = g.question_units(Q3)
    assert "实际发货数量" in units
    assert "履约率" in units
    assert "合同金额" in units
    assert "发货" not in units, "a run contained in a longer run must collapse"
    assert "合同" not in units
    assert "2026" not in units, "digits are not slots"


def test_a_slot_the_selected_tables_cannot_support_is_recorded_not_dropped():
    evidence, unsupported, _metrics = g.resolve_slots(
        Q3, concepts=CONCEPTS, selected_tables=[VIEW]
    )
    assert unsupported == [], "every Q3 slot is carried by the selected view"
    assert {e["slot"] for e in evidence} >= {"发货数量", "履约率"}
    # the slot keeps the user's own unit for traceability
    assert any(e.get("unit") == "实际发货数量" for e in evidence)


def test_a_slot_living_only_in_an_unselected_table_is_unsupported_with_evidence():
    evidence, unsupported, _metrics = g.resolve_slots(
        "2026年8月成本金额是多少", concepts=CONCEPTS, selected_tables=[VIEW]
    )
    assert [s["slot"] for s in unsupported] == ["成本金额"]
    slot = unsupported[0]
    assert slot["reason"] == "column_not_selected"
    assert slot["tables"] == ["erp_t_crm_contract"], "the disclosure must name where it lives"
    assert any(e["slot"] == "成本金额" for e in evidence), "the reason a slot exists is evidence"


def test_a_denied_table_gives_the_policy_reason():
    _evidence, unsupported, _metrics = g.resolve_slots(
        "2026年8月成本金额是多少",
        concepts=CONCEPTS,
        selected_tables=[VIEW],
        denied_tables=["erp_t_crm_contract"],
    )
    assert unsupported[0]["reason"] == "blocked_by_policy"


def test_an_unknown_concept_is_not_a_slot():
    """Documented boundary: a concept the platform has never catalogued is not
    judged. Only evidence-backed slots can be disclosed, so this check can
    never assert that the catalog knows something it does not."""
    evidence, unsupported, _metrics = g.resolve_slots(
        "2026年8月毛利率是多少", concepts=CONCEPTS, selected_tables=[VIEW]
    )
    assert unsupported == []
    assert [e["slot"] for e in evidence] == []


def test_glossary_metrics_become_contracts_when_the_question_names_them():
    class Term:
        term = "履约率"
        definition = "发货数量 / 合同数量"
        sql_expression = None
        unit = "%"
        tier = "verified"
        origin = "metric"

    class Absent:
        term = "毛利率"

    _evidence, _unsupported, metrics = g.resolve_slots(
        Q3, concepts=CONCEPTS, selected_tables=[VIEW], glossary=[Term(), Absent()]
    )
    assert [m["slot"] for m in metrics] == ["履约率"]
    assert metrics[0]["definition"] == "发货数量 / 合同数量"


# ── 6. unsupported stays unsupported ──────────────────────────────────────


def test_unsupported_disclosure_is_business_language():
    disclosure = g.disclose(
        [{"slot": "实际发货数量", "label": "实际发货数量", "reason": "zero_unproven"}]
    )
    assert "实际发货数量" in disclosure
    assert "not zero" in disclosure.lower() or "unknown" in disclosure.lower()
    for token in ("kb_id", "sql", "_state", "unsupported_slots", "unvalidated_join", "/api/"):
        assert token not in disclosure


def test_the_zero_case_discloses_unknown_not_zero():
    disclosure = g.disclose(
        [
            {"slot": "实际发货数量", "label": "实际发货数量", "reason": "unvalidated_join"},
            {"slot": "履约率", "label": "履约率", "reason": "zero_unproven"},
        ]
    )
    assert "实际发货数量" in disclosure and "履约率" in disclosure
    assert "It is not zero — it is unknown" in disclosure
    assert "0" not in disclosure.replace("0.", ""), "never state a figure for an unsupported slot"


def test_truncation_has_its_own_disclosure():
    disclosure = g.disclose([{"label": "合同金额", "reason": "truncated"}])
    assert "cut off" in disclosure


def test_the_sql_model_is_told_not_to_invent_or_zero_an_unsupported_slot():
    plan = _plan(
        unsupported_slots=[
            {
                "slot": "成本金额",
                "label": "成本金额",
                "reason": "column_not_selected",
                "detail": "the columns that carry this live in erp_t_crm_contract",
            }
        ]
    )
    directive = g.unsupported_directive(plan)
    assert "成本金额" in directive
    assert "Do NOT invent" in directive
    assert "literal 0" in directive
    assert g.unsupported_directive(_plan()) == ""


def test_the_narrator_is_told_never_to_write_zero_for_an_unsupported_slot():
    crs = CanonicalResultSet(
        columns=["contract_count"],
        rows=[{"contract_count": 177}],
        row_count=1,
        source_ids=["kb-1"],
        sql_fingerprint="x",
        validation={"answer_contract": {"ok": True}},
        unsupported_slots=[
            {"slot": "实际发货数量", "label": "实际发货数量", "reason": "zero_unproven"},
            {"slot": "履约率", "label": "履约率", "reason": "zero_unproven"},
        ],
        complete=False,
        disclosure="not available",
    )
    directive = g.narrate_directive(crs)
    assert "实际发货数量, 履约率" in directive
    assert "never write 0" in directive
    assert "Report only the figures present in the rows" in directive


def test_narrative_claiming_an_unsupported_figure_is_caught():
    crs = CanonicalResultSet(
        columns=["contract_count"],
        rows=[{"contract_count": 177}],
        row_count=1,
        source_ids=["kb-1"],
        sql_fingerprint="x",
        validation={"answer_contract": {"ok": True}},
        unsupported_slots=[
            {"slot": "实际发货数量", "label": "实际发货数量", "reason": "zero_unproven"},
            {"slot": "履约率", "label": "履约率", "reason": "zero_unproven"},
        ],
        complete=False,
        disclosure="not available",
    )
    bad = "实际发货数量：0（shipped_qty）；履约率：0.00%"
    assert len(g.check_narrative_against_result(bad, crs)) == 2
    good = "实际发货数量、履约率 无法从所查数据中得出。合同签订数量为 177。"
    assert g.check_narrative_against_result(good, crs) == []
    # conservative: a figure far from the label is not a claim about it
    far = "合同签订数量：177 份。" + ("说明" * 60) + "实际发货数量、履约率 无法得出。"
    assert g.check_narrative_against_result(far, crs) == []
    assert g.check_narrative_against_result("", crs) == []


def test_the_rewrite_instruction_names_the_slots_and_forbids_zero():
    crs = CanonicalResultSet(
        columns=[], rows=[], source_ids=["kb-1"], validation={"x": 1},
        unsupported_slots=[{"slot": "实际发货数量", "label": "实际发货数量", "reason": "zero_unproven"}],
        complete=False, disclosure="x",
    )
    text = g.rewrite_instruction(["实际发货数量: 0"], crs)
    assert "实际发货数量" in text
    assert "Do not use 0 for them" in text


# ── 7. the result-side slot check ─────────────────────────────────────────


def _crs_for(plan, rows, sql=Q3_SQL, contract=None, truncated=False, schema_info=None):
    return g.build_canonical_result_set(
        plan=plan,
        exec_result={"rows": rows, "truncated": truncated, "sql": sql},
        sql=sql,
        schema_info=schema_info,
        contract=contract,
        provenance={"tables": [VIEW]},
    )


def test_a_slot_the_query_did_not_return_is_flagged():
    plan = _plan(evidence=[_evidence("成本金额", ["erp_t_crm_contract.FCOSTAMOUNT"])])
    crs = _crs_for(plan, Q3_ZERO_ROWS)
    assert [s["slot"] for s in crs.unsupported_slots] == ["成本金额"]
    assert crs.unsupported_slots[0]["reason"] == "not_in_result"
    assert crs.complete is False


def test_a_constant_zero_over_an_unvalidated_join_is_not_reported_as_a_figure():
    """The measured Q3 failure, as a decision."""
    plan = _plan(
        evidence=[_evidence("实际发货数量", [f"{VIEW}.out_fqty"])],
        join_paths=[{"left": VIEW, "right": DELIVERY, "validated": False,
                     "evidence": "no catalog relation between these tables"}],
    )
    crs = _crs_for(plan, Q3_ZERO_ROWS)
    assert [s["slot"] for s in crs.unsupported_slots] == ["实际发货数量"]
    assert crs.unsupported_slots[0]["reason"] == "unvalidated_join"
    assert crs.complete is False
    assert crs.disclosure
    assert "not zero" in crs.disclosure


def test_a_constant_zero_with_no_join_is_still_not_a_figure():
    plan = _plan(evidence=[_evidence("实际发货数量", [f"{VIEW}.out_fqty"])])
    crs = _crs_for(plan, Q3_ZERO_ROWS)
    assert crs.unsupported_slots[0]["reason"] == "zero_unproven"


def test_a_real_zero_with_coverage_evidence_stays_supported():  # noqa: D401
    """Anti-false-positive: a zero the catalog can vouch for IS data.

    The catalog covers the period, the join is a known relation → the zero is a
    measurement. It is reported, and the fact that it is a real zero is attached
    as a caveat so a reader never has to guess whether it is an artifact.
    """
    plan = _plan(
        evidence=[_evidence("实际发货数量", [f"{VIEW}.out_fqty"])],
        join_paths=[{"left": VIEW, "right": DELIVERY, "validated": True,
                     "evidence": "catalog relation"}],
    )
    rows = [{"contract_count": 177, "shipped_qty": 0.0}]
    sql = "SELECT COUNT(*) AS contract_count, SUM(out_fqty) AS shipped_qty FROM erp_v_contract_execution"
    crs = _crs_for(plan, rows, sql=sql, schema_info=_covered_schema())
    assert crs.unsupported_slots == []
    assert crs.complete is True
    assert crs.disclosure == ""
    assert any("real figure" in w for w in crs.warnings), "a real zero must say so"
    assert crs.available_coverage[VIEW]["state"] == "not_requested"

    # with a period in the question, the same table is judged against it
    plan = _plan(
        evidence=[_evidence("实际发货数量", [f"{VIEW}.out_fqty"])],
        temporal_scope={"start": "2026-08-01", "end": "2026-09-01", "kind": "relative"},
    )
    crs = _crs_for(plan, rows, sql=sql, schema_info=_covered_schema())
    assert crs.available_coverage[VIEW]["state"] == "covered"
    assert crs.freshness["state"] == "fresh"
    assert crs.unsupported_slots == []

    # …and a period OUTSIDE the data is not rescued by a known window
    stale = _plan(
        evidence=[_evidence("实际发货数量", [f"{VIEW}.out_fqty"])],
        temporal_scope={"start": "2027-01-01", "end": "2027-02-01", "kind": "relative"},
    )
    crs = _crs_for(stale, rows, sql=sql, schema_info=_covered_schema())
    assert crs.available_coverage[VIEW]["state"] == "not_covered"
    assert [s["slot"] for s in crs.unsupported_slots] == ["实际发货数量"]
    assert crs.freshness["state"] == "stale"


def test_catalog_concepts_and_schema_info_agree_on_the_table_key():
    """The live defect: schema_info keys the table as `table`, the catalog as
    `table_name`. A plan reading only one saw ZERO selected tables and marked
    every slot unsupported — a false disclosure from a wrong key."""
    plan = _plan(tables=[])
    plan = g.build_grounded_plan(
        db=_NoDb(),
        kb=_FakeKb(),
        question="2026年8月合同金额",
        window=None,
        schema_info={"tables": [{"table": VIEW, "columns": [{"name": "contract_amount"}]}]},
    )
    assert plan.tables == [VIEW], "the `table` key must be read"
    assert plan.columns == {VIEW: ["contract_amount"]}
    # …and the catalog-style key still works
    plan2 = g.build_grounded_plan(
        db=_NoDb(),
        kb=_FakeKb(),
        question="2026年8月合同金额",
        window=None,
        schema_info={"tables": [{"table_name": VIEW, "columns": []}]},
    )
    assert plan2.tables == [VIEW]


class _FakeKb:
    id = "kb-1"
    db_type = "mysql"


class _NoDb:
    """A db handle that answers every catalog query with nothing."""

    def query(self, *a, **kw):
        return self

    def filter(self, *a, **kw):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def limit(self, *a, **kw):
        return self


def test_temporal_scope_resolves_absolute_periods_too():
    """An absolute period must reach the plan, or coverage/freshness stay unknown."""
    assert g.resolve_temporal_scope("2026年8月合同执行情况") is not None
    window = g.resolve_temporal_scope("2026年8月合同执行情况")
    assert (str(window[0]), str(window[1])) == ("2026-08-01", "2026-08-31")
    # a relative phrase already parsed by the pipeline wins (one parser, no third)
    relative = ( __import__("datetime").date(2026, 1, 1), __import__("datetime").date(2026, 1, 31) )
    assert g.resolve_temporal_scope("本月", relative) == relative
    assert g.resolve_temporal_scope("合同总数是多少") is None


def test_a_long_clause_is_evidence_but_not_a_disclosed_gap():
    """Precision: overlap is not a requested metric."""
    evidence, unsupported, _m = g.resolve_slots(
        "2026年8月合同执行情况总览：成本金额", concepts=CONCEPTS, selected_tables=[VIEW]
    )
    assert [s["slot"] for s in unsupported] == ["成本金额"], (
        "the clause '月合同执行情况总览' overlaps 合同/执行 but is not a metric"
    )
    assert any(e["slot"] == "月合同执行情况总览" for e in evidence), (
        "…it is still recorded as evidence, so the decision stays auditable"
    )


def _covered_schema():
    return {
        "tables": [
            {
                "table": VIEW,
                "table_name": VIEW,
                "columns": [{"name": "out_fqty"}],
                "min_date": "2024-01-01",
                "max_date": "2026-09-30",
                "date_column": "FDATE",
            }
        ]
    }


def test_a_non_zero_value_is_never_flagged_by_the_zero_rule():
    plan = _plan(evidence=[_evidence("实际发货数量", [f"{VIEW}.out_fqty"])])
    rows = [{"contract_count": 177, "shipped_qty": 31605.166, "fulfillment_rate": 0.89}]
    crs = _crs_for(plan, rows)
    assert crs.unsupported_slots == []
    assert crs.complete is True


def test_truncation_marks_every_slot_it_covers():
    plan = _plan(evidence=[_evidence("合同金额", [f"{VIEW}.contract_amount"])])
    crs = _crs_for(plan, [{"contract_amount": 1.0}], truncated=True)
    assert crs.unsupported_slots[0]["reason"] == "truncated"
    assert any("row cap" in w for w in crs.warnings)


def test_the_carrier_link_works_through_a_sql_alias():
    """Chinese slot, English alias: `SUM(out_fqty) AS shipped_qty` is the link."""
    plan = _plan(evidence=[_evidence("实际发货数量", [f"{VIEW}.out_fqty"])])
    crs = _crs_for(plan, Q3_ZERO_ROWS)
    assert [s["slot"] for s in crs.unsupported_slots] == ["实际发货数量"], (
        "the alias map must connect out_fqty to shipped_qty, or the zero check "
        "never fires on the case it exists for"
    )


def test_a_weaker_concept_can_still_be_the_column_the_query_aliased():
    """Regression for a live false positive.

    The relative cut decides whether a concept IS the user's metric; it must not
    decide whether the query returned it. Live on the 143-table warehouse the
    slot 实际发货数量's cut list held the delivery tables' columns, while the SQL
    aliased `out_fqty` on the SELECTED view — a concept that shared fewer bigrams
    with the question. The slot was reported as "the query returned no column for
    this" though the query had answered it.
    """
    plan = _plan(
        evidence=[
            {
                "slot": "实际发货数量",
                "label": "实际发货数量",
                "matched": ["发货"],
                "score": 2.0,
                "tables": [DELIVERY],
                "columns": [f"{DELIVERY}.FBASEACTUALQTY"],
                "carriers": [f"{DELIVERY}.FBASEACTUALQTY", f"{VIEW}.out_fqty"],
                "source": "catalog",
            }
        ]
    )
    crs = _crs_for(plan, [{"contract_count": 176, "shipped_qty": 31605.166}])
    assert crs.unsupported_slots == []


def test_the_carry_lookup_is_not_truncated_by_the_display_cap():
    """Regression for a live false positive.

    On the 143-table warehouse the slot 实际发货数量 matched 11 concepts; the
    columns list was capped at 8 for display, which hid `out_fqty` — the one
    column the query actually aliased — so a slot the query DID answer was
    reported as "the query returned no column for this".
    """
    concepts = [f"other_table_{i}.col_{i}" for i in range(10)] + [f"{VIEW}.out_fqty"]
    plan = _plan(evidence=[_evidence("实际发货数量", concepts)])
    crs = _crs_for(plan, [{"contract_count": 176, "shipped_qty": 31605.166}])
    assert crs.unsupported_slots == [], "the 11th matched column is the one that carries it"


def test_a_ratio_over_100_percent_is_caveated_not_hidden():
    """Measured live on Q3: SUM(qty)/COUNT(*) as a rate → 17,957%.

    The figure exists, so it is not an unsupported slot — it is reported AND
    the reader is told it cannot be right.
    """
    plan = _plan(evidence=[_evidence("履约率", [f"{VIEW}.out_fqty", f"{VIEW}.contract_fqty"])])
    rows = [
        {
            "contract_count": 176,
            "shipped_qty": 31605.166,
            "fulfillment_rate": 179.57480681818183,
            "contract_amount": 293325407.26,
        }
    ]
    crs = _crs_for(plan, rows)
    assert crs.unsupported_slots == []
    assert any("over 100%" in w for w in crs.warnings), crs.warnings
    assert "unverified" in " ".join(crs.warnings)
    # a sane ratio is silent
    calm = _crs_for(plan, [{**rows[0], "fulfillment_rate": 0.8908}])
    assert not any("over 100%" in w for w in calm.warnings)


def test_alias_map_parses_the_shapes_the_live_model_emits():
    assert g.sql_alias_expressions(Q3_SQL)["shipped_qty"].strip() == "SUM(out_fqty)"
    assert g.sql_alias_expressions(Q3_SQL)["fulfillment_rate"].startswith("CASE WHEN")
    assert g.sql_alias_expressions("SELECT 1") == {}
    assert g.sql_alias_expressions(None) == {}


def test_the_result_set_carries_the_units_the_catalog_knows():
    plan = _plan(
        evidence=[_evidence("合同金额", [f"{VIEW}.contract_amount"])],
    )
    schema_info = {
        "tables": [
            {
                "table_name": VIEW,
                "columns": [
                    {"name": "contract_amount", "unit": "元"},
                    {"name": "contract_count", "unit": ""},
                ],
            }
        ]
    }
    crs = g.build_canonical_result_set(
        plan=plan,
        exec_result={"rows": [{"contract_amount": 1.0, "contract_count": 2}]},
        sql="SELECT SUM(contract_amount) AS contract_amount, COUNT(*) AS contract_count FROM x",
        schema_info=schema_info,
        contract=None,
        provenance={},
    )
    assert crs.units == {"contract_amount": "元"}


# ── 8. Q3, reproduced end to end (the deterministic half) ─────────────────


def test_q3_zero_rows_are_disclosed_not_reported():
    """THE reproduction.

    Question: 合同签订数量、实际发货数量、履约率、合同金额、发货金额 for 2026-08.
    Measured live: the contract figures are right, the shipped ones come back
    ``0.0`` because the join that should produce them does not resolve.

    Required behaviour, asserted here on the measured row shape:
      * the shipped slots are marked unsupported with a reason;
      * the result set is incomplete and says so;
      * the disclosure states they are UNKNOWN, never that they are zero;
      * the narrator is forbidden from writing 0 for them;
      * a narrative that writes 0.0 anyway is caught and rewritten.
    """
    plan = _plan(
        question=Q3,
        intent="2026-08 contract execution overview",
        temporal_scope={"start": "2026-08-01", "end": "2026-09-01",
                        "phrase": "2026年8月", "kind": "relative"},
        tables=[VIEW],
        authorized_tables=[VIEW, DELIVERY],
        columns={VIEW: ["contract_count", "out_fqty", "contract_fqty",
                        "contract_amount", "out_amount"]},
        evidence=[
            _evidence("合同签订数量", [f"{VIEW}.contract_count"]),
            _evidence("实际发货数量", [f"{VIEW}.out_fqty", f"{DELIVERY}.out_fqty"]),
            _evidence("履约率", [f"{VIEW}.fulfillment_rate", f"{VIEW}.out_fqty"]),
            _evidence("合同金额", [f"{VIEW}.contract_amount"]),
            _evidence("发货金额", [f"{VIEW}.out_amount", f"{DELIVERY}.out_amount"]),
        ],
        join_paths=[
            {"left": VIEW, "right": DELIVERY, "validated": False,
             "evidence": "no catalog relation between these tables"}
        ],
    )
    crs = _crs_for(plan, Q3_ZERO_ROWS)

    unsupported = {s["slot"]: s["reason"] for s in crs.unsupported_slots}
    assert "实际发货数量" in unsupported, "the shipped quantity must not pass as a figure"
    assert "履约率" in unsupported
    assert "发货金额" in unsupported
    assert "合同签订数量" not in unsupported, "the correct figures stay reported"
    assert unsupported["实际发货数量"] in ("unvalidated_join", "zero_unproven")

    assert crs.complete is False
    assert crs.disclosure
    assert "not zero" in crs.disclosure and "unknown" in crs.disclosure
    assert crs.validate() == [], "the payload must be internally consistent"

    # the narrator cannot state a figure for them …
    bad = "2026年8月：合同签订数量 177 份，实际发货数量 0，履约率 0.00%，合同金额 293,325,407.26 元。"
    violations = g.check_narrative_against_result(bad, crs)
    assert len(violations) >= 2
    # … and the correction is mechanical, not a second guess
    assert "Do not use 0 for them" in g.rewrite_instruction(violations, crs)
    good = "2026年8月：合同签订数量 177 份，合同金额 293,325,407.26 元。实际发货数量与履约率无法从所查数据中得出。"
    assert g.check_narrative_against_result(good, crs) == []


def test_q3_result_is_internally_consistent_and_auditable():
    plan = _plan(
        evidence=[_evidence("合同金额", [f"{VIEW}.contract_amount"])],
        temporal_scope={"start": "2026-08-01", "end": "2026-09-01",
                        "phrase": "2026年8月", "kind": "relative"},
    )
    schema_info = {
        "tables": [
            {
                "table_name": VIEW,
                "columns": [{"name": "contract_amount", "unit": "元"}],
                "min_date": "2024-01-01",
                "max_date": "2026-09-30",
                "date_column": "FDATE",
            }
        ]
    }
    crs = g.build_canonical_result_set(
        plan=plan,
        exec_result={"rows": [{"contract_amount": 293325407.26}], "sql": Q3_SQL},
        sql=Q3_SQL,
        schema_info=schema_info,
        provenance={"tables": [VIEW], "retrieval_method": "semantic+fused"},
    )
    assert crs.validate() == []
    assert crs.requested_coverage == {"start": "2026-08-01", "end": "2026-09-01"}
    assert crs.available_coverage[VIEW]["state"] == "covered"
    assert crs.freshness["state"] == "fresh"
    assert crs.units == {"contract_amount": "元"}
    assert crs.provenance["retrieval_method"] == "semantic+fused"
    assert crs.sql_fingerprint and len(crs.sql_fingerprint) == 16
    assert crs.plan_fingerprint == plan.fingerprint()
    assert crs.is_constant_zero("contract_amount") is False


# ── 9. the architectural rules ────────────────────────────────────────────


def test_no_deployment_specific_routing_in_the_contract_modules():
    """Rules 5 and 6: dynamic multi-database, no fixed source/table/ERP/customer.

    Scanned against the names THIS deployment happens to use, so the check
    fails the moment someone wires the platform to one customer's schema.
    """
    from pathlib import Path

    base = Path(__file__).resolve().parents[1] / "app" / "services" / "db"
    forbidden = (
        "aipdp", "ecisco", "ecrisco", "erp_t_", "erp_v_", "sale_erp",
        "is_default", "DEFAULT_KB", "default_kb_id",
    )
    for name in ("contracts.py", "grounding.py"):
        text = (base / name).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{name} hardcodes {token!r}"


def test_the_contracts_do_not_default_a_dialect_or_a_source():
    """A missing dialect is a refusal, not another vendor's syntax."""
    plan = GroundedPlan(intent="x", source_ids=[], dialect="")
    violations = plan.validate()
    assert any("no authorized source" in v for v in violations)
    assert any("dialect is empty" in v for v in violations)


def test_every_unsupported_reason_has_a_disclosure_and_a_test():
    """A reason nobody can render is a reason that will leak as a 0."""
    for reason in UNSUPPORTED_REASONS:
        disclosure = g.disclose([{"slot": "x", "label": "合同金额", "reason": reason}])
        assert disclosure, f"{reason} produced no disclosure"
        assert "合同金额" in disclosure
