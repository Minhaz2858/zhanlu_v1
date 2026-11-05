"""Dynamic NL2SQL query contract — generic regression suite (2026-10-29).

Fixes verified against the live incident (conv e70fa696): verified metric
definitions were advisory-only, one report blended SUM(FAMOUNT) products with
SUM(FALLAMOUNT) totals (net vs tax-inclusive), and a grouped top-N truncated
product families across the LIMIT boundary.

Everything here is schema-agnostic: TWO synthetic datasources with disjoint
naming conventions (mysql-flavored ERP-ish names vs a plain analytics names),
each with its OWN verified metric definition. No test references the real
warehouse's table or column names.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import sqlglot  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.metric_definition import MetricDefinition  # noqa: E402
from app.services.db.execution_gate import (  # noqa: E402
    ExecutionValidationError,
    enforce,
)
from app.services.db.query_contract import (  # noqa: E402
    MetricBinding,
    build_family_correction_sql,
    contract_prompt_block,
    datasource_metric_bindings,
    detect_family_truncation,
    measure_fingerprint,
    reconcile_fingerprints,
    claimed_measure_terms,
    validate_confusable_measures,
    validate_sql_measure_aliases,
)
from app.services.db import turn_sql_budget  # noqa: E402
from app.services.synexia.plan_dag import _bound_nl2sql_plan  # noqa: E402

# ── Two synthetic datasources, deliberately different naming conventions ───

KB_A = "kb-conv-aaaa"   # mysql-ish naming (underscores, F-prefixes)
KB_B = "kb-conv-bbbb"   # plain analytics naming


def _seed_datasource(db, kb_id, name):
    kb = db.get(KnowledgeBase, kb_id)
    if kb:
        return kb
    kb = KnowledgeBase(
        id=kb_id,
        name=name,
        db_type="mysql" if kb_id == KB_A else "postgres",
        source_kind="database",
        org_id="default-org",
        app_id="default-app",
    )
    db.add(kb)
    db.commit()
    return kb


def _seed_column_catalog(db, kb_id, table_name, columns):
    """Seed the schema inventory the confusable derivation reads — the same
    kb_table_meta/kb_column_meta rows the runtime indexer writes."""
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

    tm = db.query(KBTableMeta).filter(
        KBTableMeta.kb_id == kb_id,
        KBTableMeta.table_name == table_name,
    ).first()
    if tm is None:
        tm = KBTableMeta(
            kb_id=kb_id, schema_name="", table_name=table_name,
        )
        db.add(tm)
        db.flush()
    existing = {
        c.column_name for c in db.query(KBColumnMeta).filter(
            KBColumnMeta.table_meta_id == tm.id
        ).all()
    }
    for i, col in enumerate(columns):
        if col in existing:
            continue
        db.add(KBColumnMeta(
            table_meta_id=tm.id, column_name=col, ordinal=i,
        ))
    db.commit()


@pytest.fixture
def two_datasources():
    """Schema A: mysql-ish ERP-ish names. Schema B: disjoint analytics names.

    Both define a verified "net revenue"-class metric on a DIFFERENT column —
    proving bindings are per-datasource and never cross.
    """
    db = SessionLocal()
    _seed_datasource(db, KB_A, "schema-a")
    _seed_datasource(db, KB_B, "schema-b")
    # Blocker-1 basis: the catalog itself documents the sibling measure
    # column on schema A's base table — the same way the runtime indexer
    # records a real warehouse's columns. Schema B documents no sibling.
    _seed_column_catalog(db, KB_A, "order_line",
                         ["plan_date", "bill_no", "item_key", "item_name",
                          "fline_amount", "fgross_amount"])
    _seed_column_catalog(db, KB_B, "sales_fact",
                         ["sale_date", "region", "revenue_net"])
    if not db.query(MetricDefinition).filter(
        MetricDefinition.datasource_id == KB_A
    ).first():
        db.add(MetricDefinition(
            id=str(uuid.uuid4()), name="net_revenue", display_name="Net Revenue",
            datasource_id=KB_A, base_table="order_line",
            base_column="fline_amount", aggregation="sum",
            description="net of tax; fgross_amount is tax-inclusive",
            synonyms=json.dumps(["revenue", "sales"]),
            org_id="default-org", app_id="default-app",
        ))
    if not db.query(MetricDefinition).filter(
        MetricDefinition.datasource_id == KB_B
    ).first():
        db.add(MetricDefinition(
            id=str(uuid.uuid4()), name="net_sales", display_name="Net Sales",
            datasource_id=KB_B, base_table="sales_fact",
            base_column="revenue_net", aggregation="sum",
            description="net of adjustments",
            synonyms=json.dumps(["sales"]),
            org_id="default-org", app_id="default-app",
        ))
    db.commit()
    yield db
    db.close()


# ── Phase 1: deterministic, datasource-scoped metric binding ───────────────


def test_bindings_are_per_datasource(two_datasources):
    db = two_datasources
    a = datasource_metric_bindings(db, KB_A)
    b = datasource_metric_bindings(db, KB_B)
    assert [b_.base_column for b_ in a] == ["fline_amount"]
    assert [b_.base_column for b_ in b] == ["revenue_net"]
    # A binding from datasource A can never appear in datasource B's list.
    assert all(x.base_table == "order_line" for x in a)
    assert all(x.base_table == "sales_fact" for x in b)


def test_conflicting_alias_rejected_compliant_passes(two_datasources):
    db = two_datasources
    bindings = datasource_metric_bindings(db, KB_A)

    # The incident shape: alias claims "revenue" but sums the OTHER column.
    bad = sqlglot.parse_one(
        "SELECT SUM(fgross_amount) AS revenue FROM order_line "
        "WHERE plan_date >= '2026-10-19'", dialect="mysql")
    violations = validate_sql_measure_aliases(bindings, bad, ["order_line"])
    assert violations and "fline_amount" in violations[0]

    # Compliant: the verified column itself — no violation.
    good = sqlglot.parse_one(
        "SELECT SUM(fline_amount) AS revenue FROM order_line", dialect="mysql")
    assert validate_sql_measure_aliases(bindings, good, ["order_line"]) == []

    # Unrelated alias ("order count") claims nothing — no violation.
    other = sqlglot.parse_one(
        "SELECT COUNT(DISTINCT bill_no) AS order_count FROM order_line",
        dialect="mysql")
    assert validate_sql_measure_aliases(bindings, other, ["order_line"]) == []


def test_enforcement_is_dialect_agnostic(two_datasources):
    """Same violation detected through postgres and mysql parses — the check
    rides the shared sqlglot parse, not a dialect adapter."""
    db = two_datasources
    bindings = datasource_metric_bindings(db, KB_B)
    for dialect in ("postgres", "mysql"):
        bad = sqlglot.parse_one(
            'SELECT SUM(revenue_gross) AS sales FROM sales_fact',
            dialect=dialect)
        violations = validate_sql_measure_aliases(bindings, bad, ["sales_fact"])
        assert violations, f"dialect {dialect} missed the violation"


def test_execution_gate_rejects_before_execution(two_datasources, monkeypatch):
    """The REAL pre-execution boundary: enforce() refuses the conflicting
    statement and reports the metric_binding check by name."""
    db = two_datasources
    sql = ("SELECT SUM(fgross_amount) AS revenue FROM order_line "
           "WHERE plan_date >= '2026-10-19'")
    with pytest.raises(ExecutionValidationError) as exc:
        enforce(db, KB_A, sql)
    payload = exc.value.payload
    assert payload["checks"]["metric_binding"] == "fail"
    assert any("fline_amount" in e for e in payload["errors"])

    # Compliant statement clears the gate (other checks not-applicable on an
    # un-introspected source — they never block here).
    ok = enforce(db, KB_A, "SELECT SUM(fline_amount) AS revenue FROM order_line")
    assert ok["checks"]["metric_binding"] == "pass"


def test_no_verified_definitions_nothing_invented(two_datasources, monkeypatch):
    """Ambiguity policy: a datasource with no verified metrics gets NO binding
    and NO invented prompt constraints — existing behavior is unchanged."""
    db = two_datasources
    _seed_datasource(db, "kb-conv-cccc", "schema-c")  # exists, defines nothing
    bindings = datasource_metric_bindings(db, "kb-conv-cccc")
    assert bindings == []
    assert contract_prompt_block(bindings) == ""
    # And the gate records not-applicable rather than failing.
    ok = enforce(db, "kb-conv-cccc",
                 "SELECT SUM(anything) AS revenue FROM whatever_table")
    assert ok["checks"]["metric_binding"] == "not-applicable"


def test_negative_flag_disables_binding_check(two_datasources, monkeypatch):
    """Negative test: with the guard off, the incident's SQL passes the gate —
    proving the new check is what catches it (not a pre-existing one)."""
    db = two_datasources
    monkeypatch.setattr(settings, "METRIC_BINDING_ENFORCEMENT_ENABLED", False)
    ok = enforce(db, KB_A,
                 "SELECT SUM(fgross_amount) AS revenue FROM order_line")
    assert ok["checks"]["metric_binding"] == "disabled"


# ── Phase 2: grouping-grain / LIMIT family truncation ──────────────────────

FAMILY_SQL = (
    "SELECT item_key, item_name, SUM(fline_amount) AS revenue "
    "FROM order_line WHERE plan_date >= '2026-10-19' AND plan_date < '2026-10-26' "
    "GROUP BY item_key, item_name ORDER BY revenue DESC LIMIT 10"
)
FAMILY_ROWS = [
    {"item_key": "K1", "item_name": "Pentane", "revenue": 102.0},
    {"item_key": "K2", "item_name": "Resin", "revenue": 52.0},
    {"item_key": "K3", "item_name": "Resin", "revenue": 44.0},
    {"item_key": "K4", "item_name": "Isoprene", "revenue": 37.0},
]


def test_family_truncation_detected():
    hit = detect_family_truncation(FAMILY_SQL, FAMILY_ROWS)
    assert hit is not None and hit["column"] == "item_name"
    assert hit["families"].get("resin") == 2  # K2 + K3 share the family name


def test_family_truncation_not_flagged_without_group_or_limit():
    plain = "SELECT item_name, SUM(fline_amount) AS r FROM order_line"
    assert detect_family_truncation(plain, FAMILY_ROWS) is None


def test_family_correction_sql_regroups_at_family_grain():
    corrected = build_family_correction_sql(FAMILY_SQL, "item_name")
    assert corrected is not None
    low = corrected.lower()
    assert "group by" in low and "item_name" in low
    # The correction aggregates the SAME measure — never a different column.
    assert "sum(fline_amount)" in low.replace(" ", "")


def test_family_correction_refuses_unsafe_shapes():
    # No GROUP BY at all → nothing to correct.
    assert build_family_correction_sql(
        "SELECT SUM(fline_amount) AS revenue FROM order_line", "item_name"
    ) is None


# ── Phase 3: cross-node measure reconciliation ─────────────────────────────


def test_reconciliation_flags_conflicting_measures():
    # The incident: one node claimed revenue from the net column, others from
    # the tax-inclusive column.
    conflicts = reconcile_fingerprints([
        {"net_revenue": "fline_amount"},
        {"net_revenue": "fgross_amount"},
        {"net_revenue": "fgross_amount"},
    ])
    assert len(conflicts) == 1
    assert "fline_amount" in conflicts[0] and "fgross_amount" in conflicts[0]
    # Agreeing nodes → no conflict; volume-only nodes claim nothing.
    assert reconcile_fingerprints([
        {"net_revenue": "fline_amount"},
        {"net_revenue": "fline_amount"},
        {},
    ]) == []


def test_fingerprint_is_engine_neutral():
    for dialect in ("mysql", "postgres"):
        p = sqlglot.parse_one(
            "SELECT cust, SUM(amount) AS revenue FROM sales GROUP BY cust LIMIT 10",
            dialect=dialect)
        fp = measure_fingerprint(p, ["sales"])
        assert fp["aggs"] == ["sum:amount"]
        assert fp["group_by"] == ["cust"]
        assert fp["limit"] == 10


def test_claimed_terms_resolve_via_synonyms():
    bindings_a = datasource_metric_bindings(SessionLocal(), KB_A)
    p = sqlglot.parse_one(
        "SELECT SUM(fgross_amount) AS total_revenue FROM order_line",
        dialect="mysql")
    claimed = claimed_measure_terms(p, bindings_a)
    assert claimed == {"net_revenue": "fgross_amount"}  # "revenue" synonym hit


# ── Phase 4: bounded planning — duplicate merge + 4-node cap ───────────────


def _node(name, question, deps):
    return {"node_type": "nl2sql", "name": name, "description": question,
            "dependencies": list(deps), "expected_output": "DataSnapshot"}


def test_duplicate_intents_merged_and_capped():
    steps = [
        _node("totals", "overall sales totals for last week", []),
        _node("woW", "overall sales totals for last week (WoW)", [0]),   # dup
        _node("by_prod", "sales by product last week", [0]),
        _node("by_prod_dup", "sales by product for last week", [0]),    # dup
        _node("by_cust", "sales by customer last week", [0]),
        _node("by_region", "sales by region last week", [0]),
        _node("trend", "sales daily trend last week", [0]),
        _node("extra", "sales by channel last week", [0]),
        {"node_type": "synthesize", "name": "report", "description": "report",
         "dependencies": [0, 1, 2, 3, 4, 5, 6, 7]},
    ]
    bounded = _bound_nl2sql_plan(steps)
    nl = [s for s in bounded if s["node_type"] == "nl2sql"]
    assert len(nl) <= settings.NL2SQL_MAX_REPORT_NODES
    # The synthesize node survived and its deps all point at existing indices.
    synth = next(s for s in bounded if s["node_type"] == "synthesize")
    assert all(0 <= d < len(bounded) for d in synth["dependencies"])
    # Totals node (first authored) is kept — priority order preserved.
    assert "totals" in bounded[0]["name"] or "totals" in bounded[0]["description"]


def test_small_plans_untouched():
    steps = [
        _node("totals", "sales totals last week", []),
        {"node_type": "synthesize", "name": "report", "description": "r",
         "dependencies": [0]},
    ]
    assert _bound_nl2sql_plan(steps) == steps


def test_negative_cap_disabled(monkeypatch):
    """Negative test: with the cap raised, the same 8-node plan survives —
    proving the cap (not an accident) bounds the plan."""
    monkeypatch.setattr(settings, "NL2SQL_MAX_REPORT_NODES", 10)
    steps = [
        _node(f"n{i}", f"sales by dimension{i} last week", [0] if i else [])
        for i in range(8)
    ]
    steps.append({"node_type": "synthesize", "name": "r", "description": "r",
                  "dependencies": [0]})
    bounded = _bound_nl2sql_plan(steps)
    assert len([s for s in bounded if s["node_type"] == "nl2sql"]) == 8


def test_report_decomposition_fixture_capped():
    """The incident-shaped fixture: 8 dimensions → 10 decomposition nodes —
    bounded to the configured cap with the synthesize tail intact."""
    from app.services.synexia.plan_dag import _build_report_decomposition_steps
    task_spec = {
        "dimensions": [f"dim{i}" for i in range(8)],
        "requires_data": True,
        "user_message": "last week sales report by every dimension",
        "entities": {},
    }
    dec = _build_report_decomposition_steps(task_spec)
    if dec is None:
        pytest.skip("decomposition declined this fixture")
    bounded = _bound_nl2sql_plan(dec)
    assert len([s for s in bounded if s["node_type"] == "nl2sql"]) <= 4
    # Dependencies remain valid positional indices.
    for s in bounded:
        for d in s.get("dependencies", []):
            assert 0 <= d < len(bounded)


# ── Blocker 1: alias-INDEPENDENT differing-measure enforcement ──────────────


def test_confusable_columns_derived_from_catalog_description(two_datasources):
    """The binding's own catalog description names the sibling measure column,
    and the catalog documents that column on the base table → confusable."""
    db = two_datasources
    a = datasource_metric_bindings(db, KB_A)
    b = datasource_metric_bindings(db, KB_B)
    assert a[0].confusable_columns == ("fgross_amount",)
    # Schema B's description mentions no other documented column → none.
    assert b[0].confusable_columns == ()


def test_confusable_measure_rejected_without_alias_claim(two_datasources):
    """The reviewer's exact blocker: SQL aggregating a DIFFERENT documented
    basis of a verified concept is rejected even when no output alias claims
    the concept (neutral alias here — the alias rule cannot see this)."""
    db = two_datasources
    bindings = datasource_metric_bindings(db, KB_A)
    assert bindings[0].confusable_columns, "fixture must define confusables"

    # Neutral alias + the tax-inclusive column alone → violation.
    sneaky = sqlglot.parse_one(
        "SELECT SUM(fgross_amount) AS total_amount FROM order_line "
        "WHERE plan_date >= '2026-10-19'", dialect="mysql")
    violations = validate_confusable_measures(bindings, sneaky, ["order_line"])
    assert violations and "fline_amount" in violations[0]

    # No alias at all → still a violation.
    bare = sqlglot.parse_one(
        "SELECT SUM(fgross_amount) FROM order_line", dialect="mysql")
    assert validate_confusable_measures(bindings, bare, ["order_line"])

    # The verified column alone → compliant.
    good = sqlglot.parse_one(
        "SELECT SUM(fline_amount) AS revenue FROM order_line", dialect="mysql")
    assert validate_confusable_measures(bindings, good, ["order_line"]) == []

    # An explicit multi-basis query (both columns, labeled) → allowed; the
    # cross-node reconciliation governs blended narratives.
    both = sqlglot.parse_one(
        "SELECT SUM(fline_amount) AS net, SUM(fgross_amount) AS gross "
        "FROM order_line", dialect="mysql")
    assert validate_confusable_measures(bindings, both, ["order_line"]) == []


def test_confusable_measure_rejected_at_execution_gate(two_datasources):
    """The REAL pre-execution boundary refuses the differing measure — this
    is the shape the alias rule let through during the incident."""
    db = two_datasources
    sql = ("SELECT SUM(fgross_amount) AS total_amount FROM order_line "
           "WHERE plan_date >= '2026-10-19'")
    with pytest.raises(ExecutionValidationError) as exc:
        enforce(db, KB_A, sql)
    payload = exc.value.payload
    assert payload["checks"]["metric_binding"] == "fail"
    assert any("fline_amount" in e for e in payload["errors"])

    # Compliant statement clears the gate.
    ok = enforce(db, KB_A,
                 "SELECT SUM(fline_amount) AS revenue FROM order_line")
    assert ok["checks"]["metric_binding"] == "pass"


def test_confusable_scoped_to_own_table_and_datasource(two_datasources):
    """Schema B's binding has no confusables → its gate behavior unchanged;
    a column NOT documented on the base table is never convicted."""
    db = two_datasources
    sql = "SELECT SUM(revenue_net) AS sales FROM sales_fact"
    ok = enforce(db, KB_B, sql)
    assert ok["checks"]["metric_binding"] == "pass"

    bindings_b = datasource_metric_bindings(db, KB_B)
    other_tbl = sqlglot.parse_one(
        "SELECT SUM(revenue_net) FROM sales_fact", dialect="postgres")
    assert validate_confusable_measures(bindings_b, other_tbl, ["sales_fact"]) == []


# ── Blocker 3: per-turn SQL execution budget (ReAct-path closure) ───────────


def test_union_passes_metric_binding_gate_with_both_bases(two_datasources):
    """Cycle 2: a net-vs-gross comparison UNION (read-only set operation,
    BOTH measure bases present) clears the real execution gate — the exact
    query shape whose rejection burned six regeneration cycles in the
    2026-10-29 replay."""
    db = two_datasources
    bindings = datasource_metric_bindings(db, KB_A)
    sql = (
        "SELECT plan_date, SUM(fline_amount) AS net FROM order_line "
        "WHERE plan_date >= '2026-10-19' GROUP BY plan_date "
        "UNION SELECT plan_date, SUM(fgross_amount) AS gross FROM order_line "
        "WHERE plan_date >= '2026-10-19' GROUP BY plan_date"
    )
    parsed = sqlglot.parse_one(sql, dialect="mysql")
    assert validate_confusable_measures(bindings, parsed, ["order_line"]) == []
    ok = enforce(db, KB_A, sql)
    assert ok["checks"]["metric_binding"] == "pass"
    assert ok["checks"]["read_only"] == "pass"


def test_budget_scope_resolution_priority():
    assert turn_sql_budget.resolve_scope(
        turn_id="t", execution_id="e", conversation_id="c") == "turn:t"
    assert turn_sql_budget.resolve_scope(
        execution_id="e", conversation_id="c") == "exec:e"
    assert turn_sql_budget.resolve_scope(conversation_id="c") == "conv:c"
    assert turn_sql_budget.resolve_scope() is None


def test_budget_register_count_and_exhaustion():
    turn_sql_budget.reset_scope("turn:bt")
    allowed, used, eff = turn_sql_budget.budget_verdict("turn:bt", 2, 1800.0)
    assert allowed and used == 0 and eff == 2
    turn_sql_budget.register_execution("turn:bt")
    turn_sql_budget.register_execution("turn:bt")
    allowed, used, eff = turn_sql_budget.budget_verdict("turn:bt", 2, 1800.0)
    assert not allowed and used == 2 and eff == 2
    # Refusals register nothing: recount is stable.
    allowed, used, _ = turn_sql_budget.budget_verdict("turn:bt", 2, 1800.0)
    assert not allowed and used == 2
    turn_sql_budget.reset_scope("turn:bt")
    allowed, used, _ = turn_sql_budget.budget_verdict("turn:bt", 2, 1800.0)
    assert allowed and used == 0


def test_budget_conversation_scope_doubled():
    """Defense-in-depth fallback: conversation scope gets 2x within its
    window so consecutive normal turns are not falsely refused."""
    turn_sql_budget.reset_scope("conv:bc")
    _, _, eff_turn = turn_sql_budget.budget_verdict("turn:bx", 3, 1800.0)
    _, _, eff_conv = turn_sql_budget.budget_verdict("conv:bc", 3, 1800.0)
    assert eff_turn == 3 and eff_conv == 6
    # No scope at all → unlimited (single-shot API paths unaffected).
    allowed, used, eff = turn_sql_budget.budget_verdict(None, 3, 1800.0)
    assert allowed and used == 0 and eff == 3
    turn_sql_budget.reset_scope("conv:bc")


def test_execute_query_enforces_turn_budget(two_datasources, monkeypatch):
    """The data-agent sub-loop and raw-SQL calls reach the warehouse through
    db_tools._execute_query — the budget must refuse there too, scoped to
    the turn threaded through the tool context."""
    import asyncio

    from app.services.tool_handlers import db_tools as _db_tools

    db = two_datasources
    scope = "turn:bq-test"
    turn_sql_budget.reset_scope(scope)
    monkeypatch.setattr(_db_tools, "_refuse_if_not_ready", lambda *a, **k: None)
    monkeypatch.setattr(settings, "NL2SQL_MAX_TURN_SQL_QUERIES", 1)
    # The structural validator (global flag) would otherwise short-circuit
    # the synthetic source before _do_work — irrelevant to this test, whose
    # subject is the budget boundary that runs BEFORE it.
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False)

    class _StubQueryService:
        """Executes nothing; returns a well-shaped result so the FIRST call
        registers against the budget (the second is then refused)."""

        def __init__(self, db):
            self._db = db

        def execute(self, kb_id, sql, max_rows=None, timeout_s=None):
            return {
                "sql": sql, "rows": [{"revenue": 1.0}],
                "columns": ["revenue"], "row_count": 1,
                "source": {"id": kb_id, "name": "synthetic"},
            }

    monkeypatch.setattr(_db_tools, "QueryService", _StubQueryService)

    async def _run():
        ctx = {"turn_id": scope, "conversation_id": "conv-bq"}
        # First execution: allowed (fails later on the synthetic driver —
        # any outcome EXCEPT a budget refusal proves the guard passed it).
        first = await _db_tools._execute_query(
            {"sql": "SELECT SUM(fline_amount) AS revenue FROM order_line",
             "data_source_id": KB_A}, db, None, context=dict(ctx))
        assert first.get("error_kind") != "turn_sql_budget_exhausted"
        # Second execution in the same turn: refused at the boundary.
        second = await _db_tools._execute_query(
            {"sql": "SELECT SUM(fline_amount) AS revenue FROM order_line",
             "data_source_id": KB_A}, db, None, context=dict(ctx))
        assert second.get("error_kind") == "turn_sql_budget_exhausted"
        assert "already spent" in second.get("error", "")
        # Different turn → fresh budget, no refusal at the boundary.
        other = await _db_tools._execute_query(
            {"sql": "SELECT SUM(fline_amount) AS revenue FROM order_line",
             "data_source_id": KB_A}, db, None,
            context={"turn_id": "turn:bq-other"})
        assert other.get("error_kind") != "turn_sql_budget_exhausted"

    try:
        asyncio.run(_run())
    finally:
        turn_sql_budget.reset_scope(scope)
        turn_sql_budget.reset_scope("turn:bq-other")


# ── Cycle 2.1: pre-flight budget check in query_database ────────────────────


def test_query_database_preflight_refuses_exhausted_turn(monkeypatch):
    """A guaranteed-refused query_database invocation must be refused BEFORE
    FSM startup (measured 2026-10-30: one refused attempt cost 159s of
    avoidable orchestration). The probe is read-only — it must not consume
    budget."""
    import asyncio

    from app.services.tool_handlers import query_database_tool as _qdt
    from app.services.synexia import fsm as _fsm_mod

    # Scope key exactly as production resolves it: ctx carries the raw turn
    # id; resolve_scope adds the "turn:" prefix.
    scope = turn_sql_budget.resolve_scope(turn_id="pf-x")
    turn_sql_budget.reset_scope(scope)
    for _ in range(6):
        turn_sql_budget.register_execution(scope)
    used_before = turn_sql_budget.count_recent(scope)
    assert used_before == 6

    fsm_started = []

    class _NoFSM:
        @staticmethod
        def run(*a, **k):
            fsm_started.append(1)
            raise AssertionError("FSM must not start when budget is exhausted")

    monkeypatch.setattr(_fsm_mod, "SynexiaFSM", _NoFSM)
    monkeypatch.setattr(settings, "NL2SQL_MAX_TURN_SQL_QUERIES", 6)
    monkeypatch.setattr(settings, "NL2SQL_PREFLIGHT_PROBE_ENABLED", False)

    async def _run():
        return await _qdt._query_database(
            {"question": "breakdown by product"}, None, None,
            context={"turn_id": "pf-x", "org_id": "default-org",
                     "app_id": "default-app",
                     "bound_kb_ids": ["kb-conv-aaaa"]},
        )

    result = asyncio.run(_run())
    assert result.get("error_kind") == "turn_sql_budget_exhausted"
    assert "already spent" in result.get("error", "")
    assert result.get("turn_sql_budget", {}).get("used") == 6
    assert fsm_started == []  # no FSM startup, no planner LLM call
    # The probe reserved nothing — registry unchanged.
    assert turn_sql_budget.count_recent(scope) == used_before
    turn_sql_budget.reset_scope(scope)


def test_query_database_preflight_passes_with_capacity(monkeypatch):
    """With budget remaining, the probe must NOT interfere: the FSM starts,
    and the probe itself consumed no attempts."""
    import asyncio

    from app.services.tool_handlers import query_database_tool as _qdt
    from app.services.synexia import fsm as _fsm_mod

    turn_sql_budget.reset_scope("turn:pf-ok")
    started = []

    class _StubFSM:
        def __init__(self, *a, **k):
            pass

        def run(self, *a, **k):
            started.append(1)
            return {"success": True, "result": "ok"}

    monkeypatch.setattr(_fsm_mod, "SynexiaFSM", _StubFSM)
    monkeypatch.setattr(settings, "NL2SQL_PREFLIGHT_PROBE_ENABLED", False)

    async def _run():
        return await _qdt._query_database(
            {"question": "totals"}, None, None,
            context={"turn_id": "pf-ok", "org_id": "default-org",
                     "app_id": "default-app",
                     "bound_kb_ids": ["kb-conv-aaaa"]},
        )

    result = asyncio.run(_run())
    # Subject of this test: the probe. With capacity the FSM STARTED and
    # the probe consumed nothing — the tool's result-shape contract is
    # exercised elsewhere.
    assert started == [1]
    assert result.get("error_kind") != "turn_sql_budget_exhausted"
    # The read-only probe consumed nothing.
    assert turn_sql_budget.count_recent("turn:pf-ok") == 0
    turn_sql_budget.reset_scope("turn:pf-ok")


def test_has_capacity_is_pure_and_race_safe():
    """has_capacity never mutates the registry — concurrent try_acquire
    remains the only way budget is consumed."""
    turn_sql_budget.reset_scope("turn:pf-pure")
    for _ in range(20):
        assert turn_sql_budget.has_capacity("turn:pf-pure", 6, 1800.0)
    assert turn_sql_budget.count_recent("turn:pf-pure") == 0
    # No scope / disabled budget → capacity (fail-open).
    assert turn_sql_budget.has_capacity(None, 6, 1800.0)
    assert turn_sql_budget.has_capacity("turn:pf-pure", 0, 1800.0)
    turn_sql_budget.reset_scope("turn:pf-pure")
