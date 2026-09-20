"""The execution boundary: validation every caller gets, and no way around it.

The gap this pins: validation lived in whichever caller remembered to ask.
``execute_query`` checked the schema before calling through; dashboards, report
recipes, forecasting probes and data tools called :meth:`QueryService.execute`
directly, so a model-authored table or column nobody had vouched for reached the
driver. These tests hold the checks at the boundary — where every caller, every
agent and every alternate path arrives — and hold that nothing may switch them
off.

Nothing here needs a live database: the source's own schema answers are stubbed,
and the connector is replaced, so a "pass" means the gate passed on the statement
and not that a warehouse was reachable.
"""

from __future__ import annotations

import contextlib

import pytest

from app.services.db import execution_gate as gate
from app.services.db import query_service as qs
from app.services.db.contracts import CanonicalResultSet, GroundedPlan
from app.services.db.execution_gate import ExecutionValidationError


# ── fixtures: a source, a schema, and a connector that never gets used by accident ──


class _Source:
    def __init__(self, db_type="mysql"):
        self.id = "kb-1"
        self.name = "warehouse"
        self.db_type = db_type
        self.source_kind = "database"
        self.is_deleted = False


SCHEMA = {
    "orders": ["order_id", "customer_id", "order_qty", "created_at", "region"],
    "customers": ["customer_id", "customer_name", "region"],
}
GOOD_SQL = (
    "SELECT o.order_id, SUM(o.order_qty) AS qty FROM orders o "
    "JOIN customers c ON o.customer_id = c.customer_id "
    "WHERE o.created_at >= '2026-08-01' GROUP BY o.order_id"
)


def _stub_source(monkeypatch, *, db_type="mysql", schema=None, source=None):
    """A reachable, introspectable source — the only thing the gate may trust."""
    monkeypatch.setattr(gate, "_load_source", lambda db, kb_id: source or _Source(db_type))
    tables = {t.lower() for t in (schema or SCHEMA)}
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: tables)

    def _columns(db, kb_id, table):
        table = table.split(".")[-1].lower()
        if schema is None and table not in SCHEMA:
            return None
        return list((schema or SCHEMA).get(table, [])) or None

    monkeypatch.setattr(gate, "_described_columns", _columns)
    monkeypatch.setattr(gate, "_configured_row_filters", lambda db, kb_id: [])


@contextlib.contextmanager
def _connector(rows=None, executed=None):
    class _Conn:
        last_executed_sql = executed or ""

        def execute(self, sql, max_rows=10, timeout_s=5):
            return list(rows or [])

    yield _Conn()


def _stub_boundary(monkeypatch, *, rows=None, executed=None, schema=None, db_type="mysql"):
    """QueryService with a fake KB lookup and a recording connector."""
    _stub_source(monkeypatch, db_type=db_type, schema=schema)
    monkeypatch.setattr(qs.QueryService, "_load_kb", lambda self, kb_id: _Source(db_type))
    calls: list[str] = []

    def _factory(kb):
        calls.append(getattr(kb, "id", ""))

        @contextlib.contextmanager
        def _cm():
            with _connector(rows=rows, executed=executed) as conn:
                yield conn

        return _cm()

    monkeypatch.setattr(qs, "get_connector", _factory)
    return calls


# ── 1 & 9: a valid statement on both dialects ─────────────────────────────


def test_a_valid_mysql_query_passes_every_check(monkeypatch):
    _stub_source(monkeypatch)
    payload = gate.enforce(None, "kb-1", GOOD_SQL)
    assert payload["ok"] is True
    assert payload["checks"]["source_authorization"] == "pass"
    assert payload["checks"]["read_only"] == "pass"
    assert payload["checks"]["dialect_validity"] == "pass"
    assert payload["checks"]["table_existence"] == "pass"
    assert payload["checks"]["column_existence"] == "pass"
    assert payload["authored_sql"] == GOOD_SQL, "the statement as authored is preserved"
    assert payload["executed_sql"] == "", "nothing has been sent at validation time"
    assert payload["enforced"] is True
    assert payload["flag_effect"] == "observability-only"


def test_a_valid_query_still_executes_on_mysql_and_postgres(monkeypatch):
    for db_type, sql in (
        ("mysql", GOOD_SQL),
        ("postgresql", GOOD_SQL.replace("'2026-08-01'", "DATE '2026-08-01'")),
    ):
        calls = _stub_boundary(monkeypatch, rows=[{"order_id": 1, "qty": 5}], executed="RAN " + db_type, db_type=db_type)
        result = qs.QueryService(object()).execute("kb-1", sql)
        assert calls == ["kb-1"], "the connector was opened exactly once"
        assert result["rows"] == [{"order_id": 1, "qty": 5}]
        assert result["validation"]["ok"] is True
        assert result["validation"]["executed_sql"] == "RAN " + db_type, (
            "the executed statement is read back, not assumed"
        )


# ── 2 & 3: nothing may switch enforcement off ─────────────────────────────


def test_the_shadow_flag_cannot_disable_enforcement(monkeypatch):
    from app.config import settings

    _stub_source(monkeypatch)
    bad = "SELECT nope FROM ghost_table"
    for flag in (True, False):
        monkeypatch.setattr(settings, "NL2SQL_SHADOW_MODE_ENABLED", flag, raising=False)
        with pytest.raises(ExecutionValidationError) as caught:
            gate.enforce(None, "kb-1", bad)
        assert caught.value.payload["checks"]["table_existence"] == "fail"
        assert caught.value.payload["flag_effect"] == "observability-only"


def test_direct_execute_query_cannot_bypass_validation(monkeypatch):
    """A caller that never asked for a schema check still gets one."""
    opened: list[str] = []
    _stub_source(monkeypatch, schema={})  # nothing is known: the statement must be refused
    monkeypatch.setattr(qs.QueryService, "_load_kb", lambda self, kb_id: _Source())

    def _factory(kb):  # pragma: no cover - must never run
        opened.append("connector")
        raise AssertionError("the database was contacted for an unvalidated statement")

    monkeypatch.setattr(qs, "get_connector", _factory)
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: {"orders"})
    with pytest.raises(ExecutionValidationError):
        qs.QueryService(object()).execute("kb-1", "SELECT * FROM not_a_table")
    assert opened == [], "the statement must be refused before the driver sees it"


def test_an_alternate_caller_gets_the_same_gate(monkeypatch):
    """Bare call, and a call carrying policy + plan context: same enforcement."""
    _stub_boundary(monkeypatch, rows=[{"x": 1}])
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: {"orders"})
    ok_sql = "SELECT order_id FROM orders"
    assert qs.QueryService(object()).execute("kb-1", ok_sql)["rows"] == [{"x": 1}]

    _stub_boundary(monkeypatch, rows=[{"x": 1}])
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: {"orders"})
    plan = GroundedPlan(intent="i", question="q", source_ids=["kb-1"], tables=["orders"])
    svc = qs.QueryService(object(), policy=None, plan=plan)
    assert svc.execute("kb-1", ok_sql)["rows"] == [{"x": 1}]

    # …and the same caller is refused when the statement names a table that is not there
    _stub_boundary(monkeypatch, rows=[{"x": 1}])
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: {"orders"})
    with pytest.raises(ExecutionValidationError):
        qs.QueryService(object(), policy=None, plan=plan).execute("kb-1", "SELECT * FROM ghost")


# ── 4 & 5: objects the source does not have ───────────────────────────────


def test_a_nonexistent_table_is_refused(monkeypatch):
    _stub_source(monkeypatch)
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-1", "SELECT * FROM ghost_table")
    payload = caught.value.payload
    assert payload["checks"]["table_existence"] == "fail"
    assert "does not exist" in payload["errors"][0]
    assert payload["authored_sql"].endswith("ghost_table")


def test_a_nonexistent_column_is_refused(monkeypatch):
    _stub_source(monkeypatch)
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-1", "SELECT ghost_column FROM orders")
    payload = caught.value.payload
    assert payload["checks"]["column_existence"] == "fail"
    assert "ghost_column" in payload["errors"][0]


def test_an_unprovable_schema_is_not_a_pass_and_not_a_refusal(monkeypatch):
    """Introspection unavailable: recorded as unavailable, never as valid."""
    monkeypatch.setattr(gate, "_load_source", lambda db, kb_id: _Source())
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: set())
    payload = gate.enforce(None, "kb-1", "SELECT order_id FROM orders")
    assert payload["checks"]["table_existence"] == "unavailable"
    assert payload["checks"]["column_existence"] == "unavailable"
    assert payload["checks"]["source_authorization"] == "pass"


# ── 6: a join that cannot be evaluated ───────────────────────────────────


def test_an_unreachable_join_is_refused(monkeypatch):
    _stub_source(monkeypatch)
    sql = (
        "SELECT c.customer_name FROM orders o "
        "JOIN customers c ON o.not_a_key = c.customer_id"
    )
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-1", sql)
    payload = caught.value.payload
    assert payload["checks"]["join_reachability"] == "fail"
    assert any("join key" in e for e in payload["errors"]), payload["errors"]


def test_a_join_the_catalog_does_not_know_is_recorded_not_refused(monkeypatch):
    _stub_source(monkeypatch)
    payload = gate.enforce(None, "kb-1", GOOD_SQL)
    assert payload["checks"]["join_reachability"] == "unbacked"
    assert payload["ok"] is True, "an unbacked relation is evidence, not a refusal"
    assert payload["detail"]["joins"], "the join was examined and is on the record"


# ── 7: the source itself ─────────────────────────────────────────────────


def test_an_unauthorized_source_is_refused(monkeypatch):
    monkeypatch.setattr(
        gate, "_load_source", lambda db, kb_id: _Source()
    )
    monkeypatch.setattr(gate, "_known_tables", lambda db, kb_id: {"orders"})

    boom = _Source()
    boom.source_kind = "file"

    def _refuse(db, kb_id):
        raise ExecutionValidationError(
            {
                "ok": False,
                "checks": {"source_authorization": "fail"},
                "errors": [f"data source {kb_id!r} is not a database source (source_kind='file')"],
                "detail": {"hints": []},
                "source_id": kb_id,
                "authored_sql": "",
                "executed_sql": "",
                "enforced": True,
                "flag_effect": "observability-only",
            }
        )

    monkeypatch.setattr(gate, "_load_source", _refuse)
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-9", "SELECT order_id FROM orders")
    assert caught.value.payload["checks"]["source_authorization"] == "fail"
    assert "not a database source" in str(caught.value)


# ── 8: policy and row filters ────────────────────────────────────────────


def test_a_blocked_table_is_refused_by_policy_and_absent_policy_is_not_a_pass(monkeypatch):
    _stub_source(monkeypatch)

    class _Policy:
        def blocked_tables_for_kb(self, kb_id):
            return ["orders"]

        def allowed_tables_for_kb(self, kb_id):
            return []

    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-1", "SELECT order_id FROM orders", policy=_Policy())
    assert caught.value.payload["checks"]["policy"] == "fail"
    assert "may not read" in caught.value.payload["errors"][0]

    assert gate.enforce(None, "kb-1", "SELECT order_id FROM orders")["checks"]["policy"] == (
        "not-in-scope"
    ), "no policy in scope is an observed state, never a pass"


def test_an_unapplied_row_filter_is_refused(monkeypatch):
    _stub_source(monkeypatch)
    filters = [{"table": "orders", "column": "region", "value": "north"}]
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(
            None,
            "kb-1",
            "SELECT order_id FROM orders",
            row_filters=filters,
        )
    assert caught.value.payload["checks"]["row_filter"] == "fail"
    assert "row filter" in caught.value.payload["errors"][0]

    applied = gate.enforce(
        None,
        "kb-1",
        "SELECT order_id FROM orders WHERE region = 'north'",
        row_filters=filters,
    )
    assert applied["checks"]["row_filter"] == "pass"


# ── 9: the metric/ratio contract, before publication ──────────────────────


def test_a_broken_ratio_contract_stops_the_statement_before_execution(monkeypatch):
    _stub_source(monkeypatch)
    plan = GroundedPlan(
        intent="i",
        question="q",
        source_ids=["kb-1"],
        tables=["orders"],
        ratio_contracts=[
            {
                "metric_id": "fulfilment rate",
                "numerator": "SUM(order_qty)",
                "denominator": "SUM(order_qty)",  # divides by itself
                "numerator_grain": "quantity",
                "denominator_grain": "quantity",
                "required_grain": "quantity",
            }
        ],
    )
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-1", "SELECT order_id FROM orders", plan=plan)
    assert caught.value.payload["checks"]["metric_contract"] == "fail"
    assert "divides a value by itself" in " ".join(caught.value.payload["errors"])


def test_an_absent_plan_leaves_the_contract_check_unclaimed(monkeypatch):
    _stub_source(monkeypatch)
    payload = gate.enforce(None, "kb-1", "SELECT order_id FROM orders")
    assert payload["checks"]["metric_contract"] == "not-in-scope"
    assert payload["checks"]["join_reachability"] == "not-applicable"


# ── read-only and dialect validity ───────────────────────────────────────


def test_a_write_statement_is_refused(monkeypatch):
    _stub_source(monkeypatch)
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-1", "DELETE FROM orders")
    assert caught.value.payload["checks"]["read_only"] == "fail"


def test_an_unparseable_statement_is_refused(monkeypatch):
    _stub_source(monkeypatch)
    with pytest.raises(ExecutionValidationError) as caught:
        gate.enforce(None, "kb-1", "SELECT FROM WHERE")
    assert caught.value.payload["checks"]["dialect_validity"] == "fail"


# ── 5 & 6: a structured failure, with provenance ─────────────────────────


def test_a_refused_statement_is_structured_on_the_plan_and_the_result_set(monkeypatch):
    _stub_source(monkeypatch)
    plan = GroundedPlan(intent="i", question="q", source_ids=["kb-1"], tables=["orders"])
    try:
        gate.enforce(None, "kb-1", "SELECT * FROM ghost_table", plan=plan)
    except ExecutionValidationError as exc:
        payload = exc.payload
    else:  # pragma: no cover
        raise AssertionError("expected a refusal")

    plan.execution_validation = payload
    assert plan.to_dict()["execution_validation"]["checks"]["table_existence"] == "fail"

    crs = gate.failure_result_set(plan, payload)
    assert isinstance(crs, CanonicalResultSet)
    assert crs.complete is False
    assert crs.rows == []
    assert crs.execution_validation["authored_sql"] == "SELECT * FROM ghost_table"
    assert crs.execution_validation["executed_sql"] == "", "nothing was executed"
    assert crs.disclosure and "not zero" in crs.disclosure
    assert crs.validate() == [], crs.validate()
    assert crs.to_dict()["execution_validation"]["errors"]


def test_the_ratio_seam_still_refuses_an_unverifiable_ratio_before_publication():
    """P5-A does not move the ratio gate: it stays where publication is decided."""
    from app.services.db import grounding as g
    from app.services.db import ratio_contract as rc

    sql = (
        "SELECT SUM(shipped_qty) / COUNT(DISTINCT order_id) AS fulfilment_rate "
        "FROM orders WHERE created_at >= '2026-08-01'"
    )
    plan = GroundedPlan(
        intent="i",
        question="q",
        source_ids=["kb-1"],
        tables=["orders"],
        evidence=[
            {
                "slot": "fulfilment rate",
                "label": "fulfilment rate",
                "matched": [],
                "score": 1.0,
                "tables": ["orders"],
                "columns": ["orders.shipped_qty"],
                "source": "catalog",
            }
        ],
    )
    crs = g.build_canonical_result_set(
        plan=plan,
        exec_result={"rows": [{"shipped_qty": 100.0, "fulfilment_rate": 17.0}], "truncated": False, "sql": sql},
        sql=sql,
        provenance={"tables": ["orders"]},
    )
    assert crs.ratio_validations and crs.ratio_validations[0]["ok"] is False
    assert "quantity_over_count" in crs.ratio_validations[0]["reasons"]
    assert [s["reason"] for s in crs.unsupported_slots] == ["ratio_unverified"]
    assert rc.ratio_disclosure(crs.ratio_validations[0]).startswith("fulfilment rate")
    assert crs.complete is False
